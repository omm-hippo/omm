"""One noninteractive operation per owned subprocess; never executes a shell."""
from __future__ import annotations

import json
import signal
import sys
import threading


_OUTPUT_LOCK = threading.Lock()


def emit(event: str, **fields):
    with _OUTPUT_LOCK:
        print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


def execute(request: dict, stop: threading.Event) -> dict:
    import click
    from omm import cli, config, downloader, hub, install_state, linker, registry

    options = cli.GlobalOptions(yes=True, quiet=True, no_color=True)
    with click.Context(click.Command("web-worker"), obj=options):
        operation = request["operation"]
        if operation in {"compare", "profile_save", "profile_restore"}:
            from omm.web import performance
            if operation == "compare":
                return performance.compare(request["filenames"], request["engine"], stop,
                    lambda done, total: emit("progress", completed_count=done, total_count=total))
            return performance.profile_action(request["filename"], request["engine"], restore=operation == "profile_restore")
        if operation == "engine_install":
            from omm import onboarding
            key = request["engine"]
            if key not in {spec.key for spec in linker.ENGINES}:
                raise ValueError("Unknown local runner")
            if not linker.is_engine_installed(key) and not onboarding.install_selected_engines(cli.console, [key]):
                raise ValueError("실행 앱을 설치하지 못했어요. 연결 상태 화면에서 다시 확인해 주세요.")
            if not linker.is_engine_installed(key):
                raise ValueError("실행 앱 설치 결과를 확인하지 못했어요.")
            return {"engine": key, "installed": True}
        if operation in {"import_scan", "import", "cleanup"}:
            from omm.web import maintenance, management
            if operation == "import_scan":
                return maintenance.scan()
            if operation == "import":
                return maintenance.adopt(request["group"])
            return management.cleanup_selected(request["files"])
        filename = hub.validate_model_filename(request["filename"])
        if operation == "install":
            resolved = hub.resolve_model(request["ref"])
            if resolved.filename != filename:
                raise ValueError("Resolved package differs from the selected model")
            cli._install_plan_for(resolved)

            class ProgressBudget(downloader.DownloadBudget):
                def record(self, count):
                    super().record(count)
                    emit("progress", bytes_received=self.used, total_bytes=resolved.expected_size_bytes)

            download_state = {}
            try:
                with downloader.download_budget_scope(ProgressBudget()):
                    outcome = cli._install_impl(
                        resolved, no_upload=True, skip_unfit=True, assume_yes=True,
                        runtime_load_consent=False, verify_runtime_after_install=False,
                        benchmark_after_install=False, enforce_memory_guard=True,
                        link_only_engine=request.get("engine"), stop_event=stop,
                        downloaded_state=download_state,
                    )
                if outcome.skipped_unfit or outcome.skipped_low_disk:
                    raise ValueError("Installation was skipped because of memory or disk constraints")
                entry = registry.load_registry().get(filename)
                if not entry or not (config.MODELS_DIR / filename).is_file():
                    raise ValueError("The installed file and registry could not be confirmed")
                return {"filename": filename, "linked": outcome.linked, "sha256": outcome.sha256,
                        "runtime_verified": False, "uploaded": False}
            except (cli.InstallInterrupted, downloader.DownloadCancelled, KeyboardInterrupt):
                if not install_state.preserve_interrupted_file(filename):
                    cli._cleanup_interrupted_install(filename, downloaded_now=download_state.get("downloaded_now", False))
                raise
        entry = registry.load_registry().get(filename)
        if not isinstance(entry, dict):
            raise ValueError("The model is no longer managed by OMM")
        if operation == "verify":
            from omm.web.probe import check
            if request.get("engine") not in {"ollama", "lmstudio"} or not entry.get("linked", {}).get(request["engine"]):
                raise ValueError("The model is not linked to the selected runtime")
            return check(filename, entry, request["engine"])
        if operation == "uninstall":
            if not cli._remove_one(filename, entry):
                raise ValueError("Some files or runner links could not be removed; retry after closing the runner")
            return {"filename": filename, "removed": True}
        if operation == "link":
            dest = cli._managed_model_path(filename)
            if not dest.is_file():
                raise ValueError("The managed model file is missing")
            linked = cli._link_model(dest, entry.get("repo_id"), entry.get("ollama_name") or linker.sanitize_ollama_tag(filename), only_engine=request.get("engine"))
            registry.upsert_entry(filename, linked=linked)
            if not any(linked.values()):
                raise ValueError("No runner link was created. Check whether the selected runner is installed and its model directory is writable.")
            return {"filename": filename, "linked": linked}
        raise ValueError("Unsupported operation")


def main():
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, lambda *_: stop.set())
    try:
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise ValueError("Operation input is too large")
        request = json.loads(raw)
        emit("started")
        result = execute(request, stop)
        if request.get("operation") == "verify" and not result["runtime_verified"]:
            emit("failed", result=result, message=result["message"])
            return 1
        emit("completed", result=result)
    except BaseException as error:
        if stop.is_set() or isinstance(error, KeyboardInterrupt):
            emit("cancelled", message="작업을 중단했어요. 파일과 실행 앱 상태를 확인한 뒤 다시 진행해 주세요.")
            return 2
        emit("failed", message=str(error)[:2000] or type(error).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
