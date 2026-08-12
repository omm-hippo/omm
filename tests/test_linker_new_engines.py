from pathlib import Path

import pytest

from omm import linker


# --- Jan (model.yml manifest) -----------------------------------------


def test_link_jan_writes_model_yaml_with_absolute_path(tmp_path, monkeypatch):
    gguf_path = tmp_path / "model.gguf"
    gguf_path.write_bytes(b"fake-gguf-bytes")
    monkeypatch.setattr(linker, "jan_models_dir", lambda: tmp_path / "jan-models")

    config_path = linker.link_jan(gguf_path, "tinyllama-q4")

    assert config_path == tmp_path / "jan-models" / "tinyllama-q4" / "model.yml"
    text = config_path.read_text()
    assert f'model_path: "{gguf_path}"' in text
    assert 'name: "tinyllama-q4"' in text
    assert f"size_bytes: {len(b'fake-gguf-bytes')}" in text


def test_read_jan_model_path_extracts_field(tmp_path):
    config_path = tmp_path / "model.yml"
    config_path.write_text('model_path: "/some/path/model.gguf"\nname: "x"\nsize_bytes: 5\n')

    assert linker.read_jan_model_path(config_path) == "/some/path/model.gguf"


def test_read_jan_model_path_returns_none_when_missing(tmp_path):
    config_path = tmp_path / "model.yml"
    config_path.write_text('name: "x"\n')

    assert linker.read_jan_model_path(config_path) is None


def test_unlink_jan_removes_manifest_and_empty_dir(tmp_path, monkeypatch):
    gguf_path = tmp_path / "model.gguf"
    gguf_path.write_bytes(b"x")
    monkeypatch.setattr(linker, "jan_models_dir", lambda: tmp_path / "jan-models")
    config_path = linker.link_jan(gguf_path, "tinyllama-q4")
    assert config_path.exists()

    linker.unlink_jan("tinyllama-q4")

    assert not config_path.exists()
    assert not config_path.parent.exists()


def test_autoremove_jan_deletes_manifests_pointing_at_missing_files(tmp_path, monkeypatch):
    models_dir = tmp_path / "jan-models"
    monkeypatch.setattr(linker, "jan_models_dir", lambda: models_dir)

    still_here = tmp_path / "still-here.gguf"
    still_here.write_bytes(b"x")
    linker.link_jan(still_here, "kept")

    gone = tmp_path / "gone.gguf"
    gone.write_bytes(b"x")
    linker.link_jan(gone, "broken")
    gone.unlink()

    removed = linker.autoremove_jan()

    assert removed == 1
    assert (models_dir / "kept" / "model.yml").exists()
    assert not (models_dir / "broken").exists()


def test_autoremove_jan_returns_zero_when_dir_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(linker, "jan_models_dir", lambda: tmp_path / "nope")

    assert linker.autoremove_jan() == 0


# --- AnythingLLM (Ollama-format at its own models_dir) ------------------


def test_link_ollama_at_custom_models_dir_does_not_touch_default(isolated_omm_home, tmp_path, monkeypatch):
    """AnythingLLM reuses link_ollama() pointed at its own models_dir - it
    must not fall back to the real ~/.ollama when one is passed."""
    gguf_path = tmp_path / "model.gguf"
    gguf_path.write_bytes(b"x")
    monkeypatch.setattr(linker, "read_gguf_metadata", lambda path, keys: {"general.architecture": "llama"})
    default_dir = tmp_path / "should-not-be-touched"
    monkeypatch.setattr(linker, "ollama_models_dir", lambda: default_dir)
    custom_dir = tmp_path / "anythingllm-ollama"

    linker.link_ollama(gguf_path, "mymodel", models_dir=custom_dir)

    assert not default_dir.exists()
    assert (custom_dir / "blobs").exists()
    manifest_path = custom_dir / "manifests" / "registry.ollama.ai" / "library" / "mymodel" / "latest"
    assert manifest_path.exists()


