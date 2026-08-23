import json
import platform
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
    assert f"model_path: {json.dumps(str(gguf_path))}" in text
    assert 'name: "tinyllama-q4"' in text
    assert f"size_bytes: {len(b'fake-gguf-bytes')}" in text


@pytest.mark.skipif(
    platform.system() == "Windows",
    reason='Windows filenames cannot contain the double quote character',
)
def test_link_jan_escapes_quotes_in_model_path(tmp_path, monkeypatch):
    gguf_path = tmp_path / 'model "quoted".gguf'
    gguf_path.write_bytes(b"x")
    monkeypatch.setattr(linker, "jan_models_dir", lambda: tmp_path / "jan-models")

    config_path = linker.link_jan(gguf_path, "quoted-model")

    assert f"model_path: {json.dumps(str(gguf_path))}" in config_path.read_text()
    assert linker.read_jan_model_path(config_path) == str(gguf_path)


def test_link_jan_rejects_symlinked_model_directory(
    isolated_omm_home, tmp_path, monkeypatch
):
    models_dir = tmp_path / "jan-models"
    outside = tmp_path / "outside"
    models_dir.mkdir()
    outside.mkdir()
    (models_dir / "safe").symlink_to(outside, target_is_directory=True)
    gguf_path = tmp_path / "model.gguf"
    gguf_path.write_bytes(b"x")
    monkeypatch.setattr(linker, "jan_models_dir", lambda: models_dir)

    with pytest.raises(linker.LinkError, match="symlinked model directory"):
        linker.link_jan(gguf_path, "safe")

    assert not (outside / "model.yml").exists()


def test_read_jan_model_path_extracts_field(tmp_path):
    config_path = tmp_path / "model.yml"
    config_path.write_text('model_path: "/some/path/model.gguf"\nname: "x"\nsize_bytes: 5\n', encoding="utf-8")

    assert linker.read_jan_model_path(config_path) == "/some/path/model.gguf"


def test_read_jan_model_path_returns_none_when_missing(tmp_path):
    config_path = tmp_path / "model.yml"
    config_path.write_text('name: "x"\n', encoding="utf-8")

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


def test_link_jan_refuses_to_overwrite_unowned_manifest(
    isolated_omm_home, tmp_path, monkeypatch
):
    gguf_path = tmp_path / "model.gguf"
    gguf_path.write_bytes(b"x")
    models_dir = tmp_path / "jan-models"
    monkeypatch.setattr(linker, "jan_models_dir", lambda: models_dir)
    config_path = models_dir / "tinyllama-q4" / "model.yml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text('model_path: "/user/model.gguf"\nname: "user"\n', encoding="utf-8")

    with pytest.raises(linker.LinkError, match="unowned Jan manifest"):
        linker.link_jan(gguf_path, "tinyllama-q4")

    assert config_path.read_text() == 'model_path: "/user/model.gguf"\nname: "user"\n'


def test_unlink_jan_preserves_unowned_manifest(isolated_omm_home, tmp_path, monkeypatch):
    models_dir = tmp_path / "jan-models"
    monkeypatch.setattr(linker, "jan_models_dir", lambda: models_dir)
    config_path = models_dir / "user-model" / "model.yml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text('model_path: "/missing/user.gguf"\n', encoding="utf-8")

    linker.unlink_jan("user-model")

    assert config_path.exists()


def test_unlink_jan_preserves_owned_manifest_modified_in_place(
    isolated_omm_home, tmp_path, monkeypatch
):
    gguf_path = tmp_path / "model.gguf"
    gguf_path.write_bytes(b"x")
    models_dir = tmp_path / "jan-models"
    monkeypatch.setattr(linker, "jan_models_dir", lambda: models_dir)
    config_path = linker.link_jan(gguf_path, "model")
    config_path.write_text(
        f"model_path: {json.dumps(str(gguf_path))}\nname: \"user-edited\"\n",
        encoding="utf-8",
    )

    linker.unlink_jan("model")

    assert config_path.exists()


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


def test_autoremove_jan_preserves_unowned_broken_manifest(
    isolated_omm_home, tmp_path, monkeypatch
):
    models_dir = tmp_path / "jan-models"
    monkeypatch.setattr(linker, "jan_models_dir", lambda: models_dir)
    config_path = models_dir / "user-model" / "model.yml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(f'model_path: "{tmp_path / "missing.gguf"}"\n', encoding="utf-8")

    assert linker.autoremove_jan() == 0
    assert config_path.exists()


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


