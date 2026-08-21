#!/usr/bin/env python3
"""Validate and stage OMM's npm launcher and platform packages.

The checked-in source manifests always stay private. Release automation may
request publishable copies in a separate staging directory after the signed
release identity and package contents have been validated.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
LAUNCHER_SOURCE = ROOT / "packaging" / "npm" / "launcher"
TARGETS_FILE = LAUNCHER_SOURCE / "targets.json"
LICENSE_FILE = ROOT / "LICENSE"
LAUNCHER_NAME = "@omm-hippo/omm"
LIFECYCLE_SCRIPTS = {"preinstall", "install", "postinstall", "prepare"}
VERSION_PATTERN = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
EXPECTED_LAUNCHER_FILES = {
    "LICENSE",
    "bin/omm.js",
    "lib/launcher.js",
    "package.json",
    "targets.json",
}
MAGIC_PREFIXES = {
    "darwin": {
        bytes.fromhex("cffaedfe"),
        bytes.fromhex("feedfacf"),
        bytes.fromhex("cafebabe"),
        bytes.fromhex("cafebabf"),
    },
    "linux": {bytes.fromhex("7f454c46")},
    "win32": {b"MZ"},
}


class NpmPackageError(RuntimeError):
    """Raised when an npm package cannot be tied to the OMM release."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise NpmPackageError(f"cannot read JSON from {path}: {error}") from error
    if not isinstance(value, dict):
        raise NpmPackageError(f"{path} must contain a JSON object")
    return value


def project_version(pyproject: Path = PYPROJECT) -> str:
    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError as error:
        raise NpmPackageError(f"cannot read {pyproject}") from error
    match = re.search(r"(?ms)^\[project\]\s*$\n(.*?)(?=^\[|\Z)", text)
    if match is None:
        raise NpmPackageError(f"cannot read [project] from {pyproject}")
    project = match.group(1)

    def literal(field: str) -> str | None:
        value = re.search(
            rf'(?m)^{re.escape(field)}\s*=\s*"([^"]+)"\s*$', project
        )
        return value.group(1) if value is not None else None

    name = literal("name")
    version = literal("version")
    if name != "omm-model":
        raise NpmPackageError("pyproject [project].name must be 'omm-model'")
    if not isinstance(version, str) or VERSION_PATTERN.fullmatch(version) is None:
        raise NpmPackageError(f"unsupported project version: {version!r}")
    return version


def targets(path: Path = TARGETS_FILE) -> dict[str, dict[str, str]]:
    raw = _read_json(path)
    parsed: dict[str, dict[str, str]] = {}
    for target_name, value in raw.items():
        if not isinstance(target_name, str) or not isinstance(value, dict):
            raise NpmPackageError("npm targets must map names to objects")
        required = {"package", "os", "cpu", "binary"}
        if not required.issubset(value) or not all(
            isinstance(value[key], str) for key in required
        ):
            raise NpmPackageError(f"npm target {target_name!r} is incomplete")
        unexpected = set(value) - (required | {"libc"})
        if unexpected:
            raise NpmPackageError(
                f"npm target {target_name!r} has unexpected keys {sorted(unexpected)}"
            )
        if value["os"] == "linux":
            if value.get("libc") != "glibc" or not target_name.endswith("-gnu"):
                raise NpmPackageError(f"Linux target {target_name!r} must require glibc")
        elif "libc" in value:
            raise NpmPackageError(f"non-Linux target {target_name!r} cannot declare libc")
        parsed[target_name] = {key: str(item) for key, item in value.items()}
    if len({item["package"] for item in parsed.values()}) != len(parsed):
        raise NpmPackageError("npm target package names must be unique")
    return parsed