def test_is_anythingllm_installed_reflects_app_dir_existence_on_non_darwin(tmp_path, monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "Linux")
    monkeypatch.setattr(linker, "anythingllm_app_dir", lambda: tmp_path / "anythingllm-desktop")
    assert linker.is_anythingllm_installed() is False

    (tmp_path / "anythingllm-desktop").mkdir()
    assert linker.is_anythingllm_installed() is True


def test_is_anythingllm_installed_reflects_app_bundle_on_darwin(tmp_path, monkeypatch):
    """A leftover data dir (e.g. after dragging the app to Trash) must not
    count as "installed" on macOS - only the actual .app bundle does."""
    monkeypatch.setattr(linker.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(linker, "_APP_BUNDLE_SEARCH_ROOTS", [tmp_path / "Applications"])
    monkeypatch.setattr(linker, "anythingllm_app_dir", lambda: tmp_path / "anythingllm-desktop")
    (tmp_path / "anythingllm-desktop").mkdir()  # leftover data dir, no bundle
    assert linker.is_anythingllm_installed() is False

    (tmp_path / "Applications").mkdir()
    (tmp_path / "Applications" / "AnythingLLM.app").mkdir()
    assert linker.is_anythingllm_installed() is True


# --- Msty (flat symlink dir) --------------------------------------------


def test_is_mstystudio_installed_reflects_app_dir_existence_on_non_darwin(tmp_path, monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "Linux")
    monkeypatch.setattr(linker, "mstystudio_app_dir", lambda: tmp_path / "MstyStudio")
    assert linker.is_mstystudio_installed() is False

    (tmp_path / "MstyStudio").mkdir()
    assert linker.is_mstystudio_installed() is True


def test_is_mstystudio_installed_reflects_app_bundle_on_darwin(tmp_path, monkeypatch):
    monkeypatch.setattr(linker.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(linker, "_APP_BUNDLE_SEARCH_ROOTS", [tmp_path / "Applications"])
    monkeypatch.setattr(linker, "mstystudio_app_dir", lambda: tmp_path / "MstyStudio")
    (tmp_path / "MstyStudio").mkdir()  # leftover data dir, no bundle
    assert linker.is_mstystudio_installed() is False

    (tmp_path / "Applications").mkdir()
    (tmp_path / "Applications" / "MstyStudio.app").mkdir()
    assert linker.is_mstystudio_installed() is True


def test_link_custom_directory_reused_for_mstystudio(isolated_omm_home, tmp_path, monkeypatch):
    gguf_path = tmp_path / "model.gguf"
    gguf_path.write_bytes(b"x")
    models_dir = tmp_path / "MstyStudio" / "models"
    monkeypatch.setattr(linker, "mstystudio_models_dir", lambda: models_dir)

    warning = linker.link_engine("mstystudio", gguf_path, repo_id=None, ollama_tag="model")

    assert warning is None
    destination = models_dir / "model.gguf"
    assert destination.is_symlink() or destination.samefile(gguf_path)
    assert destination.samefile(gguf_path)


# --- KoboldCpp / text-generation-webui heuristic discovery --------------


@pytest.fixture(autouse=True)
def _clear_heuristic_caches():
    linker.find_koboldcpp_binary.cache_clear()
    linker.find_textgenwebui_root.cache_clear()
    yield
    linker.find_koboldcpp_binary.cache_clear()
    linker.find_textgenwebui_root.cache_clear()


def test_find_koboldcpp_binary_finds_top_level_file(tmp_path, monkeypatch):
    monkeypatch.setattr(linker, "_HEURISTIC_SEARCH_ROOTS", [tmp_path])
    binary = tmp_path / "koboldcpp-mac-arm64"
    binary.write_bytes(b"x")

    assert linker.find_koboldcpp_binary() == binary
    assert linker.is_koboldcpp_installed() is True


def test_find_koboldcpp_binary_finds_file_inside_named_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(linker, "_HEURISTIC_SEARCH_ROOTS", [tmp_path])
    subdir = tmp_path / "KoboldCpp"
    subdir.mkdir()
    binary = subdir / "koboldcpp.exe"
    binary.write_bytes(b"x")

    assert linker.find_koboldcpp_binary() == binary


def test_find_koboldcpp_binary_returns_none_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(linker, "_HEURISTIC_SEARCH_ROOTS", [tmp_path])

    assert linker.find_koboldcpp_binary() is None
    assert linker.is_koboldcpp_installed() is False
    assert linker.koboldcpp_models_dir() is None


def test_koboldcpp_models_dir_is_sibling_of_binary(tmp_path, monkeypatch):
    monkeypatch.setattr(linker, "_HEURISTIC_SEARCH_ROOTS", [tmp_path])
    binary = tmp_path / "koboldcpp-mac-arm64"
    binary.write_bytes(b"x")

    assert linker.koboldcpp_models_dir() == tmp_path / "models"


def test_find_textgenwebui_root_requires_name_hint_and_marker_files(tmp_path, monkeypatch):
    monkeypatch.setattr(linker, "_HEURISTIC_SEARCH_ROOTS", [tmp_path])

    # Wrong name, has marker files - should not match.
    wrong_name = tmp_path / "some-other-project"
    wrong_name.mkdir()
    (wrong_name / "server.py").touch()
    (wrong_name / "one_click.py").touch()
    assert linker.find_textgenwebui_root() is None
    linker.find_textgenwebui_root.cache_clear()

    # Right name, missing a marker file - should not match.
    incomplete = tmp_path / "text-generation-webui-old"
    incomplete.mkdir()
    (incomplete / "server.py").touch()
    assert linker.find_textgenwebui_root() is None
    linker.find_textgenwebui_root.cache_clear()

    # Right name, both marker files present - matches.
    real_root = tmp_path / "text-generation-webui"
    real_root.mkdir()
    (real_root / "server.py").touch()
    (real_root / "one_click.py").touch()
    assert linker.find_textgenwebui_root() == real_root
    linker.find_textgenwebui_root.cache_clear()
    assert linker.is_textgenwebui_installed() is True
    assert linker.textgenwebui_models_dir() == real_root / "user_data" / "models"


def test_link_engine_raises_link_error_when_textgenwebui_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr(linker, "_HEURISTIC_SEARCH_ROOTS", [tmp_path])
    gguf_path = tmp_path / "model.gguf"
    gguf_path.write_bytes(b"x")

    with pytest.raises(linker.LinkError):
        linker.link_engine("textgenwebui", gguf_path, repo_id=None, ollama_tag="model")


def test_find_textgenwebui_root_recognizes_portable_build_layout(tmp_path, monkeypatch):
    """Portable releases extract to a `textgen-<version>` folder with
    `app/server.py` (not root-level server.py+one_click.py like the old
    git-clone layout) - verified directly against a real release archive."""
    monkeypatch.setattr(linker, "_HEURISTIC_SEARCH_ROOTS", [tmp_path])
    portable_root = tmp_path / "textgen-4.9"
    (portable_root / "app").mkdir(parents=True)
    (portable_root / "app" / "server.py").touch()
    (portable_root / "user_data" / "models").mkdir(parents=True)

    assert linker.find_textgenwebui_root() == portable_root
    assert linker.textgenwebui_models_dir() == portable_root / "user_data" / "models"


def test_find_textgenwebui_root_still_recognizes_old_git_clone_layout(tmp_path, monkeypatch):
    """Regression guard: existing users with the old git-clone install
    (root-level server.py + one_click.py) must not stop being detected."""
    monkeypatch.setattr(linker, "_HEURISTIC_SEARCH_ROOTS", [tmp_path])
    old_root = tmp_path / "text-generation-webui"
    old_root.mkdir()
    (old_root / "server.py").touch()
    (old_root / "one_click.py").touch()

    assert linker.find_textgenwebui_root() == old_root


def test_find_textgenwebui_root_portable_layout_requires_name_hint(tmp_path, monkeypatch):
    monkeypatch.setattr(linker, "_HEURISTIC_SEARCH_ROOTS", [tmp_path])
    wrong_name = tmp_path / "some-other-4.9"
    (wrong_name / "app").mkdir(parents=True)
    (wrong_name / "app" / "server.py").touch()

    assert linker.find_textgenwebui_root() is None


# --- Engine dispatch table ------------------------------------------------


def test_is_engine_installed_reflects_monkeypatched_per_engine_check(monkeypatch):
    """is_engine_installed() must call each is_X_installed() by name (not
    from a dict of function references captured at import time), so that
    monkeypatching e.g. linker.is_jan_installed actually takes effect."""
    monkeypatch.setattr(linker, "is_jan_installed", lambda: True)

    assert linker.is_engine_installed("jan") is True


def test_engines_table_has_no_duplicate_keys():
    keys = [spec.key for spec in linker.ENGINES]
    assert len(keys) == len(set(keys))


def test_link_jan_raises_link_error_when_write_fails(tmp_path, monkeypatch):
    gguf_path = tmp_path / "model.gguf"
    gguf_path.write_bytes(b"weights")
    monkeypatch.setattr(linker, "jan_models_dir", lambda: tmp_path / "jan")
    monkeypatch.setattr(
        Path, "write_text", lambda self, content: (_ for _ in ()).throw(OSError("disk full"))
    )

    with pytest.raises(linker.LinkError):
        linker.link_jan(gguf_path, "model-id")


def test_unlink_ollama_swallows_permission_error(isolated_omm_home, tmp_path, monkeypatch):
    models_dir = tmp_path / "ollama"
    monkeypatch.setattr(linker, "read_gguf_metadata", lambda *_: {"general.architecture": "llama"})
    source = tmp_path / "source.gguf"
    source.write_bytes(b"weights")
    linker.link_ollama(source, "model", models_dir=models_dir)

    monkeypatch.setattr(Path, "unlink", lambda self, missing_ok=False: (_ for _ in ()).throw(OSError("permission denied")))

    linker.unlink_ollama("model", models_dir=models_dir)  # must not raise


def test_unlink_jan_swallows_permission_error(tmp_path, monkeypatch):
    monkeypatch.setattr(linker, "jan_models_dir", lambda: tmp_path / "jan")
    gguf_path = tmp_path / "model.gguf"
    gguf_path.write_bytes(b"weights")
    linker.link_jan(gguf_path, "model-id")

    monkeypatch.setattr(Path, "unlink", lambda self, missing_ok=False: (_ for _ in ()).throw(OSError("permission denied")))

    linker.unlink_jan("model-id")  # must not raise


def test_autoremove_jan_skips_manifest_it_cannot_unlink(tmp_path, monkeypatch):
    models_dir = tmp_path / "jan"
    model_dir = models_dir / "model-id"
    model_dir.mkdir(parents=True)
    config_path = model_dir / "model.yml"
    config_path.write_text('model_path: "/gone/model.gguf"\n')
    monkeypatch.setattr(linker, "jan_models_dir", lambda: models_dir)
    monkeypatch.setattr(Path, "unlink", lambda self, missing_ok=False: (_ for _ in ()).throw(OSError("permission denied")))

    removed = linker.autoremove_jan()  # must not raise

    assert removed == 0
    assert config_path.exists()


# --- LM Studio load-verification helpers -------------------------------


def test_lms_cli_path_prefers_which(monkeypatch):
    monkeypatch.setattr(linker.shutil, "which", lambda name: "/usr/local/bin/lms" if name == "lms" else None)
    assert linker._lms_cli_path() == "/usr/local/bin/lms"


def test_lms_cli_path_falls_back_to_bootstrap_location(tmp_path, monkeypatch):
    monkeypatch.setattr(linker.shutil, "which", lambda name: None)
    monkeypatch.setattr(linker, "lmstudio_home_dir", lambda: tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    lms_file = bin_dir / "lms"
    lms_file.write_text("#!/bin/sh\n")
    assert linker._lms_cli_path() == str(lms_file)


def test_lms_cli_path_returns_none_when_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr(linker.shutil, "which", lambda name: None)
    monkeypatch.setattr(linker, "lmstudio_home_dir", lambda: tmp_path)
    assert linker._lms_cli_path() is None


class _FakeResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_lmstudio_server_status_parses_running_json(monkeypatch):
    monkeypatch.setattr(
        linker.subprocess, "run",
        lambda cmd, **kw: _FakeResult(stdout='{"running": true, "port": 1234}'),
    )
    assert linker._lmstudio_server_status("lms") == {"running": True, "port": 1234}


def test_lmstudio_server_status_returns_none_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(linker.subprocess, "run", lambda cmd, **kw: _FakeResult(returncode=1))
    assert linker._lmstudio_server_status("lms") is None


def test_lmstudio_server_status_returns_none_on_bad_json(monkeypatch):
    monkeypatch.setattr(linker.subprocess, "run", lambda cmd, **kw: _FakeResult(stdout="not json"))
    assert linker._lmstudio_server_status("lms") is None


def test_lmstudio_server_status_returns_none_on_timeout(monkeypatch):
    def _raise(cmd, **kw):
        raise linker.subprocess.TimeoutExpired(cmd, kw.get("timeout", 5))

    monkeypatch.setattr(linker.subprocess, "run", _raise)
    assert linker._lmstudio_server_status("lms") is None


_LMS_LS_JSON = (
    '[{"type": "llm", "modelKey": "tinyllama-1.1b-chat-v1.0", '
    '"path": "local/tinyllama-1.1b-chat-v1.0.Q4_K_M/tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf"}, '
    '{"type": "embedding", "modelKey": "nomic-embed-text-v1.5", '
    '"path": "nomic-ai/nomic-embed-text-v1.5-GGUF/nomic-embed-text-v1.5.Q4_K_M.gguf"}]'
)


def test_lmstudio_list_models_parses_json_array(monkeypatch):
    monkeypatch.setattr(linker.subprocess, "run", lambda cmd, **kw: _FakeResult(stdout=_LMS_LS_JSON))
    models = linker._lmstudio_list_models("lms")
    assert models is not None
    assert models[0]["modelKey"] == "tinyllama-1.1b-chat-v1.0"


def test_lmstudio_list_models_returns_none_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(linker.subprocess, "run", lambda cmd, **kw: _FakeResult(returncode=1))
    assert linker._lmstudio_list_models("lms") is None


def test_lmstudio_list_models_returns_none_on_bad_json(monkeypatch):
    monkeypatch.setattr(linker.subprocess, "run", lambda cmd, **kw: _FakeResult(stdout="not json"))
    assert linker._lmstudio_list_models("lms") is None


def test_lmstudio_model_key_resolves_by_matching_path(monkeypatch):
    monkeypatch.setattr(linker, "_lmstudio_list_models", lambda lms_path: [
        {"modelKey": "tinyllama-1.1b-chat-v1.0",
         "path": "local/tinyllama-1.1b-chat-v1.0.Q4_K_M/tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf"},
    ])
    key = linker._lmstudio_model_key(
        "lms", "local", "tinyllama-1.1b-chat-v1.0.Q4_K_M", "tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf"
    )
    assert key == "tinyllama-1.1b-chat-v1.0"


def test_lmstudio_model_key_none_when_path_not_found(monkeypatch):
    monkeypatch.setattr(linker, "_lmstudio_list_models", lambda lms_path: [
        {"modelKey": "other-model", "path": "local/other/other.gguf"},
    ])
    assert linker._lmstudio_model_key("lms", "local", "repo", "file.gguf") is None


def test_lmstudio_model_key_none_when_list_unavailable(monkeypatch):
    monkeypatch.setattr(linker, "_lmstudio_list_models", lambda lms_path: None)
    assert linker._lmstudio_model_key("lms", "local", "repo", "file.gguf") is None


def test_start_lmstudio_server_returns_true_once_status_reports_running(monkeypatch):
    calls = {"n": 0}

    def fake_run(cmd, **kw):
        return _FakeResult()

    def fake_status(lms_path, timeout=5):
        calls["n"] += 1
        return {"running": calls["n"] >= 2, "port": 1234}

    monkeypatch.setattr(linker.subprocess, "run", fake_run)
    monkeypatch.setattr(linker, "_lmstudio_server_status", fake_status)
    monkeypatch.setattr(linker.time, "sleep", lambda seconds: None)
    assert linker._start_lmstudio_server("lms", timeout=5) is True


def test_start_lmstudio_server_returns_false_on_timeout(monkeypatch):
    monkeypatch.setattr(linker.subprocess, "run", lambda cmd, **kw: _FakeResult())
    monkeypatch.setattr(linker, "_lmstudio_server_status", lambda lms_path, timeout=5: {"running": False, "port": 1234})
    monkeypatch.setattr(linker.time, "sleep", lambda seconds: None)
    assert linker._start_lmstudio_server("lms", timeout=2) is False


def test_start_lmstudio_server_returns_false_when_start_command_fails(monkeypatch):
    def _raise(cmd, **kw):
        raise OSError("lms not executable")

    monkeypatch.setattr(linker.subprocess, "run", _raise)
    assert linker._start_lmstudio_server("lms", timeout=5) is False


def test_stop_lmstudio_server_swallows_failures(monkeypatch):
    def _raise(cmd, **kw):
        raise OSError("already gone")

    monkeypatch.setattr(linker.subprocess, "run", _raise)
    linker._stop_lmstudio_server("lms")  # must not raise


class _FakeHTTPResponse:
    def __init__(self, ok=True, status_code=200, payload=None):
        self.ok = ok
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


def test_probe_lmstudio_generate_true_on_real_text(monkeypatch):
    import requests

    payload = {"choices": [{"message": {"content": "OK"}}]}
    monkeypatch.setattr(requests, "post", lambda url, json, timeout: _FakeHTTPResponse(payload=payload))
    assert linker._probe_lmstudio_generate(1234, "tinyllama-test") is True


def test_probe_lmstudio_generate_false_on_empty_content(monkeypatch):
    import requests

    payload = {"choices": [{"message": {"content": "   "}}]}
    monkeypatch.setattr(requests, "post", lambda url, json, timeout: _FakeHTTPResponse(payload=payload))
    assert linker._probe_lmstudio_generate(1234, "tinyllama-test") is False


def test_probe_lmstudio_generate_false_on_http_error(monkeypatch):
    import requests

    monkeypatch.setattr(requests, "post", lambda url, json, timeout: _FakeHTTPResponse(ok=False, status_code=500))
    assert linker._probe_lmstudio_generate(1234, "tinyllama-test") is False


def test_probe_lmstudio_generate_false_on_malformed_json(monkeypatch):
    import requests

    class _BadJSON(_FakeHTTPResponse):
        def json(self):
            raise ValueError("not json")

    monkeypatch.setattr(requests, "post", lambda url, json, timeout: _BadJSON())
    assert linker._probe_lmstudio_generate(1234, "tinyllama-test") is False


def test_probe_lmstudio_generate_none_on_connection_error(monkeypatch):
    import requests

    def _raise(url, json, timeout):
        raise requests.exceptions.ConnectionError("refused")

    monkeypatch.setattr(requests, "post", _raise)
    assert linker._probe_lmstudio_generate(1234, "tinyllama-test") is None


def test_lms_unload_swallows_failures(monkeypatch):
    def _raise(cmd, **kw):
        raise OSError("model not found")

    monkeypatch.setattr(linker.subprocess, "run", _raise)
    linker._lms_unload("lms", "tinyllama-test")  # must not raise


def test_verify_lmstudio_load_none_when_lms_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(linker, "_lms_cli_path", lambda: None)
    called = {"status": False}
    monkeypatch.setattr(linker, "_lmstudio_server_status", lambda *a, **k: called.__setitem__("status", True))
    assert linker.verify_lmstudio_load(tmp_path / "model.gguf", None) is None
    assert called["status"] is False


def test_verify_lmstudio_load_none_when_server_unreachable(tmp_path, monkeypatch):
    monkeypatch.setattr(linker, "_lms_cli_path", lambda: "lms")
    monkeypatch.setattr(linker, "_lmstudio_server_status", lambda lms_path: None)
    assert linker.verify_lmstudio_load(tmp_path / "model.gguf", None) is None


def test_verify_lmstudio_load_none_when_model_key_not_resolved(tmp_path, monkeypatch):
    monkeypatch.setattr(linker, "_lms_cli_path", lambda: "lms")
    monkeypatch.setattr(linker, "_lmstudio_server_status", lambda lms_path: {"running": True, "port": 1234})
    monkeypatch.setattr(linker, "_lmstudio_model_key", lambda *a, **k: None)
    probed = {"called": False}
    monkeypatch.setattr(linker, "_probe_lmstudio_generate", lambda *a, **k: probed.__setitem__("called", True))
    assert linker.verify_lmstudio_load(tmp_path / "model.gguf", None) is None
    assert probed["called"] is False


def test_verify_lmstudio_load_leaves_already_running_server_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(linker, "_lms_cli_path", lambda: "lms")
    monkeypatch.setattr(linker, "_lmstudio_server_status", lambda lms_path: {"running": True, "port": 1234})
    monkeypatch.setattr(linker, "_lmstudio_model_key", lambda *a, **k: "tinyllama-1.1b-chat-v1.0")
    started = {"called": False}
    stopped = {"called": False}
    monkeypatch.setattr(linker, "_start_lmstudio_server", lambda *a, **k: started.__setitem__("called", True))
    monkeypatch.setattr(linker, "_stop_lmstudio_server", lambda *a, **k: stopped.__setitem__("called", True))
    monkeypatch.setattr(linker, "_probe_lmstudio_generate", lambda port, model_key, **k: True)
    monkeypatch.setattr(linker, "_lms_unload", lambda *a, **k: None)

    assert linker.verify_lmstudio_load(tmp_path / "model.gguf", None) is True
    assert started["called"] is False
    assert stopped["called"] is False


def test_verify_lmstudio_load_starts_and_stops_server_when_not_running(tmp_path, monkeypatch):
    monkeypatch.setattr(linker, "_lms_cli_path", lambda: "lms")
    monkeypatch.setattr(linker, "_lmstudio_server_status", lambda lms_path: {"running": False, "port": 1234})
    monkeypatch.setattr(linker, "_lmstudio_model_key", lambda *a, **k: "widget")
    monkeypatch.setattr(linker, "_start_lmstudio_server", lambda lms_path: True)
    stopped = {"called": False}
    monkeypatch.setattr(linker, "_stop_lmstudio_server", lambda lms_path: stopped.__setitem__("called", True))
    monkeypatch.setattr(linker, "_probe_lmstudio_generate", lambda port, model_key, **k: True)
    unloaded = {"model_key": None}
    monkeypatch.setattr(linker, "_lms_unload", lambda lms_path, model_key: unloaded.__setitem__("model_key", model_key))

    result = linker.verify_lmstudio_load(tmp_path / "model.gguf", "acme/widget")
    assert result is True
    assert stopped["called"] is True
    assert unloaded["model_key"] == "widget"


def test_verify_lmstudio_load_none_when_server_start_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(linker, "_lms_cli_path", lambda: "lms")
    monkeypatch.setattr(linker, "_lmstudio_server_status", lambda lms_path: {"running": False, "port": 1234})
    monkeypatch.setattr(linker, "_lmstudio_model_key", lambda *a, **k: "widget")
    monkeypatch.setattr(linker, "_start_lmstudio_server", lambda lms_path: False)
    probed = {"called": False}
    monkeypatch.setattr(linker, "_probe_lmstudio_generate", lambda *a, **k: probed.__setitem__("called", True))
    assert linker.verify_lmstudio_load(tmp_path / "model.gguf", None) is None
    assert probed["called"] is False


def test_verify_lmstudio_load_false_propagates_and_still_unloads(tmp_path, monkeypatch):
    monkeypatch.setattr(linker, "_lms_cli_path", lambda: "lms")
    monkeypatch.setattr(linker, "_lmstudio_server_status", lambda lms_path: {"running": True, "port": 1234})
    monkeypatch.setattr(linker, "_lmstudio_model_key", lambda *a, **k: "widget")
    monkeypatch.setattr(linker, "_probe_lmstudio_generate", lambda port, model_key, **k: False)
    unloaded = {"called": False}
    monkeypatch.setattr(linker, "_lms_unload", lambda *a, **k: unloaded.__setitem__("called", True))
    assert linker.verify_lmstudio_load(tmp_path / "model.gguf", None) is False
    assert unloaded["called"] is True