@pytest.mark.parametrize(
    "unsafe_name",
    ["", ".", "..", "../escape", "/tmp/escape", "C:escape", "name:tag"],
)
def test_link_ollama_rejects_unsafe_model_name(
    isolated_omm_home, tmp_path, monkeypatch, unsafe_name
):
    gguf_path = tmp_path / "model.gguf"
    gguf_path.write_bytes(b"x")
    monkeypatch.setattr(
        linker,
        "read_gguf_metadata",
        lambda path, keys: {"general.architecture": "llama"},
    )
    models_dir = tmp_path / "ollama"

    with pytest.raises(linker.LinkError, match="Unsafe Ollama"):
        linker.link_ollama(gguf_path, unsafe_name, models_dir=models_dir)

    assert not (tmp_path / "escape").exists()


def test_ollama_manifest_collision_preserves_first_model(
    isolated_omm_home, tmp_path, monkeypatch
):
    first = linker.MODELS_DIR / "a" / "model.gguf"
    second = linker.MODELS_DIR / "a-model.gguf"
    first.parent.mkdir(parents=True)
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    monkeypatch.setattr(
        linker,
        "read_gguf_metadata",
        lambda path, keys: {"general.architecture": "llama"},
    )
    models_dir = tmp_path / "ollama"
    tag = linker.sanitize_ollama_tag(str(first.relative_to(linker.MODELS_DIR)))
    assert tag == linker.sanitize_ollama_tag(second.name)

    linker.link_ollama(first, tag, models_dir=models_dir)
    manifest_path = (
        models_dir
        / "manifests"
        / "registry.ollama.ai"
        / "library"
        / tag
        / "latest"
    )
    original = manifest_path.read_bytes()

    with pytest.raises(linker.LinkError, match="Refusing to replace"):
        linker.link_ollama(second, tag, models_dir=models_dir)
    linker.unlink_ollama(tag, models_dir=models_dir, expected_source=second)

    assert manifest_path.read_bytes() == original


def test_unlink_ollama_leaves_manifest_when_ownership_record_is_lost(
    isolated_omm_home, tmp_path, monkeypatch
):
    """A manifest whose ownership record is missing (link-ownership.json
    reset, or a pre-ownership-registry legacy link) must not be touched
    when the caller can't prove it's the same content omm is deleting -
    the safe default stays a no-op, matching prior behavior."""
    gguf_path = linker.MODELS_DIR / "model.gguf"
    gguf_path.parent.mkdir(parents=True, exist_ok=True)
    gguf_path.write_bytes(b"model-bytes")
    monkeypatch.setattr(
        linker, "read_gguf_metadata", lambda path, keys: {"general.architecture": "llama"}
    )
    models_dir = tmp_path / "ollama"
    tag = "model"
    linker.link_ollama(gguf_path, tag, models_dir=models_dir)
    manifest_path = models_dir / "manifests" / "registry.ollama.ai" / "library" / tag / "latest"
    assert manifest_path.exists()

    # Simulate a lost ownership record (registry reset, or a legacy link
    # that predates the ownership registry entirely).
    linker._update_link_ownership(manifest_path, None)

    removed = linker.unlink_ollama(tag, models_dir=models_dir)

    assert removed is False
    assert manifest_path.exists()


def test_unlink_ollama_recovers_orphan_via_matching_content_sha256(
    isolated_omm_home, tmp_path, monkeypatch
):
    """Same lost-ownership-record scenario, but the caller (omm's own
    registry entry) knows the exact sha256 of the file it's about to
    delete. If that matches the manifest's model-layer digest, this is
    unambiguously omm's own orphaned link and must be cleaned up - this
    is the exact bug from issue #171: `omm remove` deleted the hub file
    while the ownership-record-less Ollama manifest and its now-broken
    blob symlink lived on forever, permanently invisible to `omm list`
    but still showing up in `omm benchmark all`."""
    gguf_path = linker.MODELS_DIR / "model.gguf"
    gguf_path.parent.mkdir(parents=True, exist_ok=True)
    gguf_path.write_bytes(b"model-bytes")
    monkeypatch.setattr(
        linker, "read_gguf_metadata", lambda path, keys: {"general.architecture": "llama"}
    )
    models_dir = tmp_path / "ollama"
    tag = "model"
    linker.link_ollama(gguf_path, tag, models_dir=models_dir)
    manifest_path = models_dir / "manifests" / "registry.ollama.ai" / "library" / tag / "latest"
    content_sha256 = linker.sha256_file(gguf_path)
    linker._update_link_ownership(manifest_path, None)

    removed = linker.unlink_ollama(
        tag, models_dir=models_dir, expected_content_sha256=content_sha256
    )

    assert removed is True
    assert not manifest_path.exists()


