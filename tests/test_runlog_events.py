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


def test_scan_import_adoption_logged(isolated_omm_home, tmp_path):
    from omm import scan_import

    payload = b"identical gguf bytes"
    ollama_path = tmp_path / "ollama-blob"
    ollama_path.write_bytes(payload)
    lmstudio_dir = tmp_path / "lmstudio" / "org" / "repo"
    lmstudio_dir.mkdir(parents=True)
    lmstudio_path = lmstudio_dir / "model.gguf"
    lmstudio_path.write_bytes(payload)
    group = scan_import.ModelGroup(
        sha256=scan_import.sha256_file(ollama_path),
        locations=[
            scan_import.ExternalGguf(
                "ollama", "llama3:latest", ollama_path, len(payload), "deadbeef"
            ),
            scan_import.ExternalGguf(
                "lmstudio", "model.gguf", lmstudio_path, len(payload), "deadbeef"
            ),
        ],
    )

    runlog.start(["scan"])
    scan_import.adopt_group(group)
    runlog.finish(0, "ok")

    assert "adopt" in _events(config.OMM_HOME)


class _FakeResp:
    ok = True
    status_code = 200
    headers: dict = {}

    def json(self):
        return {"ok": True}


class _FakeSession:
    trust_env = True

    def request(self, *a, **k):
        return _FakeResp()


def test_http_detail_only_with_debug(isolated_omm_home, monkeypatch):
    from omm.engines import base

    def call_once():
        client = base.LoopbackJsonClient("http://127.0.0.1:11434")
        client._session = _FakeSession()
        client.request("GET", "/api/tags")

    monkeypatch.delenv("OMM_DEBUG", raising=False)
    runlog.start(["verify"])
    call_once()
    runlog.finish(0, "ok")
    assert "http" not in _events(config.OMM_HOME)

    monkeypatch.setenv("OMM_DEBUG", "1")
    runlog.start(["verify"])
    call_once()
    runlog.finish(0, "ok")
    # newest jsonl is the debug run
    newest = sorted((config.OMM_HOME / "logs").glob("*.jsonl"))[-1]
    events = [json.loads(line).get("event") for line in newest.read_text().splitlines()]
    assert "http" in events
