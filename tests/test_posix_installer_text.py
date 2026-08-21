from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_unix_installer_persists_pipx_path_for_macos_login_zsh():
    script = (ROOT / "install.sh").read_text(encoding="utf-8")

    assert "ensure_pipx_bin_path()" in script
    assert "Darwin)" in script
    assert 'zprofile="$HOME/.zprofile"' in script
    assert 'export PATH="%s:$PATH"' in script
    assert 'run_pipx ensurepath >/dev/null 2>&1 || true' in script


def test_unix_installer_updates_current_process_path():
    script = (ROOT / "install.sh").read_text(encoding="utf-8")

    assert 'PATH="$PIPX_BIN_DIR:$PATH"; export PATH' in script
    assert "ensure_pipx_bin_path" in script
