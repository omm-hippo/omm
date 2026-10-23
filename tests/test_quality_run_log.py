"""#390: benchmark run-log diagnostics.

All 8 benchmarks of a Windows `omm contribute` run ended at exactly the 600s
session deadline, and nothing on disk could say whether Ollama stalled or
every request just ran slowly: the isolated worker is a spawned process with
no log handler, so whatever it logged was lost. These pin the local run-log
trail that will answer that on the next run.
"""

from __future__ import annotations

import json
import logging
import multiprocessing

import pytest

from omm import quality, runlog


def _run_log(home):
    runlog.finish(0, "ok")
    path = next(iter((home / "logs").glob("*.jsonl")))
    text = path.read_text(encoding="utf-8")
    return text, [json.loads(line) for line in text.splitlines()]


def test_generate_request_logs_timing_and_placement_but_never_the_prompt(
    isolated_omm_home, monkeypatch
):
    def fake_generate(tag, prompt, generation, num_predict=None, **_kwargs):
        return {
            "response": "SECRET ANSWER",
            "eval_count": 64,
            "eval_duration": 16_000_000_000,
            "prompt_eval_count": 40,
            "prompt_eval_duration": 2_000_000_000,
            "load_duration": 30_000_000_000,
            "total_duration": 48_000_000_000,
            "done_reason": "length",
        }

    monkeypatch.setattr(quality, "_generate", fake_generate)
    runlog.start(["contribute"])
    quality._generate_with_runtime(
        "model:latest",
        "SECRET PROMPT",
        {"temperature": 0, "seed": 0, "num_ctx": 4096, "num_predict": 256, "think": False},
        64,
        {"num_ctx": 1024, "num_gpu": 0, "num_thread": 16, "num_batch": 128},
    )
    text, records = _run_log(isolated_omm_home)

    (record,) = [r for r in records if r.get("event") == "generate"]
    assert record["model"] == "model:latest"
    assert record["engine"] == "ollama"
    fields = record["fields"]
    assert fields["num_predict"] == 64
    assert fields["num_ctx"] == 1024
    assert fields["num_gpu"] == 0
    assert fields["num_thread"] == 16
    assert fields["num_batch"] == 128
    assert fields["eval_count"] == 64
    assert fields["prompt_eval_count"] == 40
    assert fields["eval_duration_seconds"] == 16.0
    assert fields["load_duration_seconds"] == 30.0
    assert fields["prompt_eval_duration_seconds"] == 2.0
    assert fields["total_duration_seconds"] == 48.0
    assert fields["tokens_per_sec"] == 4.0
    assert fields["done_reason"] == "length"
    assert isinstance(fields["wall_seconds"], float)
    assert "SECRET PROMPT" not in text
    assert "SECRET ANSWER" not in text


def test_failed_generate_request_logs_its_failure_reason(isolated_omm_home, monkeypatch):
    def fake_generate(*_args, **_kwargs):
        raise quality.QualityEvaluationError(
            "Ollama /api/generate request failed",
            failure_reason=quality.FAILURE_REASON_GENERATION_TIMEOUT,
        )

    monkeypatch.setattr(quality, "_generate", fake_generate)
    runlog.start(["contribute"])
    with pytest.raises(quality.QualityEvaluationError):
        quality._generate_with_runtime("model:latest", "p", None, 8, None)
    _text, records = _run_log(isolated_omm_home)

    (record,) = [r for r in records if r.get("event") == "generate"]
    assert record["level"] == "WARNING"
    assert record["reason"] == quality.FAILURE_REASON_GENERATION_TIMEOUT


def test_worker_log_records_survive_a_real_pipe_into_the_run_log(isolated_omm_home):
    receive_conn, send_conn = multiprocessing.Pipe(duplex=False)
    source = logging.getLogger("omm.quality.test_worker")
    handler = quality._PipeLogHandler(send_conn)
    source.addHandler(handler)
    source.propagate = False
    try:
        source.warning(
            "generate x took 1.0s",
            extra={"event": "generate", "model": "x", "fields": {"wall_seconds": 1.0}},
        )
    finally:
        source.removeHandler(handler)
        source.propagate = True
    kind, payload = receive_conn.recv()

    runlog.start(["contribute"])
    assert quality._reemit_worker_log(payload) is payload
    _text, records = _run_log(isolated_omm_home)

    assert kind == "log"
    (record,) = [r for r in records if r.get("event") == "generate"]
    assert record["msg"] == "generate x took 1.0s"
    assert record["logger"] == "omm.quality.test_worker"
    assert record["model"] == "x"
    assert record["fields"] == {"wall_seconds": 1.0}


