"""Versioned Python coding packs and container-only execution.

Generated source is untrusted. This module never executes it with the host
Python interpreter; Docker or Podman is mandatory.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from importlib.resources import files
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time


MAX_PACK_BYTES = 1_000_000
MAX_ITEMS = 50
MAX_TEXT_CHARS = 50_000
MAX_OUTPUT_BYTES = 64 * 1024
DEFAULT_IMAGE = (
    "docker.io/library/python:3.12.10-alpine3.21@"
    "sha256:9c51ecce261773a684c8345b2d4673700055c513b4d54bc0719337d3e4ee552e"
)
_IMAGE_RE = re.compile(r"^[a-z0-9./:_-]+@sha256:[0-9a-f]{64}$")
_FENCE_RE = re.compile(r"```(?:python)?\s*\n(.*?)```", re.IGNORECASE | re.DOTALL)


class CodingEvaluationError(RuntimeError):
    pass


class SandboxUnavailable(CodingEvaluationError):
    pass


@dataclass(frozen=True)
class CodingTask:
    id: str
    kind: str
    prompt: str
    starter: str
    tests: str
    test_count: int


@dataclass(frozen=True)
class CodingPack:
    pack_id: str
    version: str
    language: str
    evaluator: str
    license: str
    source: str
    generation: dict
    image: str
    timeout_seconds: int
    memory_mb: int
    cpus: float
    pids: int
    tasks: tuple[CodingTask, ...]
    sha256: str


@dataclass(frozen=True)
class SandboxResult:
    outcome: str
    tests_passed: int
    tests_total: int
    duration_seconds: float
    output: str


@dataclass(frozen=True)
class CodingTaskResult:
    id: str
    kind: str
    outcome: str
    tests_passed: int
    tests_total: int
    duration_seconds: float


@dataclass(frozen=True)
class CodingEvaluationReport:
    schema_version: int
    created_at: str
    model: str
    model_provider: str | None
    model_repo_id: str | None
    model_filename: str | None
    model_digest: str | None
    quantization: str | None
    engine: str
    engine_version: str | None
    pack_id: str
    pack_version: str
    pack_sha256: str
    tasks: tuple[CodingTaskResult, ...]
    raw_responses_stored: bool = False

    def as_dict(self) -> dict:
        kinds = sorted({task.kind for task in self.tasks})
        by_kind = {
            kind: {
                "solved": sum(1 for task in self.tasks if task.kind == kind and task.outcome == "completed"),
                "total": sum(1 for task in self.tasks if task.kind == kind),
                "tests_passed": sum(task.tests_passed for task in self.tasks if task.kind == kind),
                "tests_total": sum(task.tests_total for task in self.tasks if task.kind == kind),
            }
            for kind in kinds
        }
        solved = sum(1 for task in self.tasks if task.outcome == "completed")
        return {
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "model": self.model,
            "model_provider": self.model_provider,
            "model_repo_id": self.model_repo_id,
            "model_filename": self.model_filename,
            "model_digest": self.model_digest,
            "quantization": self.quantization,
            "engine": self.engine,
            "engine_version": self.engine_version,
            "pack": {
                "id": self.pack_id,
                "version": self.pack_version,
                "sha256": self.pack_sha256,
            },
            "summary": {
                "solved": solved,
                "total": len(self.tasks),
                "score": round(solved / len(self.tasks), 4) if self.tasks else 0.0,
                "by_kind": by_kind,
            },
            "tasks": [
                {
                    "id": task.id,
                    "kind": task.kind,
                    "outcome": task.outcome,
                    "tests_passed": task.tests_passed,
                    "tests_total": task.tests_total,
                    "duration_seconds": round(task.duration_seconds, 3),
                }
                for task in self.tasks
            ],
            "raw_responses_stored": self.raw_responses_stored,
        }


def default_pack_path() -> Path:
    return Path(str(files("omm").joinpath("data/coding-pack-v1.json")))


def _bounded_int(value: object, minimum: int, maximum: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise CodingEvaluationError(f"{label} must be an integer from {minimum} to {maximum}")
    return value


def load_pack(path: Path | None = None) -> CodingPack:
    target = path or default_pack_path()
    try:
        raw = target.read_bytes()
    except OSError as error:
        raise CodingEvaluationError(f"could not read coding pack: {error}") from error
    if len(raw) > MAX_PACK_BYTES:
        raise CodingEvaluationError("coding pack is too large")
    try:
        payload = json.loads(raw)
    except (UnicodeError, ValueError) as error:
        raise CodingEvaluationError("coding pack is not valid JSON") from error
    if not isinstance(payload, dict) or payload.get("schema_version") != 2:
        raise CodingEvaluationError("unsupported coding pack schema")
    required_text = ("pack_id", "version", "language", "evaluator", "license", "source")
    for key in required_text:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > 200:
            raise CodingEvaluationError(f"coding pack {key} is missing or invalid")
    if payload["language"] != "python" or payload["evaluator"] != "python-unittest-v1":
        raise CodingEvaluationError("unsupported coding pack language or evaluator")
    generation = payload.get("generation")
    if not isinstance(generation, dict):
        raise CodingEvaluationError("coding pack generation settings are missing")
    for key in ("seed", "num_ctx", "num_predict"):
        _bounded_int(generation.get(key), 0 if key == "seed" else 1, 131072 if key == "num_ctx" else 4096, f"generation {key}")
    temperature = generation.get("temperature")
    if isinstance(temperature, bool) or not isinstance(temperature, (int, float)) or not 0 <= float(temperature) <= 2:
        raise CodingEvaluationError("generation temperature must be from 0 to 2")
    if not isinstance(generation.get("think"), bool):
        raise CodingEvaluationError("generation think must be boolean")
    sandbox = payload.get("sandbox")
    if not isinstance(sandbox, dict):
        raise CodingEvaluationError("coding pack sandbox settings are missing")
    image = sandbox.get("image")
    if not isinstance(image, str) or not _IMAGE_RE.fullmatch(image):
        raise CodingEvaluationError("sandbox image must be pinned by sha256 digest")
    if image != DEFAULT_IMAGE:
        raise CodingEvaluationError("coding packs must use OMM's reviewed sandbox image")
    timeout = _bounded_int(sandbox.get("timeout_seconds"), 1, 120, "sandbox timeout")
    memory = _bounded_int(sandbox.get("memory_mb"), 64, 4096, "sandbox memory")
    pids = _bounded_int(sandbox.get("pids"), 8, 256, "sandbox pids")
    cpus = sandbox.get("cpus")
    if isinstance(cpus, bool) or not isinstance(cpus, (int, float)) or not 0.1 <= float(cpus) <= 8:
        raise CodingEvaluationError("sandbox cpus must be from 0.1 to 8")
    items = payload.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
        raise CodingEvaluationError(f"coding pack requires 1 to {MAX_ITEMS} items")
    tasks = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            raise CodingEvaluationError("coding pack items must be objects")
        task_id = item.get("id")
        if not isinstance(task_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", task_id):
            raise CodingEvaluationError("coding task id is invalid")
        if task_id in seen:
            raise CodingEvaluationError(f"duplicate coding task id: {task_id}")
        seen.add(task_id)
        kind = item.get("kind")
        if kind not in {"generation", "repair"}:
            raise CodingEvaluationError(f"unsupported coding task kind: {kind}")
        values = {}
        for key in ("prompt", "starter", "tests"):
            value = item.get(key)
            if not isinstance(value, str) or len(value) > MAX_TEXT_CHARS:
                raise CodingEvaluationError(f"coding task {task_id} has invalid {key}")
            values[key] = value
        if not values["prompt"].strip() or not values["tests"].strip():
            raise CodingEvaluationError(f"coding task {task_id} is incomplete")
        tasks.append(
            CodingTask(
                task_id,
                kind,
                values["prompt"],
                values["starter"],
                values["tests"],
                _bounded_int(item.get("test_count"), 1, 100, f"{task_id} test_count"),
            )
        )
    return CodingPack(
        payload["pack_id"], payload["version"], payload["language"], payload["evaluator"],
        payload["license"], payload["source"], dict(generation), image, timeout, memory, float(cpus), pids,
        tuple(tasks), hashlib.sha256(raw).hexdigest(),
    )


def extract_python_source(response: str) -> str:
    if not isinstance(response, str):
        raise CodingEvaluationError("model response must be text")
    match = _FENCE_RE.search(response)
    source = (match.group(1) if match else response).strip()
    if not source or len(source) > MAX_TEXT_CHARS or "\x00" in source:
        raise CodingEvaluationError("model returned empty or oversized Python source")
    if response.count("```") not in {0, 2}:
        raise CodingEvaluationError("model response contains ambiguous code fences")
    return source + "\n"


def find_runtime() -> str:
    for name in ("docker", "podman"):
        executable = shutil.which(name)
        if executable:
            return executable
    raise SandboxUnavailable("Python coding evaluation requires Docker or Podman")


def ensure_runtime_available(runtime: str) -> None:
    try:
        result = subprocess.run(
            [runtime, "info"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SandboxUnavailable("Docker or Podman is installed but unavailable") from error
    if result.returncode != 0:
        raise SandboxUnavailable("Docker or Podman is installed but its service is not running")


def ensure_image_available(runtime: str, image: str) -> None:
    try:
        result = subprocess.run(
            [runtime, "image", "inspect", image],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SandboxUnavailable("could not inspect the pinned coding sandbox image") from error
    if result.returncode != 0:
        raise SandboxUnavailable(
            f"pinned coding sandbox image is not installed; run `{runtime} pull {image}` explicitly"
        )


def sandbox_argv(runtime: str, workspace: Path, pack: CodingPack, cidfile: Path) -> list[str]:
    mount = f"type=bind,src={workspace.resolve()},dst=/workspace,readonly"
    return [
        runtime,
        "run",
        "--rm",
        "--pull",
        "never",
        "--cidfile",
        str(cidfile),
        "--network",
        "none",
        "--read-only",
        "--pids-limit",
        str(pack.pids),
        "--user",
        "65534:65534",
        "--memory",
        f"{pack.memory_mb}m",
        "--cpus",
        f"{pack.cpus:g}",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--ulimit",
        "nofile=64:64",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=64m",
        "--mount",
        mount,
        "--workdir",
        "/tmp",
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
        "--env",
        "PYTHONHASHSEED=0",
        pack.image,
        "python",
        "-I",
        "-B",
        "/workspace/test_solution.py",
    ]


def _kill_container(runtime: str, cidfile: Path) -> None:
    try:
        container_id = cidfile.read_text(encoding="utf-8").strip()
    except OSError:
        return
    if not re.fullmatch(r"[0-9a-f]{12,64}", container_id):
        return
    subprocess.run(
        [runtime, "kill", container_id],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
        check=False,
    )


def run_in_sandbox(source: str, task: CodingTask, pack: CodingPack, *, runtime: str | None = None) -> SandboxResult:
    runtime = runtime or find_runtime()
    source = extract_python_source(source)
    with tempfile.TemporaryDirectory(prefix="omm-coding-") as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        workspace.mkdir()
        solution_path = workspace / "solution.py"
        tests_path = workspace / "test_solution.py"
        solution_path.write_text(source, encoding="utf-8")
        tests_path.write_text(task.tests, encoding="utf-8")
        solution_path.chmod(0o444)
        tests_path.chmod(0o444)
        workspace.chmod(0o555)
        cidfile = root / ".container-id"
        command = sandbox_argv(runtime, workspace, pack, cidfile)
        started = time.monotonic()
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        output = bytearray()
        overflow = threading.Event()

        def read_output() -> None:
            assert process.stdout is not None
            while chunk := process.stdout.read(4096):
                remaining = MAX_OUTPUT_BYTES - len(output)
                if remaining > 0:
                    output.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    overflow.set()
                    break

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        deadline = started + pack.timeout_seconds
        outcome = "completed"
        while process.poll() is None:
            if overflow.is_set():
                outcome = "output_limit"
                _kill_container(runtime, cidfile)
                process.terminate()
                break
            if time.monotonic() >= deadline:
                outcome = "timeout"
                _kill_container(runtime, cidfile)
                process.terminate()
                break
            time.sleep(0.05)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _kill_container(runtime, cidfile)
            process.kill()
            process.wait(timeout=5)
        reader.join(timeout=2)
        duration = time.monotonic() - started
        text = bytes(output).decode("utf-8", errors="replace")
        if outcome == "completed" and process.returncode != 0:
            outcome = "failed_tests"
        passed = task.test_count if outcome == "completed" and process.returncode == 0 else 0
        return SandboxResult(outcome, passed, task.test_count, duration, text)


def task_prompt(task: CodingTask) -> str:
    return (
        "You are editing one Python file named solution.py.\n"
        f"{task.prompt}\n\n"
        "Current solution.py:\n```python\n"
        f"{task.starter.rstrip()}\n```\n\n"
        "Return only the complete final contents of solution.py in one Python code fence. "
        "Do not include tests, commands, or an explanation."
    )


def evaluate_pack(
    model: str,
    pack: CodingPack,
    generate,
    *,
    runtime: str | None = None,
    model_provider: str | None = None,
    model_repo_id: str | None = None,
    model_filename: str | None = None,
    model_digest: str | None = None,
    quantization: str | None = None,
    engine: str = "ollama",
    engine_version: str | None = None,
) -> CodingEvaluationReport:
    runtime = runtime or find_runtime()
    results = []
    for task in pack.tasks:
        started = time.monotonic()
        try:
            response = generate(task_prompt(task))
            sandbox = run_in_sandbox(response, task, pack, runtime=runtime)
            results.append(
                CodingTaskResult(
                    task.id,
                    task.kind,
                    sandbox.outcome,
                    sandbox.tests_passed,
                    sandbox.tests_total,
                    sandbox.duration_seconds,
                )
            )
        except CodingEvaluationError as error:
            outcome = "sandbox_unavailable" if isinstance(error, SandboxUnavailable) else "invalid_response"
            results.append(CodingTaskResult(task.id, task.kind, outcome, 0, task.test_count, time.monotonic() - started))
    return CodingEvaluationReport(
        1,
        datetime.now(timezone.utc).isoformat(),
        model,
        model_provider,
        model_repo_id,
        model_filename,
        model_digest,
        quantization,
        engine,
        engine_version,
        pack.pack_id,
        pack.version,
        pack.sha256,
        tuple(results),
    )