def test_unlink_ollama_ignores_content_sha256_mismatch(isolated_omm_home, tmp_path, monkeypatch):
    """A wrong/unrelated sha256 must never be treated as proof of
    ownership - only an exact match recovers an unrecorded manifest."""
    gguf_path = linker.MODELS_DIR / "model.gguf"
    gguf_path.parent.mkdir(parents=True, exist_ok=True)
    gguf_path.write_bytes(b"model-bytes")
    monkeypatch.setattr(
        linker, "read_gguf_metadata", lambda path, keys: {"general.architecture": "llama"}
    )
    models_dir = tmp_path / "ollama"
    tag = "model"
    linker.link_ollama(gguf_path, tag, models_dir=models_dir)
    manifest_path = models_dir / "manifests" / "registry.ollama.ai" / "library" / tag / "latest"
    linker._update_link_ownership(manifest_path, None)

    removed = linker.unlink_ollama(
        tag, models_dir=models_dir, expected_content_sha256="0" * 64
    )

    assert removed is False
    assert manifest_path.exists()


def test_jan_manifest_collision_preserves_first_model(
    isolated_omm_home, tmp_path, monkeypatch
):
    first = linker.MODELS_DIR / "a" / "model.gguf"
    second = linker.MODELS_DIR / "a-model.gguf"
    first.parent.mkdir(parents=True)
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    models_dir = tmp_path / "jan-models"
    monkeypatch.setattr(linker, "jan_models_dir", lambda: models_dir)
    tag = linker.sanitize_ollama_tag(str(first.relative_to(linker.MODELS_DIR)))
    assert tag == linker.sanitize_ollama_tag(second.name)

    manifest_path = linker.link_jan(first, tag)
    original = manifest_path.read_bytes()

    with pytest.raises(linker.LinkError, match="Refusing to replace"):
        linker.link_jan(second, tag)
    linker.unlink_jan(tag, expected_source=second)

    assert manifest_path.read_bytes() == original


def test_link_ollama_rejects_corrupted_existing_digest_blob(
    isolated_omm_home, tmp_path, monkeypatch
):
    gguf_path = tmp_path / "model.gguf"
    gguf_path.write_bytes(b"expected")
    monkeypatch.setattr(
        linker,
        "read_gguf_metadata",
        lambda path, keys: {"general.architecture": "llama"},
    )
    models_dir = tmp_path / "ollama"
    digest = linker.sha256_file(gguf_path)
    blob = models_dir / "blobs" / f"sha256-{digest}"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"corrupted")

    with pytest.raises(linker.LinkError, match="does not match its digest"):
        linker.link_ollama(gguf_path, "model", models_dir=models_dir)


@pytest.fixture
def anythingllm_linux_env(tmp_path, monkeypatch):
    """Point every AnythingLLM Linux probe at tmp_path so the test never
    sees the real ~/AnythingLLMDesktop or /usr/share/applications."""
    monkeypatch.setattr(linker.platform, "system", lambda: "Linux")
    monkeypatch.setattr(linker, "anythingllm_app_dir", lambda: tmp_path / "anythingllm-desktop")
    monkeypatch.setattr(linker, "_ANYTHINGLLM_LINUX_INSTALL_DIRS", [tmp_path / "AnythingLLMDesktop"])
    monkeypatch.setattr(linker, "_DESKTOP_ENTRY_SEARCH_ROOTS", [tmp_path / "applications"])
    (tmp_path / "applications").mkdir()
    return tmp_path


