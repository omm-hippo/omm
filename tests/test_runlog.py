import json
import logging
from pathlib import Path

from omm import config, runlog


def _jsonl_files(home: Path) -> list[Path]:
    return sorted((home / "logs").glob("*.jsonl"))


def _only_jsonl_text(home: Path) -> str:
    return next(iter(_jsonl_files(home))).read_text()


def test_start_finish_writes_wellformed_jsonl(isolated_omm_home):
    runlog.start(["install", "some-model"])
    logging.getLogger("omm.linker").info(
        "linked", extra={"event": "link", "engine": "ollama", "method": "symlink"}
    )
    runlog.finish(0, "ok")

    files = _jsonl_files(config.OMM_HOME)
    assert len(files) == 1
    assert "_install.jsonl" in files[0].name

    lines = files[0].read_text().splitlines()
    records = [json.loads(line) for line in lines]  # every line is valid JSON
    assert records[0]["event"] == "run_start"
    assert records[-1]["event"] == "run_end"
    assert records[-1]["exit_code"] == 0
    assert records[-1]["outcome"] == "ok"
    assert any(r.get("event") == "link" and r.get("method") == "symlink" for r in records)
    assert runlog._HANDLER is None
    assert not any(
        getattr(h, "_omm_runlog", False) for h in logging.getLogger("omm").handlers
    )


def test_debug_gating(isolated_omm_home, monkeypatch):
    monkeypatch.delenv("OMM_DEBUG", raising=False)
    runlog.start(["list"])
    logging.getLogger("omm.x").debug("noisy", extra={"event": "probe"})
    logging.getLogger("omm.x").info("kept", extra={"event": "kept"})
    runlog.finish(0, "ok")
    text = _only_jsonl_text(config.OMM_HOME)
    assert "probe" not in text
    assert "kept" in text


def test_debug_env_lets_debug_through(isolated_omm_home, monkeypatch):
    monkeypatch.setenv("OMM_DEBUG", "1")
    runlog.start(["list"])
    logging.getLogger("omm.x").debug("noisy", extra={"event": "probe"})
    runlog.finish(0, "ok")
    text = _only_jsonl_text(config.OMM_HOME)
    assert "probe" in text


def test_argv_and_url_scrubbing(isolated_omm_home):
    runlog.start(["search", "secret query terms"])
    logging.getLogger("omm.d").info(
        "download",
        extra={
            "event": "download",
            "url": runlog.scrub_url(
                "https://user:pw@hf.co/repo/model.gguf?token=SECRET"
            ),
        },
    )
    runlog.finish(0, "ok")
    text = _only_jsonl_text(config.OMM_HOME)
    assert "secret query terms" not in text
    assert "SECRET" not in text
    assert "pw@" not in text
    assert "hf.co/repo/model.gguf" in text


def test_logging_failure_is_swallowed(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(
        runlog, "_logs_dir", lambda: (_ for _ in ()).throw(OSError("boom"))
    )
    runlog.start(["list"])  # must not raise
    runlog.finish(1, "failed")  # must not raise
    assert runlog._HANDLER is None


def test_history_block_appended_on_finish(isolated_omm_home):
    runlog.start(["install", "m"])
    logging.getLogger("omm.linker").info(
        "linked", extra={"event": "link", "engine": "ollama", "method": "symlink"}
    )
    runlog.finish(0, "ok")
    history = (config.OMM_HOME / "logs" / "history.log").read_text()
    assert "omm install" in history
    assert "ok" in history
    assert ".jsonl" in history


def test_rebuild_history_orders_by_ts(isolated_omm_home):
    for name in ("list", "search"):
        runlog.start([name])
        runlog.finish(0, "ok")
    (config.OMM_HOME / "logs" / "history.log").unlink()
    count = runlog.rebuild_history()
    assert count == 2
    rebuilt = (config.OMM_HOME / "logs" / "history.log").read_text()
    assert rebuilt.index("omm list") < rebuilt.index("omm search")


def test_read_history_grep_and_lines(isolated_omm_home):
    for name in ("list", "search", "install"):
        runlog.start([name])
        runlog.finish(0, "ok")
    assert "omm search" in runlog.read_history(grep="search")
    assert "omm list" not in runlog.read_history(grep="search")
