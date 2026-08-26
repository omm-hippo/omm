from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import verify_engine_link


@pytest.mark.parametrize(
    "engine", ["ollama", "lmstudio", "koboldcpp", "textgenwebui"]
)
def test_filesystem_fixture_verifier_is_scoped_and_cleans_up(tmp_path, engine):
    omm_home = tmp_path / "omm-home"
    env = os.environ.copy()
    env["OMM_HOME"] = str(omm_home)
    result = subprocess.run(
        [
            sys.executable,
            "scripts/verify_engine_link.py",
            engine,
            "--filesystem-fixture",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"omm import adopted {engine}'s model" in result.stdout
    registry_path = omm_home / "models.json"
    assert json.loads(registry_path.read_text(encoding="utf-8")) == {}
    ownership_path = omm_home / "link-ownership.json"
    if ownership_path.exists():
        assert json.loads(ownership_path.read_text(encoding="utf-8")) == {}
    assert not (omm_home / "apps" / "models").exists()
    assert not (omm_home / "apps" / "text-generation-webui-ci-fixture").exists()
    assert not (omm_home / "apps" / "ollama-models-ci-fixture").exists()
    assert not (omm_home / "apps" / "lmstudio-models-ci-fixture").exists()


def test_flat_engine_verifier_accepts_hardlinks_and_owned_copy_shape(tmp_path):
    source = tmp_path / "source.gguf"
    source.write_bytes(b"GGUF fixture")
    hardlink = tmp_path / "hardlink.gguf"
    try:
        hardlink.hardlink_to(source)
    except OSError:
        pytest.skip("hard links are unavailable on this filesystem")
    copied = tmp_path / "copied.gguf"
    shutil.copyfile(source, copied)

    verify_engine_link._verify_via_path(hardlink, source, "hardlink")
    verify_engine_link._verify_via_path(copied, source, "copy")