def test_reemit_ignores_a_malformed_payload():
    assert quality._reemit_worker_log(None) is None
    assert quality._reemit_worker_log({"msg": "no logger name"}) is None


def _worker_log_messages():
    """The exact ("log", payload) tuples a worker's _PipeLogHandler sends:
    a start line and one finished generation request."""
    sent = []

    class Sink:
        def send(self, message):
            sent.append(message)

    handler = quality._PipeLogHandler(Sink())
    previous_level = quality.log.level
    quality.log.addHandler(handler)
    quality.log.setLevel(logging.INFO)
    try:
        quality.log.info(
            "benchmark worker started",
            extra={"event": "benchmark_worker_started", "model": "model"},
        )
        quality._log_generate_request(
            "model", "ollama", 0.0, None, 8, {"num_gpu": 0}, {"eval_count": 8}
        )
    finally:
        quality.log.removeHandler(handler)
        quality.log.setLevel(previous_level)
    assert [message[0] for message in sent] == ["log", "log"]
    return sent


class _ScriptedConnection:
    def __init__(self, messages):
        self.messages = list(messages)

    def poll(self, timeout=None):
        return bool(self.messages)

    def recv(self):
        return self.messages.pop(0)

    def close(self):
        pass


class _ScriptedProcess:
    exitcode = 0

    def __init__(self, alive):
        self.alive = alive

    def start(self):
        pass

    def is_alive(self):
        return self.alive

    def terminate(self):
        self.alive = False

    def join(self, timeout=None):
        pass

    def kill(self):
        self.alive = False


def _scripted_worker(monkeypatch, messages, *, alive):
    receive_conn = _ScriptedConnection(messages)

    class Context:
        def Pipe(self, duplex=False):
            return receive_conn, _ScriptedConnection([])

        def Process(self, **kwargs):
            return _ScriptedProcess(alive)

    monkeypatch.setattr(quality.multiprocessing, "get_context", lambda method: Context())


def test_isolated_evaluator_reads_forwarded_logs_before_the_worker_result(
    isolated_omm_home, monkeypatch
):
    # The worker can log, send its result and exit before the parent reads
    # anything. Queued log lines must not make that look like a worker that
    # "exited without a result".
    _scripted_worker(
        monkeypatch, [*_worker_log_messages(), ("ok", {"speed": "result"})], alive=False
    )
    runlog.start(["contribute"])

    result = quality.evaluate_model_isolated("model", {}, timeout_seconds=600)
    _text, records = _run_log(isolated_omm_home)

    assert result == {"speed": "result"}
    events = [r.get("event") for r in records]
    assert "benchmark_worker_started" in events
    assert "generate" in events


def test_isolated_evaluator_logs_how_far_the_worker_got_at_the_deadline(
    isolated_omm_home, monkeypatch
):
    _scripted_worker(monkeypatch, _worker_log_messages(), alive=True)
    clock = iter(range(0, 100_000, 100))
    monkeypatch.setattr(quality.time, "monotonic", lambda: float(next(clock)))
    runlog.start(["contribute"])

    with pytest.raises(quality.QualityEvaluationError, match="session deadline") as error:
        quality.evaluate_model_isolated(
            "model", {}, timeout_seconds=600, runtime_options={"num_gpu": 0, "num_thread": 16}
        )
    _text, records = _run_log(isolated_omm_home)

    # The user-facing outcome is unchanged; only the run log learned more.
    assert error.value.failure_reason == quality.FAILURE_REASON_GENERATION_TIMEOUT
    (record,) = [r for r in records if r.get("event") == "benchmark_deadline"]
    assert record["level"] == "WARNING"
    fields = record["fields"]
    assert fields["timeout_seconds"] == 600
    assert fields["worker_started"] is True
    assert fields["requests_finished"] == 1
    assert fields["requests_failed"] == 0
    assert fields["seconds_since_last_request"] > 0
    assert fields["runtime_options"] == {"num_gpu": 0, "num_thread": 16}
