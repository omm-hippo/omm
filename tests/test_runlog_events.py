import json
from pathlib import Path

from omm import config, registry, runlog


def _records(home: Path):
    f = next(iter((home / "logs").glob("*.jsonl")))
    return [json.loads(line) for line in f.read_text().splitlines()]


def _events(home: Path):
    return [r.get("event") for r in _records(home)]


def test_registry_upsert_and_remove_logged(isolated_omm_home):
    runlog.start(["install"])
    registry.upsert_entry("model-a.gguf", repo_id="org/model-a")
    registry.remove_entry("model-a.gguf")
    runlog.finish(0, "ok")
    events = _events(config.OMM_HOME)
    assert "registry-upsert" in events
    assert "registry-remove" in events


def test_link_method_logged(isolated_omm_home, tmp_path):
    from omm import linker

    src = tmp_path / "central" / "m.gguf"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"gguf")
    dst = tmp_path / "engine" / "m.gguf"

    runlog.start(["install"])
    linker.link_file(src, dst)
    runlog.finish(0, "ok")

    link_events = [r for r in _records(config.OMM_HOME) if r.get("event") == "link"]
    assert link_events and link_events[0]["method"] in ("symlink", "hardlink", "copy")


def test_download_start_complete_logged(isolated_omm_home, tmp_path, monkeypatch):
    from omm import downloader

    dest = tmp_path / "m.gguf"

    def fake_impl(url, d, stop_check=None, **_kw):
        d.write_bytes(b"payload")

    monkeypatch.setattr(downloader, "_download_file_impl", fake_impl)

    runlog.start(["install"])
    downloader.download_file("https://hf.co/org/repo/m.gguf?token=SECRET", dest)
    runlog.finish(0, "ok")

    records = _records(config.OMM_HOME)
    events = [r.get("event") for r in records]
    assert "download-start" in events
    assert "download-complete" in events
    text = json.dumps(records)
    assert "SECRET" not in text  # url scrubbed


def test_download_failure_logged(isolated_omm_home, tmp_path, monkeypatch):
    from omm import downloader

    def boom(url, d, stop_check=None, **_kw):
        raise downloader.DownloadError("nope")

    monkeypatch.setattr(downloader, "_download_file_impl", boom)

    runlog.start(["install"])
    try:
        downloader.download_file("https://hf.co/x/y.gguf", tmp_path / "y.gguf")
    except downloader.DownloadError:
        pass
    runlog.finish(1, "failed")

    assert "download-failed" in _events(config.OMM_HOME)