@pytest.fixture
def anythingllm_windows_env(tmp_path, monkeypatch):
    """Point every AnythingLLM Windows probe at tmp_path. Unset the
    remaining install-location variables so a real AnythingLLM on the
    machine running the tests can't decide the outcome."""
    monkeypatch.setattr(linker.platform, "system", lambda: "Windows")
    monkeypatch.setattr(linker, "anythingllm_app_dir", lambda: tmp_path / "anythingllm-desktop")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    for variable in ("ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
        monkeypatch.delenv(variable, raising=False)
    return tmp_path


def _windows_start_menu(root: Path) -> Path:
    return root / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def test_is_anythingllm_installed_false_when_nothing_present_on_linux(anythingllm_linux_env):
    assert linker.is_anythingllm_installed() is False


def test_is_anythingllm_installed_reflects_app_dir_existence_on_non_darwin(anythingllm_linux_env):
    """The launched case: the Electron userData dir alone still counts."""
    (anythingllm_linux_env / "anythingllm-desktop").mkdir()
    assert linker.is_anythingllm_installed() is True


def test_is_anythingllm_installed_detects_never_launched_install_on_linux(anythingllm_linux_env):
    """installer.sh unpacks ~/AnythingLLMDesktop at install time; ~/.config
    /anythingllm-desktop only appears on first launch."""
    (anythingllm_linux_env / "AnythingLLMDesktop").mkdir()
    assert linker.is_anythingllm_installed() is True


def test_is_anythingllm_installed_detects_desktop_entry_on_linux(anythingllm_linux_env):
    (anythingllm_linux_env / "applications" / "anythingllm.desktop").write_text("[Desktop Entry]\n")
    assert linker.is_anythingllm_installed() is True


def test_is_anythingllm_installed_false_when_nothing_present_on_windows(anythingllm_windows_env):
    assert linker.is_anythingllm_installed() is False


def test_is_anythingllm_installed_detects_launched_app_on_windows(anythingllm_windows_env):
    (anythingllm_windows_env / "anythingllm-desktop").mkdir()
    assert linker.is_anythingllm_installed() is True


@pytest.mark.parametrize(
    "install_dir_name", ["AnythingLLM", "AnythingLLM Desktop", "anythingllm-desktop"]
)
def test_is_anythingllm_installed_detects_never_launched_install_on_windows(
    anythingllm_windows_env, install_dir_name
):
    """Regression for #132: electron-builder installs into
    %LOCALAPPDATA%\\Programs, but %APPDATA%\\anythingllm-desktop is only
    created the first time the app is launched - an installed app that was
    never opened used to read as "not installed"."""
    program_dir = anythingllm_windows_env / "Local" / "Programs" / install_dir_name
    program_dir.mkdir(parents=True)
    (program_dir / "AnythingLLM.exe").write_bytes(b"")
    assert linker.is_anythingllm_installed() is True


def test_is_anythingllm_installed_ignores_empty_program_dir_on_windows(anythingllm_windows_env):
    """A full disk makes the NSIS installer exit after creating only the
    target folder (seen live); that husk must not read as installed."""
    (anythingllm_windows_env / "Local" / "Programs" / "AnythingLLM").mkdir(parents=True)
    assert linker.is_anythingllm_installed() is False


def test_is_anythingllm_installed_detects_install_under_omm_home_apps(anythingllm_windows_env, monkeypatch):
    apps = anythingllm_windows_env / "omm-home" / "apps"
    monkeypatch.setattr(linker, "engine_install_dir", lambda: apps)
    (apps / "AnythingLLM").mkdir(parents=True)
    (apps / "AnythingLLM" / "AnythingLLM.exe").write_bytes(b"")
    assert linker.is_anythingllm_installed() is True


def test_is_anythingllm_installed_detects_machine_wide_install_on_windows(
    anythingllm_windows_env, monkeypatch
):
    monkeypatch.setenv("ProgramFiles", str(anythingllm_windows_env / "Program Files"))
    (anythingllm_windows_env / "Program Files" / "AnythingLLM").mkdir(parents=True)
    (anythingllm_windows_env / "Program Files" / "AnythingLLM" / "AnythingLLM.exe").write_bytes(b"")
    assert linker.is_anythingllm_installed() is True


@pytest.mark.parametrize(
    "shortcut", ["AnythingLLM.lnk", "AnythingLLM/AnythingLLM Desktop.lnk"]
)
def test_is_anythingllm_installed_detects_start_menu_shortcut_on_windows(
    anythingllm_windows_env, shortcut
):
    path = _windows_start_menu(anythingllm_windows_env) / shortcut
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    assert linker.is_anythingllm_installed() is True


def test_is_anythingllm_installed_ignores_unrelated_start_menu_shortcut(anythingllm_windows_env):
    menu = _windows_start_menu(anythingllm_windows_env)
    menu.mkdir(parents=True)
    (menu / "Notepad.lnk").write_bytes(b"")
    assert linker.is_anythingllm_installed() is False


def test_windows_install_artifact_probe_is_windows_only(tmp_path, monkeypatch):
    """The Windows probe must stay inert on other platforms, the way
    _app_bundle_installed does for Darwin."""
    monkeypatch.setattr(linker.platform, "system", lambda: "Linux")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    (tmp_path / "Local" / "Programs" / "AnythingLLM").mkdir(parents=True)
    assert linker._windows_install_artifact_exists(("AnythingLLM",), "AnythingLLM*.lnk") is False


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


def test_engine_discovery_ignores_unapproved_and_symlinked_roots(tmp_path, monkeypatch):
    managed = tmp_path / "omm-owned"
    managed.mkdir()
    unapproved = tmp_path / "Downloads"
    unapproved.mkdir()
    (unapproved / "koboldcpp-malicious").write_bytes(b"x")
    monkeypatch.setattr(linker, "engine_install_dir", lambda: managed)
    monkeypatch.setattr(linker, "_HEURISTIC_SEARCH_ROOTS", [])

    assert linker.find_koboldcpp_binary() is None

    real_root = tmp_path / "real-root"
    real_root.mkdir()
    (real_root / "koboldcpp-malicious").write_bytes(b"x")
    symlinked_root = tmp_path / "symlinked-root"
    symlinked_root.symlink_to(real_root, target_is_directory=True)
    monkeypatch.setattr(linker, "engine_install_dir", lambda: symlinked_root)
    linker.find_koboldcpp_binary.cache_clear()

    assert linker.find_koboldcpp_binary() is None


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
        linker,
        "atomic_write_text",
        lambda path, content: (_ for _ in ()).throw(OSError("disk full")),
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
    config_path.write_text('model_path: "/gone/model.gguf"\n', encoding="utf-8")
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


def test_lms_cli_path_finds_windows_exe_bootstrap_location(tmp_path, monkeypatch):
    """The bootstrapped binary is `lms.exe` on Windows - the extensionless
    `lms` name checked by the POSIX path never exists there."""
    monkeypatch.setattr(linker.platform, "system", lambda: "Windows")
    monkeypatch.setattr(linker.shutil, "which", lambda name: None)
    monkeypatch.setattr(linker, "lmstudio_home_dir", lambda: tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    lms_file = bin_dir / "lms.exe"
    lms_file.write_bytes(b"")
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


# --- Public LM Studio daemon-lifecycle API --------------------------------


def test_lmstudio_daemon_reachable_true_when_running(monkeypatch):
    monkeypatch.setattr(linker, "_lms_cli_path", lambda: "lms")
    monkeypatch.setattr(linker, "_lmstudio_server_status", lambda lms_path: {"running": True, "port": 1234})
    assert linker.lmstudio_daemon_reachable() is True


def test_lmstudio_daemon_reachable_false_when_not_running(monkeypatch):
    monkeypatch.setattr(linker, "_lms_cli_path", lambda: "lms")
    monkeypatch.setattr(linker, "_lmstudio_server_status", lambda lms_path: {"running": False, "port": 1234})
    assert linker.lmstudio_daemon_reachable() is False


def test_lmstudio_daemon_reachable_false_when_status_unavailable(monkeypatch):
    monkeypatch.setattr(linker, "_lms_cli_path", lambda: "lms")
    monkeypatch.setattr(linker, "_lmstudio_server_status", lambda lms_path: None)
    assert linker.lmstudio_daemon_reachable() is False


def test_lmstudio_daemon_reachable_false_when_lms_missing(monkeypatch):
    monkeypatch.setattr(linker, "_lms_cli_path", lambda: None)
    assert linker.lmstudio_daemon_reachable() is False


def test_lmstudio_server_port_returns_port_when_running(monkeypatch):
    monkeypatch.setattr(linker, "_lms_cli_path", lambda: "lms")
    monkeypatch.setattr(linker, "_lmstudio_server_status", lambda lms_path: {"running": True, "port": 5678})
    assert linker.lmstudio_server_port() == 5678


def test_lmstudio_server_port_none_when_not_running(monkeypatch):
    monkeypatch.setattr(linker, "_lms_cli_path", lambda: "lms")
    monkeypatch.setattr(linker, "_lmstudio_server_status", lambda lms_path: {"running": False, "port": 1234})
    assert linker.lmstudio_server_port() is None


def test_lmstudio_server_port_none_when_status_unavailable(monkeypatch):
    monkeypatch.setattr(linker, "_lms_cli_path", lambda: "lms")
    monkeypatch.setattr(linker, "_lmstudio_server_status", lambda lms_path: None)
    assert linker.lmstudio_server_port() is None


def test_lmstudio_server_port_none_when_lms_missing(monkeypatch):
    monkeypatch.setattr(linker, "_lms_cli_path", lambda: None)
    assert linker.lmstudio_server_port() is None


def test_start_lmstudio_daemon_returns_true_when_successful(monkeypatch):
    monkeypatch.setattr(linker, "_lms_cli_path", lambda: "lms")
    monkeypatch.setattr(linker, "_start_lmstudio_server", lambda lms_path, timeout=30.0: True)
    assert linker.start_lmstudio_daemon() is True


def test_start_lmstudio_daemon_returns_false_when_fails(monkeypatch):
    monkeypatch.setattr(linker, "_lms_cli_path", lambda: "lms")
    monkeypatch.setattr(linker, "_start_lmstudio_server", lambda lms_path, timeout=30.0: False)
    assert linker.start_lmstudio_daemon() is False


def test_start_lmstudio_daemon_returns_false_when_lms_missing(monkeypatch):
    monkeypatch.setattr(linker, "_lms_cli_path", lambda: None)
    assert linker.start_lmstudio_daemon() is False


def test_start_lmstudio_daemon_passes_timeout(monkeypatch):
    calls = {"timeout_received": None}
    def fake_start(lms_path, timeout=30.0):
        calls["timeout_received"] = timeout
        return True
    monkeypatch.setattr(linker, "_lms_cli_path", lambda: "lms")
    monkeypatch.setattr(linker, "_start_lmstudio_server", fake_start)
    linker.start_lmstudio_daemon(timeout=60.0)
    assert calls["timeout_received"] == 60.0


def test_stop_lmstudio_daemon_calls_helper_when_lms_present(monkeypatch):
    stopped = {"called": False}
    def fake_stop(lms_path):
        stopped["called"] = True
    monkeypatch.setattr(linker, "_lms_cli_path", lambda: "lms")
    monkeypatch.setattr(linker, "_stop_lmstudio_server", fake_stop)
    linker.stop_lmstudio_daemon()
    assert stopped["called"] is True


def test_stop_lmstudio_daemon_no_op_when_lms_missing(monkeypatch):
    monkeypatch.setattr(linker, "_lms_cli_path", lambda: None)
    linker.stop_lmstudio_daemon()  # must not raise


def test_resolve_lmstudio_model_returns_dict_with_metadata(monkeypatch):
    models_data = [
        {
            "type": "llm",
            "modelKey": "tinyllama-1.1b-chat-v1.0",
            "path": "local/tinyllama-q4/tinyllama-q4.gguf",
            "architecture": "llama",
            "quantization": {"name": "Q4_K_M", "bits": 4},
            "paramsString": "1.1B",
            "maxContextLength": 2048,
            "trainedForToolUse": True,
        }
    ]
    monkeypatch.setattr(linker, "_lms_cli_path", lambda: "lms")
    monkeypatch.setattr(linker, "_lmstudio_model_key", lambda lms_path, pub, repo, filename: "tinyllama-1.1b-chat-v1.0")
    monkeypatch.setattr(linker, "_lmstudio_list_models", lambda lms_path: models_data)
    result = linker.resolve_lmstudio_model("local/tinyllama-q4", "tinyllama-q4.gguf")
    assert result == {
        "model_key": "tinyllama-1.1b-chat-v1.0",
        "architecture": "llama",
        "quantization_name": "Q4_K_M",
        "quantization_bits": 4,
        "params_string": "1.1B",
        "max_context_length": 2048,
        "trained_for_tool_use": True,
    }


def test_resolve_lmstudio_model_filters_embedding_models(monkeypatch):
    models_data = [
        {
            "type": "embedding",
            "modelKey": "nomic-embed-text-v1.5",
            "path": "nomic-ai/nomic-embed-text-v1.5-GGUF/nomic-embed-text-v1.5.gguf",
        },
        {
            "type": "llm",
            "modelKey": "tinyllama-1.1b-chat-v1.0",
            "path": "local/tinyllama-q4/tinyllama-q4.gguf",
            "architecture": "llama",
            "quantization": {"name": "Q4_K_M", "bits": 4},
        },
    ]
    monkeypatch.setattr(linker, "_lms_cli_path", lambda: "lms")
    monkeypatch.setattr(linker, "_lmstudio_model_key", lambda lms_path, pub, repo, filename: None)  # No match for embedding path
    monkeypatch.setattr(linker, "_lmstudio_list_models", lambda lms_path: models_data)
    # Try to resolve the embedding model - should return None (no path match)
    result = linker.resolve_lmstudio_model("nomic-ai/nomic-embed-text-v1.5-GGUF", "nomic-embed-text-v1.5.gguf")
    assert result is None


def test_resolve_lmstudio_model_none_when_path_not_found(monkeypatch):
    models_data = [
        {
            "type": "llm",
            "modelKey": "other-model",
            "path": "local/other/other.gguf",
        }
    ]
    monkeypatch.setattr(linker, "_lms_cli_path", lambda: "lms")
    monkeypatch.setattr(linker, "_lmstudio_model_key", lambda lms_path, pub, repo, filename: None)  # No match
    monkeypatch.setattr(linker, "_lmstudio_list_models", lambda lms_path: models_data)
    result = linker.resolve_lmstudio_model("local/repo", "file.gguf")
    assert result is None


def test_resolve_lmstudio_model_none_when_list_unavailable(monkeypatch):
    monkeypatch.setattr(linker, "_lms_cli_path", lambda: "lms")
    monkeypatch.setattr(linker, "_lmstudio_list_models", lambda lms_path: None)
    result = linker.resolve_lmstudio_model("local/repo", "file.gguf")
    assert result is None


def test_resolve_lmstudio_model_none_when_lms_missing(monkeypatch):
    monkeypatch.setattr(linker, "_lms_cli_path", lambda: None)
    result = linker.resolve_lmstudio_model("local/repo", "file.gguf")
    assert result is None


def test_resolve_lmstudio_model_handles_missing_optional_fields(monkeypatch):
    models_data = [
        {
            "type": "llm",
            "modelKey": "tinyllama-1.1b-chat-v1.0",
            "path": "local/tinyllama-q4/tinyllama-q4.gguf",
            # No architecture, quantization, etc.
        }
    ]
    monkeypatch.setattr(linker, "_lms_cli_path", lambda: "lms")
    monkeypatch.setattr(linker, "_lmstudio_model_key", lambda lms_path, pub, repo, filename: "tinyllama-1.1b-chat-v1.0")
    monkeypatch.setattr(linker, "_lmstudio_list_models", lambda lms_path: models_data)
    result = linker.resolve_lmstudio_model("local/tinyllama-q4", "tinyllama-q4.gguf")
    assert result == {
        "model_key": "tinyllama-1.1b-chat-v1.0",
        "architecture": None,
        "quantization_name": None,
        "quantization_bits": None,
        "params_string": None,
        "max_context_length": None,
        "trained_for_tool_use": False,
    }


def test_unload_lmstudio_model_returns_true_when_successful(monkeypatch):
    stopped = {"called": False}
    def fake_unload(lms_path, model_key):
        stopped["called"] = True
    monkeypatch.setattr(linker, "_lms_cli_path", lambda: "lms")
    monkeypatch.setattr(linker, "_lms_unload", fake_unload)
    assert linker.unload_lmstudio_model("test-model") is True
    assert stopped["called"] is True


def test_unload_lmstudio_model_returns_false_when_lms_missing(monkeypatch):
    monkeypatch.setattr(linker, "_lms_cli_path", lambda: None)
    assert linker.unload_lmstudio_model("test-model") is False