def validate_launcher_source(
    source: Path = LAUNCHER_SOURCE,
    *,
    expected_private: bool = True,
) -> None:
    manifest = _read_json(source / "package.json")
    version = project_version()
    target_map = targets(source / "targets.json")
    optional = manifest.get("optionalDependencies")
    scripts = manifest.get("scripts")
    if manifest.get("name") != LAUNCHER_NAME or manifest.get("version") != version:
        raise NpmPackageError("npm launcher name/version must match the OMM release")
    if manifest.get("private") is not expected_private:
        state = "private" if expected_private else "publishable"
        raise NpmPackageError(f"npm launcher must be {state}")
    if manifest.get("bin") != {"omm": "bin/omm.js"}:
        raise NpmPackageError("npm launcher must expose only the omm command")
    if manifest.get("engines") != {"node": ">=22.14.0"}:
        raise NpmPackageError("npm launcher must require the trusted-publishing Node floor")
    if not isinstance(scripts, dict) or LIFECYCLE_SCRIPTS.intersection(scripts):
        raise NpmPackageError("npm launcher cannot contain install lifecycle scripts")
    expected_optional = {
        value["package"]: version for value in target_map.values()
    }
    if optional != expected_optional:
        raise NpmPackageError("npm optional dependencies must exactly match all targets")
    if manifest.get("publishConfig") != {"access": "public", "provenance": True}:
        raise NpmPackageError("npm launcher publishConfig must require public provenance")
    repository = manifest.get("repository")
    if not isinstance(repository, dict) or repository.get("url") != (
        "git+https://github.com/omm-hippo/omm.git"
    ):
        raise NpmPackageError("npm launcher repository must be the canonical public repo")
    for relative in ("LICENSE", "bin/omm.js", "lib/launcher.js", "targets.json"):
        path = source / relative
        if not path.is_file() or path.is_symlink():
            raise NpmPackageError(f"missing regular launcher file: {relative}")
    if (source / "LICENSE").read_bytes() != LICENSE_FILE.read_bytes():
        raise NpmPackageError("npm launcher LICENSE must match the repository license")
    if not (source / "bin" / "omm.js").read_text(encoding="utf-8").startswith(
        "#!/usr/bin/env node\n"
    ):
        raise NpmPackageError("npm launcher must have a Node shebang")


def _file_allowlist(root: Path) -> set[str]:
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() or path.is_symlink()
    }


def _copy_text_lf(source: Path, destination: Path) -> None:
    destination.write_text(
        source.read_text(encoding="utf-8"),
        encoding="utf-8",
        newline="\n",
    )


