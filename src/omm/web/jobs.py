"""Serialized owned workers, durable outcomes, and idempotent requests."""
from __future__ import annotations

import hashlib
import copy
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid

from filelock import FileLock, Timeout

from omm import config
from omm.atomic import atomic_write_text


class JobConflict(ValueError):
    pass


def worker_argv():
    if getattr(sys, "frozen", False):
        return [sys.executable, "_web-worker"]
    return [sys.executable, "-m", "omm.web.worker"]


class JobManager:
    def __init__(self):
        self.root = config.OMM_HOME / "web-jobs"
        self.lock = threading.RLock()
        self.jobs = {}
        self.processes = {}
        self.workers = {}
        self.closed = False
        self.operation_guard = None
        if self.root.is_symlink():
            raise ValueError("Refusing a symlinked job directory")
        self.root.mkdir(parents=True, exist_ok=True)
        self.owner = FileLock(self.root / "server.lock")
        try:
            self.owner.acquire(timeout=0)
        except Timeout:
            raise JobConflict("A local web manager is already running for this OMM home.") from None
        for path in sorted(self.root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:100]:
            try:
                if path.is_symlink() or path.stat().st_size > 65536:
                    continue
                value = json.loads(path.read_text(encoding="utf-8"))
                if str(uuid.UUID(value["id"])) != path.stem:
                    continue
                if value["status"] in {"queued", "running", "cancelling"}:
                    value.update(status="interrupted", message="Server stopped before the result was confirmed. Check the model list before retrying.")
                self.jobs[value["id"]] = value
            except (OSError, ValueError, KeyError, TypeError):
                continue

    def _save(self, job):
        atomic_write_text(self.root / (job["id"] + ".json"), json.dumps(job, ensure_ascii=False) + "\n")

    def start(self, request: dict, request_id: str) -> dict:
        try:
            request_id = str(uuid.UUID(request_id))
        except (ValueError, TypeError, AttributeError):
            raise ValueError("request_id must be a UUID") from None
        digest = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        with self.lock:
            if self.closed:
                raise JobConflict("The server is shutting down")
            existing = self.jobs.get(request_id)
            if existing:
                if existing.get("request_digest") != digest:
                    raise JobConflict("This request ID already belongs to another operation")
                return dict(existing)
            if self.operation_guard is not None:
                self.operation_guard()
            if any(j["status"] in {"queued", "running", "cancelling"} for j in self.jobs.values()):
                raise JobConflict("Another model operation is still running")
            if len(self.jobs) >= 100:
                oldest = min(self.jobs.values(), key=lambda j: j["created_at"])
                del self.jobs[oldest["id"]]
                (self.root / (oldest["id"] + ".json")).unlink(missing_ok=True)
            job = {"id": request_id, "request_digest": digest, "operation": request["operation"],
                   "filename": request["filename"], "status": "queued", "created_at": time.time(),
                   "bytes_received": 0, "total_bytes": None, "message": "", "result": None}
            if request["operation"] == "start_chat":
                job["task_request"] = dict(request)
            self.jobs[request_id] = job
            self._save(job)
            worker = threading.Thread(target=self._run, args=(request_id, request), daemon=True)
            self.workers[request_id] = worker
            worker.start()
            return dict(job)

    def _run(self, jid, request):
        timer = None
        try:
            kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
            with self.lock:
                job = self.jobs[jid]
                if self.closed or job["status"] != "queued":
                    return
                proc = subprocess.Popen(worker_argv(),
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        env={**os.environ, "OMM_HOME": str(config.OMM_HOME), "PYTHONIOENCODING": "utf-8"},
                                        **kwargs)
                self.processes[jid] = proc
                job["status"] = "running"
                self._save(job)
            if request["operation"] == "document_import":
                timer = threading.Timer(60, proc.kill)
                timer.daemon = True
                timer.start()
            proc.stdin.write(json.dumps(request).encode())
            proc.stdin.close()
            last_saved = 0.0
            for raw in iter(lambda: proc.stdout.readline(65537), b""):
                if len(raw) > 65536:
                    continue
                try:
                    event = json.loads(raw)
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(event, dict):
                    continue
                with self.lock:
                    job = self.jobs[jid]
                    kind = event.get("event")
                    if kind == "progress":
                        if job["operation"] == "start_chat":
                            from omm.web.task_flow import STAGES
                            stage, status = event.get("stage"), event.get("step_status")
                            if stage in STAGES and status in {"running", "completed"}:
                                job["stage"] = stage
                                job.setdefault("steps", {})[stage] = status
                        for field in ("bytes_received", "total_bytes", "completed_count", "total_count"):
                            value = event.get(field)
                            if field in event and (value is None or (isinstance(value, int) and not isinstance(value, bool) and value >= 0)):
                                job[field] = value
                    elif kind in {"completed", "failed", "cancelled"}:
                        job.update(status=kind, result=event.get("result"), message=str(event.get("message", ""))[:2000])
                    now = time.monotonic()
                    if kind != "progress" or "stage" in event or now - last_saved >= 0.5:
                        self._save(job)
                        last_saved = now
            code = proc.wait()
            with self.lock:
                job = self.jobs[jid]
                if job["status"] in {"running", "cancelling"}:
                    job.update(status="failed", message=f"Worker exited before confirming the result (exit {code}). Check the model list before retrying.")
                elif code != 0 and job["status"] == "completed":
                    job.update(status="failed", message="Worker exited unsuccessfully after reporting a result; check the stored model.")
                self._save(job)
        except Exception as error:
            with self.lock:
                self.jobs[jid].update(status="failed", message=str(error)[:2000])
                self._save(self.jobs[jid])
        finally:
            if timer is not None:
                timer.cancel()
            with self.lock:
                self.processes.pop(jid, None)
                self.workers.pop(jid, None)

    def list(self):
        with self.lock:
            return copy.deepcopy(sorted(self.jobs.values(), key=lambda j: j["created_at"], reverse=True))

    def retry_task(self, jid, request_id):
        from omm import hub
        with self.lock:
            job = self.jobs.get(jid)
            if not job or job["operation"] != "start_chat" or job["status"] not in {"failed", "cancelled", "interrupted"}:
                raise JobConflict("중단되거나 실패한 대화 준비 작업만 다시 시작할 수 있어요.")
            request = job.get("task_request")
            if (not isinstance(request, dict) or set(request) - {"operation", "filename", "engine", "ref", "selection_id"}
                    or request.get("operation") != "start_chat" or request.get("engine") not in {"ollama", "lmstudio"}
                    or ("ref" in request and (not isinstance(request["ref"], str) or not 1 <= len(request["ref"]) <= 2048))):
                raise ValueError("저장된 작업을 확인하지 못했어요. 모델 목록에서 다시 선택해 주세요.")
            hub.validate_model_filename(request.get("filename"))
            digest = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
            if digest != job.get("request_digest"):
                raise ValueError("저장된 작업 내용이 달라졌어요. 모델 목록에서 다시 선택해 주세요.")
            return self.start(dict(request), request_id)

    def cancel(self, jid):
        with self.lock:
            job = self.jobs.get(jid)
            if not job:
                raise ValueError("Unknown job")
            if job["operation"] not in {"install", "compare", "start_chat"} or job["status"] not in {"queued", "running", "cancelling"}:
                raise JobConflict("Only a running installation, comparison or chat preparation can be cancelled")
            proc = self.processes.get(jid)
            if proc:
                job["status"] = "cancelling"
                proc.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
            else:
                job["status"] = "cancelled"
            self._save(job)
            return dict(job)

    def close(self):
        with self.lock:
            if self.closed and not self.owner.is_locked:
                return
            self.closed = True
            for job in self.jobs.values():
                if job["status"] == "queued":
                    job.update(status="interrupted", message="Server stopped before the operation started.")
                    self._save(job)
            ids = list(self.processes)
            workers = list(self.workers.values())
        for jid in ids:
            try:
                if self.jobs[jid]["operation"] in {"install", "compare", "start_chat"}:
                    self.cancel(jid)
            except (ValueError, OSError):
                pass
        for jid in ids:
            with self.lock:
                proc = self.processes.get(jid)
            if proc:
                try:
                    proc.wait(timeout=300 if self.jobs[jid]["operation"] == "start_chat" else 240 if self.jobs[jid]["operation"] in {"verify", "compare", "profile_save", "document_question"} else 5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        # Process exit can precede the reader's final durable JSON replacement.
        # Keep exclusive ownership until those writes finish, particularly on
        # Windows where a simultaneous replacement can make a read fail.
        for worker in workers:
            worker.join(timeout=5)
            if worker.is_alive():
                raise JobConflict("Worker output is still being saved; retry shutdown.")
        self.owner.release()
