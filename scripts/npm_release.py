#!/usr/bin/env python3
"""Verify, publish, and exercise OMM npm release tarballs."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import npm_package

CHECKSUMS_NAME = "SHA256SUMS"
REGISTRY = "https://registry.npmjs.org/"
LIFECYCLE_SCRIPTS = {"preinstall", "install", "postinstall", "prepare"}
MAX_UNPACKED_PACKAGE_BYTES = 128 * 1024 * 1024
MAX_TAR_MEMBERS = 1_000


class NpmReleaseError(RuntimeError):
    """Raised when an npm release cannot be proven safe and complete."""


@dataclass(frozen=True)
class PackageInfo:
    path: Path
    name: str
    version: str
    target: str | None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _integrity(path: Path) -> str:
    digest = hashlib.sha512()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return "sha512-" + base64.b64encode(digest.digest()).decode("ascii")


def _tar_files(bundle: tarfile.TarFile) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    unpacked_size = 0
    for member_index, member in enumerate(bundle.getmembers(), start=1):
        if member_index > MAX_TAR_MEMBERS:
            raise NpmReleaseError("npm tarball contains too many members")
        path = PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise NpmReleaseError(f"unsafe npm tar member: {member.name!r}")
        if path.parts[0] != "package":
            raise NpmReleaseError(f"npm tar member is outside package/: {member.name!r}")
        if member.isdir():
            continue
        if not member.isfile() or member.issym() or member.islnk():
            raise NpmReleaseError(f"npm tar member is not a regular file: {member.name!r}")
        extracted = bundle.extractfile(member)
        if extracted is None:
            raise NpmReleaseError(f"cannot read npm tar member: {member.name!r}")
        if member.name in files:
            raise NpmReleaseError(f"duplicate npm tar member: {member.name!r}")
        unpacked_size += member.size
        if unpacked_size > MAX_UNPACKED_PACKAGE_BYTES:
            raise NpmReleaseError("npm tarball exceeds the unpacked size limit")
        files[member.name] = extracted.read()
    return files


def _manifest(files: dict[str, bytes]) -> dict[str, Any]:
    try:
        value = json.loads(files["package/package.json"].decode("utf-8"))
    except (KeyError, UnicodeDecodeError, ValueError) as error:
        raise NpmReleaseError("npm tarball has no valid package.json") from error
    if not isinstance(value, dict):
        raise NpmReleaseError("npm package.json must contain an object")
    return value


def inspect_tarball(path: Path) -> PackageInfo:
    if not path.is_file() or path.is_symlink() or path.suffix != ".tgz":
        raise NpmReleaseError(f"invalid npm tarball: {path}")
    try:
        with tarfile.open(path, mode="r:gz") as bundle:
            files = _tar_files(bundle)
    except (OSError, tarfile.TarError) as error:
        raise NpmReleaseError(f"cannot read npm tarball {path.name}: {error}") from error

    manifest = _manifest(files)
    name = manifest.get("name")
    version = manifest.get("version")
    expected_version = npm_package.project_version()
    if not isinstance(name, str) or not isinstance(version, str):
        raise NpmReleaseError("npm package name and version must be strings")
    if version != expected_version:
        raise NpmReleaseError(
            f"npm package {name!r} has version {version!r}, expected {expected_version!r}"
        )
    if manifest.get("private") is not False:
        raise NpmReleaseError(f"npm package {name!r} is not explicitly publishable")
    if manifest.get("publishConfig") != {"access": "public", "provenance": True}:
        raise NpmReleaseError(f"npm package {name!r} does not require public provenance")
    scripts = manifest.get("scripts", {})
    if not isinstance(scripts, dict) or LIFECYCLE_SCRIPTS.intersection(scripts):
        raise NpmReleaseError(f"npm package {name!r} has an install lifecycle script")
    repository = manifest.get("repository")
    if not isinstance(repository, dict) or repository.get("url") != (
        "git+https://github.com/omm-hippo/omm.git"
    ):
        raise NpmReleaseError(f"npm package {name!r} has the wrong repository")

    if name == npm_package.LAUNCHER_NAME:
        expected_files = {f"package/{item}" for item in npm_package.EXPECTED_LAUNCHER_FILES}
        if set(files) != expected_files:
            raise NpmReleaseError("npm launcher tarball has files outside its allowlist")
        if files["package/LICENSE"] != npm_package.canonical_license_bytes():
            raise NpmReleaseError("npm launcher license does not match the repository")
        expected_launcher = (
            npm_package.LAUNCHER_SOURCE / "bin" / "omm.js"
        ).read_text(encoding="utf-8").replace("\r\n", "\n").encode("utf-8")
        if files["package/bin/omm.js"] != expected_launcher:
            raise NpmReleaseError("npm launcher entry point does not match the source")
        if files["package/lib/launcher.js"] != (
            npm_package.LAUNCHER_SOURCE / "lib" / "launcher.js"
        ).read_bytes():
            raise NpmReleaseError("npm launcher implementation does not match the source")
        if manifest.get("bin") != {"omm": "bin/omm.js"}:
            raise NpmReleaseError("npm launcher exposes an unexpected command")
        if manifest.get("engines") != {"node": ">=22.14.0"}:
            raise NpmReleaseError("npm launcher has the wrong Node version floor")
        try:
            packed_targets = json.loads(files["package/targets.json"].decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise NpmReleaseError("npm launcher targets.json is invalid") from error
        if packed_targets != npm_package.targets():
            raise NpmReleaseError("npm launcher target map does not match the source contract")
        expected_optional = {
            value["package"]: expected_version
            for value in npm_package.targets().values()
        }
        if manifest.get("optionalDependencies") != expected_optional:
            raise NpmReleaseError("npm launcher has the wrong optional dependencies")
        return PackageInfo(path=path, name=name, version=version, target=None)

    target_by_package = {
        value["package"]: (target_name, value)
        for target_name, value in npm_package.targets().items()
    }
    if name not in target_by_package:
        raise NpmReleaseError(f"unexpected npm package: {name!r}")
    target_name, target = target_by_package[name]
    binary_name = f"package/{target['binary']}"
    expected_files = {"package/LICENSE", "package/package.json", binary_name}
    if set(files) != expected_files:
        raise NpmReleaseError(f"npm platform tarball {name!r} has unexpected files")
    if files["package/LICENSE"] != npm_package.canonical_license_bytes():
        raise NpmReleaseError(
            f"npm platform tarball {name!r} license does not match the repository"
        )
    binary = files[binary_name]
    magic = npm_package.MAGIC_PREFIXES[target["os"]]
    if not any(binary.startswith(prefix) for prefix in magic):
        raise NpmReleaseError(f"npm platform binary does not match {target_name}")
    metadata = manifest.get("omm")
    if not isinstance(metadata, dict) or metadata != {
        "binary": target["binary"],
        "distribution": "omm-model",
        "sha256": hashlib.sha256(binary).hexdigest(),
        "target": target_name,
    }:
        raise NpmReleaseError(f"npm platform metadata does not match {target_name}")
    if manifest.get("os") != [target["os"]] or manifest.get("cpu") != [target["cpu"]]:
        raise NpmReleaseError(f"npm platform selectors do not match {target_name}")
    expected_libc = [target["libc"]] if "libc" in target else None
    if manifest.get("libc") != expected_libc and (
        expected_libc is not None or "libc" in manifest
    ):
        raise NpmReleaseError(f"npm libc selector does not match {target_name}")
    return PackageInfo(path=path, name=name, version=version, target=target_name)


def verify_bundle(pack_dir: Path, *, write_checksums: bool = False) -> list[PackageInfo]:
    if not pack_dir.is_dir():
        raise NpmReleaseError(f"npm package directory does not exist: {pack_dir}")
    checksum_path = pack_dir / CHECKSUMS_NAME
    if checksum_path.is_symlink():
        raise NpmReleaseError("npm bundle checksum file cannot be a symlink")
    allowed_files = {checksum_path} if checksum_path.exists() else set()
    tarballs = sorted(pack_dir.glob("*.tgz"))
    actual_entries = set(pack_dir.iterdir())
    if actual_entries != set(tarballs) | allowed_files:
        unexpected = sorted(
            path.name for path in actual_entries - set(tarballs) - allowed_files
        )
        raise NpmReleaseError(f"npm bundle contains unexpected files: {unexpected}")
    packages = [inspect_tarball(path) for path in tarballs]
    expected_names = {npm_package.LAUNCHER_NAME} | {
        value["package"] for value in npm_package.targets().values()
    }
    names = [package.name for package in packages]
    if len(names) != len(set(names)) or set(names) != expected_names:
        raise NpmReleaseError(
            f"npm bundle contains packages {sorted(names)}, expected {sorted(expected_names)}"
        )
    expected_lines = [f"{_sha256(path)}  {path.name}" for path in tarballs]
    expected_text = "\n".join(expected_lines) + "\n"
    if write_checksums:
        checksum_path.write_text(expected_text, encoding="ascii")
    elif not checksum_path.is_file() or checksum_path.read_text(encoding="ascii") != expected_text:
        raise NpmReleaseError("npm bundle checksums are missing or do not match")
    return packages


def _native_command(executable: str | Path, *arguments: str) -> list[str]:
    executable = str(executable)
    if os.name == "nt" and Path(executable).suffix.casefold() in {".cmd", ".bat"}:
        command = subprocess.list2cmdline([executable, *arguments])
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", command]
    return [executable, *arguments]


def _validate_registry_url(registry: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(registry)
        port = parsed.port
    except ValueError as error:
        raise NpmReleaseError(f"invalid npm registry URL: {registry!r}") from error
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port not in (None, 443)
    ):
        raise NpmReleaseError("npm registry URL must be credential-free HTTPS")
    return registry


def _run(
    executable: str | Path,
    *arguments: str,
    check: bool = True,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _native_command(executable, *arguments),
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=cwd,
        timeout=300,
    )


def _npm() -> str:
    command = shutil.which("npm")
    if command is None:
        raise NpmReleaseError("npm is not installed")
    return command


def _command_path(prefix: Path) -> Path:
    return prefix / ("omm.cmd" if os.name == "nt" else "bin/omm")


def _probe_install(
    prefix: Path,
    omm_home: Path,
    version: str,
    target_package: str,
) -> None:
    command = _command_path(prefix)
    if not command.is_file():
        raise NpmReleaseError(f"npm did not expose the OMM command at {command}")
    version_result = _run(command, "--version")
    version_lines = f"{version_result.stdout}\n{version_result.stderr}".splitlines()
    if f"omm {version}" not in {line.strip() for line in version_lines}:
        raise NpmReleaseError("npm-installed OMM reported the wrong version")
    help_result = _run(command, "--help")
    if "Example usage:" not in f"{help_result.stdout}\n{help_result.stderr}":
        raise NpmReleaseError("npm-installed OMM help probe failed")
    environment = dict(os.environ)
    environment["OMM_HOME"] = str(omm_home)
    update = _run(command, "update", check=False, env=environment)
    update_output = f"{update.stdout}\n{update.stderr}"
    if update.returncode != 1 or "npm update --global @omm-hippo/omm" not in update_output:
        raise NpmReleaseError("npm-installed OMM did not report npm update guidance")
    if (omm_home / "src").exists():
        raise NpmReleaseError("npm-installed OMM unexpectedly created a Git checkout")
    _run(
        _npm(),
        "uninstall",
        "--global",
        "--prefix",
        str(prefix),
        npm_package.LAUNCHER_NAME,
        target_package,
    )
    # Path.exists() follows symlinks, so it misses a dangling launcher left
    # behind when npm removes the target but not the link itself.
    if os.path.lexists(command):
        raise NpmReleaseError("npm uninstall left the OMM command exposed")


def smoke_tarballs(pack_dir: Path, target_name: str) -> None:
    packages = verify_bundle(pack_dir)
    launcher = next(package for package in packages if package.target is None)
    platform_package = next(package for package in packages if package.target == target_name)
    with tempfile.TemporaryDirectory(prefix="omm-npm-smoke-") as temporary:
        root = Path(temporary)
        prefix = root / "prefix"
        _run(
            _npm(),
            "install",
            "--global",
            "--prefix",
            str(prefix),
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
            str(platform_package.path),
            str(launcher.path),
        )
        _probe_install(
            prefix,
            root / "omm-home",
            launcher.version,
            platform_package.name,
        )


def smoke_registry(version: str, target_name: str, registry: str = REGISTRY) -> None:
    registry = _validate_registry_url(registry)
    target_package = npm_package.targets()[target_name]["package"]
    with tempfile.TemporaryDirectory(prefix="omm-npm-registry-") as temporary:
        root = Path(temporary)
        prefix = root / "prefix"
        _run(
            _npm(),
            "install",
            "--global",
            "--prefix",
            str(prefix),
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
            "--registry",
            registry,
            f"{npm_package.LAUNCHER_NAME}@{version}",
        )
        _probe_install(prefix, root / "omm-home", version, target_package)

        audit = root / "audit"
        audit.mkdir()
        _run(_npm(), "init", "--yes", cwd=audit)
        _run(
            _npm(),
            "install",
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
            "--registry",
            registry,
            f"{npm_package.LAUNCHER_NAME}@{version}",
            cwd=audit,
        )
        _run(_npm(), "audit", "signatures", cwd=audit)


def _registry_integrity(package: PackageInfo, registry: str) -> str | None:
    result = _run(
        _npm(),
        "view",
        f"{package.name}@{package.version}",
        "dist.integrity",
        "--json",
        "--registry",
        registry,
        check=False,
    )
    if result.returncode == 0:
        try:
            value = json.loads(result.stdout)
        except ValueError as error:
            raise NpmReleaseError("npm registry returned invalid integrity JSON") from error
        if not isinstance(value, str):
            raise NpmReleaseError("npm registry returned no package integrity")
        return value
    if "E404" in f"{result.stdout}\n{result.stderr}":
        return None
    raise NpmReleaseError(
        f"npm registry lookup failed for {package.name}@{package.version}: {result.stderr}"
    )


def reuse_published_packages(pack_dir: Path, registry: str = REGISTRY) -> None:
    """Replace rebuilt tarballs with immutable bytes already in the registry."""

    registry = _validate_registry_url(registry)
    packages = verify_bundle(pack_dir, write_checksums=True)
    for package in packages:
        published_integrity = _registry_integrity(package, registry)
        if published_integrity is None:
            print(f"Not published yet; keeping built bytes: {package.name}@{package.version}")
            continue
        with tempfile.TemporaryDirectory(prefix="omm-npm-published-") as temporary:
            destination = Path(temporary)
            _run(
                _npm(),
                "pack",
                f"{package.name}@{package.version}",
                "--ignore-scripts",
                "--pack-destination",
                str(destination),
                "--json",
                "--registry",
                registry,
            )
            downloaded_tarballs = list(destination.glob("*.tgz"))
            if len(downloaded_tarballs) != 1:
                raise NpmReleaseError(
                    f"npm pack returned {len(downloaded_tarballs)} tarballs for "
                    f"{package.name}@{package.version}"
                )
            downloaded = inspect_tarball(downloaded_tarballs[0])
            if (
                downloaded.name,
                downloaded.version,
                downloaded.target,
            ) != (package.name, package.version, package.target):
                raise NpmReleaseError(
                    f"downloaded registry package identity does not match {package.name}"
                )
            if _integrity(downloaded.path) != published_integrity:
                raise NpmReleaseError(
                    f"downloaded registry package integrity does not match {package.name}"
                )
            shutil.copyfile(downloaded.path, package.path)
            print(f"Reused published bytes: {package.name}@{package.version}")
    verify_bundle(pack_dir, write_checksums=True)


def publish_bundle(pack_dir: Path, registry: str = REGISTRY) -> None:
    registry = _validate_registry_url(registry)
    packages = verify_bundle(pack_dir)
    ordered = sorted(packages, key=lambda package: package.target is None)
    for package in ordered:
        expected_integrity = _integrity(package.path)
        published_integrity = _registry_integrity(package, registry)
        if published_integrity is not None:
            if published_integrity != expected_integrity:
                raise NpmReleaseError(
                    f"existing {package.name}@{package.version} has different bytes"
                )
            print(f"Already published with matching bytes: {package.name}@{package.version}")
            continue
        result = _run(
            _npm(),
            "publish",
            str(package.path),
            "--access",
            "public",
            "--registry",
            registry,
            check=False,
        )
        if result.returncode != 0:
            raise NpmReleaseError(
                f"npm publish failed for {package.name}@{package.version}: {result.stderr}"
            )
        for attempt in range(8):
            published_integrity = _registry_integrity(package, registry)
            if published_integrity == expected_integrity:
                break
            if published_integrity is not None:
                raise NpmReleaseError(
                    f"published {package.name}@{package.version} has different bytes"
                )
            if attempt < 7:
                time.sleep(5)
        else:
            raise NpmReleaseError(
                f"published {package.name}@{package.version} did not appear in the registry"
            )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    bundle = commands.add_parser("verify-bundle")
    bundle.add_argument("--pack-dir", type=Path, required=True)
    bundle.add_argument("--write-checksums", action="store_true")

    smoke = commands.add_parser("smoke-tarballs")
    smoke.add_argument("--pack-dir", type=Path, required=True)
    smoke.add_argument("--target", choices=sorted(npm_package.targets()), required=True)

    registry_smoke = commands.add_parser("smoke-registry")
    registry_smoke.add_argument("--version", required=True)
    registry_smoke.add_argument("--target", choices=sorted(npm_package.targets()), required=True)
    registry_smoke.add_argument("--registry", default=REGISTRY)

    publish = commands.add_parser("publish-bundle")
    publish.add_argument("--pack-dir", type=Path, required=True)
    publish.add_argument("--registry", default=REGISTRY)

    reuse = commands.add_parser("reuse-published")
    reuse.add_argument("--pack-dir", type=Path, required=True)
    reuse.add_argument("--registry", default=REGISTRY)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "verify-bundle":
            verify_bundle(args.pack_dir, write_checksums=args.write_checksums)
        elif args.command == "smoke-tarballs":
            smoke_tarballs(args.pack_dir, args.target)
        elif args.command == "smoke-registry":
            smoke_registry(args.version, args.target, args.registry)
        elif args.command == "publish-bundle":
            publish_bundle(args.pack_dir, args.registry)
        elif args.command == "reuse-published":
            reuse_published_packages(args.pack_dir, args.registry)
    except (NpmReleaseError, OSError, subprocess.SubprocessError) as error:
        print(f"npm release validation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
