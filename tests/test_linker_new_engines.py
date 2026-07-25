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


def test_link_ollama_at_custom_models_dir_does_not_touch_default(tmp_path, monkeypatch):
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


def test_link_custom_directory_reused_for_mstystudio(tmp_path, monkeypatch):
    gguf_path = tmp_path / "model.gguf"
    gguf_path.write_bytes(b"x")
    models_dir = tmp_path / "MstyStudio" / "models"
    monkeypatch.setattr(linker, "mstystudio_models_dir", lambda: models_dir)

    warning = linker.link_engine("mstystudio", gguf_path, repo_id=None, ollama_tag="model")

    assert warning is None
    assert (models_dir / "model.gguf").is_symlink()
    assert (models_dir / "model.gguf").resolve() == gguf_path.resolve()


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
