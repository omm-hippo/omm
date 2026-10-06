"""Durable text conversations and one explicitly owned local model load."""
from __future__ import annotations

import copy
from dataclasses import replace
import hashlib
import json
import math
import threading
import time
import uuid

from omm import cli, config, hardware, hub, install_state, registry, runtime_profiles, tuning
from omm.atomic import atomic_write_text
from omm.engines.base import LoadOptions, find_runtime_model
from omm.hashutil import sha256_file
from omm.web.chat_stream import ChatCancelled, stream_reply
from omm.web.jobs import JobConflict
from omm.web.probe import adapter_for

ACTIVE = {"starting", "ready", "generating", "cancelling", "closing", "release_failed"}


def valid_uuid(value):
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("요청 ID가 올바르지 않아요.") from None


class ChatManager:
    def __init__(self, jobs):
        self.jobs = jobs
        self.root = config.OMM_HOME / "web-chats"
        if self.root.is_symlink():
            raise ValueError("대화 기록 폴더의 연결 경로를 확인해 주세요.")
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.records = {}
        self.active = None
        self.adapter = self.receipt = self.worker = self.response = None
        self.stop = threading.Event()
        self.release_finished = threading.Event()
        self.release_finished.set()
        self.closing = self.releasing = self.closed = False
        self.fingerprint = None
        for path in sorted(self.root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
            try:
                if path.is_symlink() or path.stat().st_size > 1024 * 1024:
                    continue
                record = json.loads(path.read_text(encoding="utf-8"))
                if valid_uuid(record["id"]) != path.stem or record.get("engine") not in {"ollama", "lmstudio"}:
                    continue
                if not isinstance(record.get("turns"), list) or len(record["turns"]) > 40:
                    continue
                hub.validate_model_filename(record["filename"])
                if record.get("status") not in ACTIVE | {"closed", "failed", "interrupted"}:
                    continue
                timestamp = record.get("updated_at")
                if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp):
                    continue
                for turn in record["turns"]:
                    valid_uuid(turn["id"])
                    if not isinstance(turn.get("user"), str) or len(turn["user"]) > 8192 or not isinstance(turn.get("assistant"), str) or len(turn["assistant"]) > 16384:
                        raise ValueError("Invalid stored turn")
                    if turn.get("status") not in {"generating", "completed", "cancelled", "failed", "interrupted"}:
                        raise ValueError("Invalid stored status")
                if record["status"] in ACTIVE:
                    record["status"] = "interrupted"
                    record["notice"] = "서버가 재시작됐어요. 기록을 확인한 뒤 대화를 다시 연결하세요."
                    for turn in record["turns"]:
                        if turn.get("status") == "generating":
                            turn["status"] = "interrupted"
                    self._save(record)
                self.records[record["id"]] = record
            except (OSError, ValueError, TypeError, KeyError, hub.ModelResolutionError):
                continue

    def _save(self, record):
        path = self.root / (record["id"] + ".json")
        if path.is_symlink() or self.root.is_symlink():
            raise ValueError("대화 기록 경로가 바뀌었어요.")
        atomic_write_text(path, json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")

    def guard_jobs(self):
        with self.lock:
            if self.active:
                raise JobConflict("채팅용 모델을 먼저 종료한 뒤 관리 작업을 진행하세요.")

    def list(self):
        with self.lock:
            return [{k: v for k, v in x.items() if k != "turns"} for x in sorted(self.records.values(), key=lambda x: x["updated_at"], reverse=True)]

    def get(self, key):
        with self.lock:
            record = self.records.get(valid_uuid(key))
            if record is None:
                raise ValueError("대화 기록을 찾지 못했어요.")
            return copy.deepcopy(record)

    def start(self, request, request_id, chat_id=None):
        request_id = valid_uuid(request_id)
        key = valid_uuid(chat_id) if chat_id is not None else request_id
        with self.jobs.lock, self.lock:
            if self.closed or self.jobs.closed:
                raise JobConflict("서버가 종료 중이에요.")
            existing = self.records.get(key)
            if existing and existing.get("open_request_id") == request_id:
                if existing["filename"] != request["filename"] or existing["engine"] != request["engine"]:
                    raise JobConflict("이 요청 ID는 다른 모델에 사용됐어요.")
                return copy.deepcopy(existing)
            if self.active or any(j["status"] in {"queued", "running", "cancelling"} for j in self.jobs.jobs.values()):
                raise JobConflict("현재 채팅이나 관리 작업을 마친 뒤 시작하세요.")
            if chat_id is not None and not existing:
                raise ValueError("다시 연결할 대화 기록을 찾지 못했어요.")
            if existing and (existing["filename"] != request["filename"] or existing["engine"] != request["engine"]):
                raise ValueError("기록과 같은 모델·실행 앱을 선택하세요.")
            if not existing and len(self.records) >= 20:
                raise JobConflict("저장된 대화가 20개예요. 필요 없는 대화를 삭제한 뒤 시작하세요.")
            record = {**(existing or {}), "id": key, "filename": request["filename"], "engine": request["engine"],
                      "model_id": hashlib.sha256(request["filename"].encode()).hexdigest(),
                      "open_request_id": request_id, "status": "starting", "notice": "모델을 준비하고 있어요.",
                      "updated_at": time.time(), "turns": (existing or {}).get("turns", [])}
            self.records[key] = record
            self.active = key
            self.stop = threading.Event()
            self.release_finished.clear()
            self.closing = False
            try:
                self._save(record)
            except Exception:
                self.active = None
                self.release_finished.set()
                if existing:
                    self.records[key] = existing
                else:
                    self.records.pop(key, None)
                raise
            self.worker = threading.Thread(target=self._load, args=(key,), daemon=True)
            self.worker.start()
            return copy.deepcopy(record)

    def _load(self, key):
        receipt = None
        adapter = None
        try:
            record = self.get(key)
            filename, engine = record["filename"], record["engine"]
            with install_state.cleanup_guard(filename):
                path = cli._managed_model_path(filename)
                digest = sha256_file(path)
                if record.get("sha256") and record["sha256"] != digest:
                    raise ValueError("모델 파일이 변경됐어요. 새 대화로 시작해 주세요.")
                entry = registry.load_registry().get(filename)
                if not entry or not entry.get("linked", {}).get(engine):
                    raise ValueError("모델 연결 상태가 바뀌었어요. 내 모델에서 확인하세요.")
                adapter = adapter_for(engine)
                if not adapter.health().reachable:
                    raise ValueError("실행 앱의 로컬 서버를 먼저 켜 주세요.")
                reference = cli._compatibility_model_ref(filename, entry, engine)
                models = adapter.list_models()
                visible = find_runtime_model(models, reference)
                if visible is None:
                    raise ValueError("실행 앱에서 모델을 찾지 못했어요. 연결을 확인하세요.")
                options = LoadOptions()
                if not visible.loaded:
                    if any(model.loaded for model in models):
                        raise ValueError("다른 모델이 사용 중이에요. 기존 작업을 유지하고 새 로딩을 중단했어요.")
                    saved = runtime_profiles.saved_options_for_file(filename, engine, path)
                    if saved:
                        options = saved
                    else:
                        metadata = runtime_profiles._metadata(path)
                        candidate = {**entry, "filename": filename, "size_bytes": path.stat().st_size,
                                     "context_length": metadata.get(f"{metadata.get('general.architecture')}.context_length")}
                        profile = tuning.recommend_runtime_settings(hardware.scan_hardware(), candidate)
                        options = runtime_profiles.proposed_options(profile, path, engine)
                    runtime_profiles.ensure_memory(path, options, hardware.scan_hardware())
                receipt = adapter.load(reference, options)
                stat = path.stat()
                with self.lock:
                    self.adapter, self.receipt = adapter, receipt
                    self.fingerprint = (stat.st_size, stat.st_mtime_ns, stat.st_ino)
                    record = self.records[key]
                    record.update(sha256=digest, loaded_by_omm=receipt.loaded_by_omm,
                                  context_length=receipt.load_options.context_length if receipt.load_options else options.context_length,
                                  updated_at=time.time())
                    self._save(record)
            with self.lock:
                if not self.closing:
                    self.records[key].update(status="ready", notice="")
                    self._save(self.records[key])
        except Exception as error:
            with self.lock:
                self.adapter, self.receipt = adapter, receipt
                self.records[key].update(status="failed", notice=str(error)[:500], updated_at=time.time())
                if receipt is None:
                    self.active = None
                    self.release_finished.set()
                try:
                    self._save(self.records[key])
                except (OSError, ValueError):
                    pass
        finally:
            if self.closing or (receipt is not None and self.get(key)["status"] == "failed"):
                self._release(key)

    def send(self, key, text, request_id):
        key, request_id = valid_uuid(key), valid_uuid(request_id)
        if not isinstance(text, str) or not text.strip() or len(text) > 8192:
            raise ValueError("메시지는 1–8192자까지 입력할 수 있어요.")
        with self.lock:
            record = self.records.get(key)
            if record is None:
                raise ValueError("대화 기록을 찾지 못했어요.")
            duplicate = next((turn for turn in record["turns"] if turn["id"] == request_id), None)
            if duplicate:
                if duplicate["user"] != text:
                    raise JobConflict("이 요청 ID는 다른 메시지에 사용됐어요.")
                return copy.deepcopy(record)
            if self.active != key or record["status"] != "ready" or self.closing:
                raise JobConflict("모델이 준비되고 이전 응답이 끝난 뒤 전송하세요.")
            if len(record["turns"]) >= 40:
                raise ValueError("이 대화의 메시지 한도에 도달했어요. 새 대화를 시작하세요.")
            if sum(len((x["user"] + x["assistant"]).encode("utf-8")) for x in record["turns"]) + len(text.encode("utf-8")) > 384 * 1024:
                raise ValueError("대화 기록 크기 한도에 가까워졌어요. 새 대화를 시작하세요.")
            context = [{"role": "system", "content": "You are a helpful assistant. Reply in the user's language."}]
            for turn in record["turns"]:
                if turn["status"] == "completed":
                    context.extend(({"role": "user", "content": turn["user"]}, {"role": "assistant", "content": turn["assistant"]}))
            context.append({"role": "user", "content": text})
            # Conservative character guard; do not silently truncate saved history.
            if sum(len(x["content"]) for x in context) > min(32000, record["context_length"]):
                raise ValueError("대화가 현재 문맥 길이에 가까워졌어요. 새 대화로 시작해 주세요.")
            turn = {"id": request_id, "user": text, "assistant": "", "status": "generating", "created_at": time.time()}
            record["turns"].append(turn)
            record.update(status="generating", updated_at=time.time(), notice="")
            self.stop.clear()
            try:
                self._save(record)
            except Exception:
                record["turns"].pop()
                record["status"] = "ready"
                raise
            self.worker = threading.Thread(target=self._generate, args=(key, turn, context), daemon=True)
            self.worker.start()
            return copy.deepcopy(record)

    def _connection(self, response):
        with self.lock:
            self.response = response
            stopped = self.stop.is_set()
        if response is not None and stopped:
            response.close()

    def _generate(self, key, turn, context):
        last_saved = 0.0
        with self.lock:
            used_bytes = sum(len((x["user"] + x["assistant"]).encode("utf-8")) for x in self.records[key]["turns"])
        def chunk(text):
            nonlocal last_saved, used_bytes
            with self.lock:
                if self.stop.is_set():
                    raise ChatCancelled()
                if len(turn["assistant"]) + len(text) > 16384:
                    raise ValueError("응답 크기 제한에 도달했어요.")
                used_bytes += len(text.encode("utf-8"))
                if used_bytes > 512 * 1024:
                    raise ValueError("대화 기록 크기 제한에 도달했어요.")
                turn["assistant"] += text
                self.records[key]["updated_at"] = time.time()
                if time.monotonic() - last_saved >= 0.5:
                    self._save(self.records[key])
                    last_saved = time.monotonic()
        try:
            record = self.get(key)
            with install_state.cleanup_guard(record["filename"]):
                stat = cli._managed_model_path(record["filename"]).stat()
                if (stat.st_size, stat.st_mtime_ns, stat.st_ino) != self.fingerprint:
                    raise ValueError("모델 파일이 변경됐어요. 모델을 종료하고 새 대화로 시작하세요.")
                visible = find_runtime_model(self.adapter.list_models(), self.receipt.model)
                if visible is None or not visible.loaded:
                    self.closing = True
                    raise ValueError("실행 앱의 모델 로딩이 종료됐어요. 대화를 다시 연결하세요.")
                changed_instance = visible.instance_id and visible.instance_id != self.receipt.instance_id
                actual_context = getattr(self.adapter, "_resident_contexts", {}).get(visible.key.removesuffix(":latest"))
                changed_context = actual_context is not None and actual_context != record["context_length"]
                if changed_instance or changed_context:
                    self.receipt = replace(self.receipt, loaded_by_omm=False)
                    self.closing = True
                    raise ValueError("실행 앱의 로딩 상태가 다른 작업에서 바뀌었어요. 기존 모델을 유지하고 연결을 종료했어요.")
                finish = stream_reply(self.adapter, self.receipt, context, self.stop, chunk, self._connection)
            if not turn["assistant"].strip():
                raise ValueError("실행 앱이 응답 내용을 반환하지 않았어요.")
            turn.update(status="completed", finish_reason=finish)
        except ChatCancelled:
            turn["status"] = "cancelled"
        except Exception as error:
            turn.update(status="cancelled" if self.stop.is_set() else "failed", error="" if self.stop.is_set() else str(error)[:500])
        finally:
            with self.lock:
                record = self.records[key]
                record.update(status="closing" if self.closing else "ready", updated_at=time.time())
                try:
                    self._save(record)
                except (OSError, ValueError):
                    turn.update(status="failed", error="대화 기록을 저장하지 못했어요. 대화 JSON을 저장한 뒤 저장 공간을 확인하세요.")
                    record["notice"] = turn["error"]
            if self.closing:
                self._release(key)

    def cancel(self, key):
        key = valid_uuid(key)
        with self.lock:
            record = self.records.get(key)
            if self.active != key or record is None:
                raise JobConflict("진행 중인 대화가 없어요.")
            if record["status"] not in {"generating", "cancelling"}:
                return copy.deepcopy(record)
            self.stop.set()
            record["status"] = "cancelling"
            response = self.response
            self._save(record)
        if response is not None:
            threading.Thread(target=response.close, daemon=True).start()
        return self.get(key)

    def end(self, key):
        key = valid_uuid(key)
        with self.lock:
            if key not in self.records:
                raise ValueError("대화 기록을 찾지 못했어요.")
            if self.active != key:
                return self.get(key)
            self.closing = True
            self.stop.set()
            self.records[key]["status"] = "closing"
            self._save(self.records[key])
            worker, response = self.worker, self.response
        if response is not None:
            threading.Thread(target=response.close, daemon=True).start()
        def release_after_work():
            if worker is not None:
                worker.join(180)
                if worker.is_alive():
                    return
            self._release(key)
        threading.Thread(target=release_after_work, daemon=True).start()
        return self.get(key)

    def _release(self, key):
        with self.lock:
            if self.active != key or self.releasing:
                return
            self.releasing = True
            adapter, receipt = self.adapter, self.receipt
        released = True
        try:
            if receipt is not None and receipt.loaded_by_omm:
                released = adapter.unload(receipt).unloaded
        except Exception:
            released = False
        with self.lock:
            record = self.records[key]
            record.update(status="closed" if released else "release_failed", updated_at=time.time(),
                          notice="" if released else "모델 메모리 해제를 확인하지 못했어요. 실행 앱에서 상태를 확인하세요.",
                          released_owned_load=bool(released and receipt is not None and receipt.loaded_by_omm))
            if released:
                self.active = self.adapter = self.receipt = None
            self.releasing = False
            try:
                self._save(record)
            finally:
                self.release_finished.set()

    def delete(self, key):
        key = valid_uuid(key)
        with self.lock:
            if self.active == key:
                raise JobConflict("모델을 종료한 뒤 대화 기록을 삭제하세요.")
            if key not in self.records:
                raise ValueError("대화 기록을 찾지 못했어요.")
            path = self.root / (key + ".json")
            if path.is_symlink():
                raise ValueError("대화 기록 경로를 확인해 주세요.")
            path.unlink()
            del self.records[key]
        return {"deleted": True}

    def close(self):
        with self.lock:
            self.closed = True
            key, worker = self.active, self.worker
        if key:
            self.end(key)
            if worker:
                worker.join(180)
            self._release(key)
            self.release_finished.wait(180)
