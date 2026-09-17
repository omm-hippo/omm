from __future__ import annotations

import json
from pathlib import Path

import pytest

from omm import coding_eval


def test_default_pack_is_bounded_versioned_and_digest_pinned():
    pack = coding_eval.load_pack()
    assert pack.pack_id == "omm-python-coding-smoke"
    assert pack.version == "1.0.0"
    assert pack.image.endswith("@sha256:9c51ecce261773a684c8345b2d4673700055c513b4d54bc0719337d3e4ee552e")
    assert {task.kind for task in pack.tasks} == {"generation", "repair"}
    assert pack.generation["temperature"] == 0
    assert len(pack.sha256) == 64


def test_pack_rejects_duplicate_ids_and_unpinned_image(tmp_path):
    source = json.loads(coding_eval.default_pack_path().read_text(encoding="utf-8"))
    source["items"].append(dict(source["items"][0]))
    path = tmp_path / "duplicate.json"
    path.write_text(json.dumps(source), encoding="utf-8")
    with pytest.raises(coding_eval.CodingEvaluationError, match="duplicate"):
        coding_eval.load_pack(path)
    source["items"].pop()
    source["sandbox"]["image"] = "python:latest"
    path.write_text(json.dumps(source), encoding="utf-8")
    with pytest.raises(coding_eval.CodingEvaluationError, match="pinned"):
        coding_eval.load_pack(path)


def test_extract_python_source_accepts_one_fence_and_rejects_ambiguous():
    assert coding_eval.extract_python_source("```python\ndef f():\n    return 1\n```") == "def f():\n    return 1\n"
    assert coding_eval.extract_python_source("def f(): return 1") == "def f(): return 1\n"
    with pytest.raises(coding_eval.CodingEvaluationError, match="ambiguous"):
        coding_eval.extract_python_source("```python\nx=1\n```\n```python\ny=2\n```")


def test_sandbox_argv_has_security_boundaries(tmp_path):
    pack = coding_eval.load_pack()
    argv = coding_eval.sandbox_argv("/usr/bin/docker", tmp_path, pack, tmp_path / "cid")
    text = " ".join(argv)
    assert "--network none" in text
    assert "--read-only" in argv
    assert "--pull never" in text
    assert "--user 65534:65534" in text
    assert "--cap-drop ALL" in text
    assert "no-new-privileges" in text
    assert "readonly" in text
    assert pack.image in argv
    assert argv[-4:] == ["python", "-I", "-B", "/workspace/test_solution.py"]


def test_run_in_sandbox_reports_pass_without_storing_source(monkeypatch, tmp_path):
    pack = coding_eval.load_pack()
    task = pack.tasks[0]

    class FakeStdout:
        chunks = iter([b".....\nRan 5 tests\nOK\n", b""])
        def read(self, _size):
            return next(self.chunks)

    class FakeProcess:
        stdout = FakeStdout()
        returncode = 0
        def poll(self): return 0
        def wait(self, timeout=None): return 0
        def terminate(self): pass
        def kill(self): pass

    captured = {}
    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        return FakeProcess()

    monkeypatch.setattr(coding_eval.subprocess, "Popen", fake_popen)
    result = coding_eval.run_in_sandbox("def chunks(values, size): return []", task, pack, runtime="docker")
    assert result.outcome == "completed"
    assert result.tests_passed == task.test_count
    assert "def chunks" not in result.output
    assert captured["argv"][0] == "docker"


def test_find_runtime_fails_closed(monkeypatch):
    monkeypatch.setattr(coding_eval.shutil, "which", lambda name: None)
    with pytest.raises(coding_eval.SandboxUnavailable):
        coding_eval.find_runtime()


def test_runtime_preflight_requires_running_service(monkeypatch):
    class Result:
        returncode = 1
    monkeypatch.setattr(coding_eval.subprocess, "run", lambda *args, **kwargs: Result())
    with pytest.raises(coding_eval.SandboxUnavailable, match="not running"):
        coding_eval.ensure_runtime_available("docker")


def test_image_preflight_requires_explicit_pull(monkeypatch):
    class Result:
        returncode = 1
    monkeypatch.setattr(coding_eval.subprocess, "run", lambda *args, **kwargs: Result())
    with pytest.raises(coding_eval.SandboxUnavailable, match="pull"):
        coding_eval.ensure_image_available("docker", coding_eval.DEFAULT_IMAGE)


def test_output_limit_kills_container_without_retaining_unbounded_text(monkeypatch):
    pack = coding_eval.load_pack()
    task = pack.tasks[0]

    class LoudStdout:
        calls = 0
        def read(self, _size):
            self.calls += 1
            return b"x" * (coding_eval.MAX_OUTPUT_BYTES + 1) if self.calls == 1 else b""

    class FakeProcess:
        stdout = LoudStdout()
        returncode = None
        def poll(self): return self.returncode
        def wait(self, timeout=None): self.returncode = self.returncode if self.returncode is not None else -15; return self.returncode
        def terminate(self): self.returncode = -15
        def kill(self): self.returncode = -9

    killed = []
    monkeypatch.setattr(coding_eval.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(coding_eval, "_kill_container", lambda runtime, cidfile: killed.append(runtime))
    result = coding_eval.run_in_sandbox("x = 1", task, pack, runtime="docker")
    assert result.outcome == "output_limit"
    assert len(result.output.encode()) == coding_eval.MAX_OUTPUT_BYTES
    assert killed == ["docker"]


def test_timeout_kills_container(monkeypatch):
    pack = coding_eval.load_pack()
    task = pack.tasks[0]

    class EmptyStdout:
        def read(self, _size): return b""

    class FakeProcess:
        stdout = EmptyStdout()
        returncode = None
        def poll(self): return self.returncode
        def wait(self, timeout=None): self.returncode = self.returncode if self.returncode is not None else -15; return self.returncode
        def terminate(self): self.returncode = -15
        def kill(self): self.returncode = -9

    times = iter([0.0, 0.0, 100.0, 100.0])
    killed = []
    monkeypatch.setattr(coding_eval.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(coding_eval.time, "monotonic", lambda: next(times, 100.0))
    monkeypatch.setattr(coding_eval, "_kill_container", lambda runtime, cidfile: killed.append(runtime))
    result = coding_eval.run_in_sandbox("x = 1", task, pack, runtime="docker")
    assert result.outcome == "timeout"
    assert killed == ["docker"]


def test_evaluate_pack_keeps_only_structured_results(monkeypatch):
    pack = coding_eval.load_pack()
    monkeypatch.setattr(
        coding_eval,
        "run_in_sandbox",
        lambda response, task, pack, runtime=None: coding_eval.SandboxResult(
            "completed", task.test_count, task.test_count, 0.2, "private generated output"
        ),
    )
    report = coding_eval.evaluate_pack(
        "model:latest",
        pack,
        lambda prompt: "```python\ndef placeholder(): return 1\n```",
        runtime="docker",
        model_provider="huggingface",
        model_repo_id="org/model",
        model_filename="model-Q4_K_M.gguf",
        model_digest="a" * 64,
        quantization="Q4_K_M",
        engine_version="1.0",
    )
    payload = report.as_dict()
    assert payload["summary"]["solved"] == len(pack.tasks)
    assert payload["model_repo_id"] == "org/model"
    assert payload["raw_responses_stored"] is False
    serialized = json.dumps(payload)
    assert "private generated output" not in serialized
    assert "def placeholder" not in serialized