def stage_launcher(output_dir: Path, *, publishable: bool = False) -> Path:
    validate_launcher_source()
    destination = output_dir / "omm-launcher"
    if destination.exists():
        raise NpmPackageError(f"refusing to overwrite {destination}")
    (destination / "bin").mkdir(parents=True)
    (destination / "lib").mkdir()
    shutil.copy2(LAUNCHER_SOURCE / "package.json", destination / "package.json")
    shutil.copy2(LAUNCHER_SOURCE / "targets.json", destination / "targets.json")
    _copy_text_lf(
        LAUNCHER_SOURCE / "bin" / "omm.js",
        destination / "bin" / "omm.js",
    )
    shutil.copy2(
        LAUNCHER_SOURCE / "lib" / "launcher.js", destination / "lib" / "launcher.js"
    )
    shutil.copy2(LICENSE_FILE, destination / "LICENSE")
    if publishable:
        manifest = _read_json(destination / "package.json")
        manifest["private"] = False
        (destination / "package.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
    (destination / "bin" / "omm.js").chmod(0o755)
    validate_launcher_package(destination, publishable=publishable)
    return destination


def validate_launcher_package(root: Path, *, publishable: bool = False) -> None:
    validate_launcher_source(root, expected_private=not publishable)
    actual = _file_allowlist(root)
    if actual != EXPECTED_LAUNCHER_FILES:
        raise NpmPackageError(
            f"launcher contains {sorted(actual)}, expected {sorted(EXPECTED_LAUNCHER_FILES)}"
        )
    if not (root / "LICENSE").read_text(encoding="utf-8").strip():
        raise NpmPackageError("launcher LICENSE is empty")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_binary(binary: Path, os_name: str) -> None:
    if not binary.is_file() or binary.is_symlink():
        raise NpmPackageError(f"binary must be a regular non-symlink file: {binary}")
    with binary.open("rb") as stream:
        prefix = stream.read(4)
    if not any(prefix.startswith(magic) for magic in MAGIC_PREFIXES[os_name]):
        raise NpmPackageError(f"binary does not match the {os_name} executable format")


def stage_platform_package(
    target_name: str,
    binary: Path,
    output_dir: Path,
    *,
    publishable: bool = False,
) -> Path:
    target_map = targets()
    if target_name not in target_map:
        raise NpmPackageError(f"unsupported npm target: {target_name!r}")
    target = target_map[target_name]
    _validate_binary(binary, target["os"])
    version = project_version()
    destination = output_dir / target_name
    if destination.exists():
        raise NpmPackageError(f"refusing to overwrite {destination}")
    packaged_binary = destination / target["binary"]
    packaged_binary.parent.mkdir(parents=True)
    shutil.copyfile(binary, packaged_binary)
    packaged_binary.chmod(0o755)
    shutil.copy2(LICENSE_FILE, destination / "LICENSE")

    manifest: dict[str, Any] = {
        "name": target["package"],
        "version": version,
        "description": f"Open Model Manager CLI binary for {target_name}",
        "license": "MIT",
        "private": not publishable,
        "repository": {
            "type": "git",
            "url": "git+https://github.com/omm-hippo/omm.git",
            "directory": "packaging/npm",
        },
        "os": [target["os"]],
        "cpu": [target["cpu"]],
        "files": [target["binary"], "LICENSE"],
        "publishConfig": {"access": "public", "provenance": True},
        "omm": {
            "binary": target["binary"],
            "distribution": "omm-model",
            "sha256": _sha256(packaged_binary),
            "target": target_name,
        },
    }
    if "libc" in target:
        manifest["libc"] = [target["libc"]]
    (destination / "package.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    validate_platform_package(destination, target_name, publishable=publishable)
    return destination


def validate_platform_package(
    root: Path,
    target_name: str,
    *,
    publishable: bool = False,
) -> None:
    target = targets()[target_name]
    version = project_version()
    manifest = _read_json(root / "package.json")
    binary = root / target["binary"]
    expected_private = not publishable
    expected_files = {"LICENSE", "package.json", target["binary"]}
    if _file_allowlist(root) != expected_files:
        raise NpmPackageError("platform package contains files outside its allowlist")
    _validate_binary(binary, target["os"])
    if (
        manifest.get("name") != target["package"]
        or manifest.get("version") != version
        or manifest.get("private") is not expected_private
        or manifest.get("os") != [target["os"]]
        or manifest.get("cpu") != [target["cpu"]]
        or manifest.get("publishConfig") != {"access": "public", "provenance": True}
    ):
        raise NpmPackageError("platform package identity is invalid")
    if target.get("libc"):
        if manifest.get("libc") != [target["libc"]]:
            raise NpmPackageError("platform package libc is invalid")
    elif "libc" in manifest:
        raise NpmPackageError("non-Linux platform package cannot declare libc")
    metadata = manifest.get("omm")
    if not isinstance(metadata, dict) or metadata != {
        "binary": target["binary"],
        "distribution": "omm-model",
        "sha256": _sha256(binary),
        "target": target_name,
    }:
        raise NpmPackageError("platform package OMM metadata is invalid")
    if not (root / "LICENSE").read_text(encoding="utf-8").strip():
        raise NpmPackageError("platform package LICENSE is empty")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate")
    commands.add_parser("version")
    launcher = commands.add_parser("stage-launcher")
    launcher.add_argument("--output-dir", type=Path, required=True)
    launcher.add_argument("--publishable", action="store_true")
    platform_package = commands.add_parser("stage-platform")
    platform_package.add_argument("--target", choices=sorted(targets()), required=True)
    platform_package.add_argument("--binary", type=Path, required=True)
    platform_package.add_argument("--output-dir", type=Path, required=True)
    platform_package.add_argument("--publishable", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "validate":
        validate_launcher_source()
    elif args.command == "version":
        print(project_version())
    elif args.command == "stage-launcher":
        stage_launcher(args.output_dir, publishable=args.publishable)
    elif args.command == "stage-platform":
        stage_platform_package(
            args.target,
            args.binary,
            args.output_dir,
            publishable=args.publishable,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
