#!/usr/bin/env python3
"""Build-independent checks for OMM release archives.

The release workflow calls this module after ``python -m build``.  Keeping
the checks in Python instead of shell makes the same validation available on
macOS, Linux, and Windows runners.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import venv
import zipfile
from email.parser import BytesParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
CHECKSUMS_FILENAME = "SHA256SUMS"
SUBPROCESS_TIMEOUT_SECONDS = 300


class ReleaseValidationError(RuntimeError):
    """Raised when an archive cannot be tied to the declared release."""


def _project_table(text: str) -> str:
    match = re.search(r"(?ms)^\[project\]\s*$\n(.*?)(?=^\[|\Z)", text)
    if match is None:
        raise ReleaseValidationError("pyproject.toml has no [project] table")
    return match.group(1)


def project_identity(pyproject: Path = PYPROJECT) -> tuple[str, str]:
    table = _project_table(pyproject.read_text(encoding="utf-8"))

    def value(field: str) -> str:
        match = re.search(rf'(?m)^{re.escape(field)}\s*=\s*"([^"]+)"\s*$', table)
        if match is None:
            raise ReleaseValidationError(f"[project].{field} must be a literal string")
        return match.group(1)

    return value("name"), value("version")


def validate_tag(tag: str, version: str) -> None:
    expected = f"v{version}"
    if tag != expected:
        raise ReleaseValidationError(
            f"release tag {tag!r} does not match pyproject version; expected {expected!r}"
        )


def _metadata_identity(payload: bytes, source: Path) -> tuple[str, str]:
    metadata = BytesParser().parsebytes(payload)
    name = metadata.get("Name")
    version = metadata.get("Version")
    if not name or not version:
        raise ReleaseValidationError(f"{source.name} metadata has no Name/Version")
    return name, version


def _wheel_identity(wheel: Path) -> tuple[str, str]:
    with zipfile.ZipFile(wheel) as archive:
        metadata_files = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_files) != 1:
            raise ReleaseValidationError(
                f"{wheel.name} must contain exactly one .dist-info/METADATA file"
            )
        return _metadata_identity(archive.read(metadata_files[0]), wheel)


def _sdist_identity(sdist: Path) -> tuple[str, str]:
    with tarfile.open(sdist, "r:gz") as archive:
        metadata_files = [
            member
            for member in archive.getmembers()
            if member.isfile() and member.name.endswith("/PKG-INFO")
        ]
        if len(metadata_files) != 1:
            raise ReleaseValidationError(
                f"{sdist.name} must contain exactly one top-level PKG-INFO file"
            )
        stream = archive.extractfile(metadata_files[0])
        if stream is None:
            raise ReleaseValidationError(f"cannot read metadata from {sdist.name}")
        return _metadata_identity(stream.read(), sdist)


def distribution_archives(dist_dir: Path) -> tuple[Path, Path]:
    wheels = sorted(dist_dir.glob("*.whl"))
    sdists = sorted(dist_dir.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ReleaseValidationError(
            f"expected one wheel and one sdist in {dist_dir}, found "
            f"{len(wheels)} wheel(s) and {len(sdists)} sdist(s)"
        )
    return wheels[0], sdists[0]


def _validate_dist_contents(
    dist_dir: Path, archives: tuple[Path, Path], *, require_checksums: bool
) -> None:
    expected = {path.name for path in archives}
    checksum_file = dist_dir / CHECKSUMS_FILENAME
    if require_checksums or checksum_file.exists():
        expected.add(CHECKSUMS_FILENAME)
    actual = {entry.name for entry in dist_dir.iterdir()}
    if actual != expected:
        raise ReleaseValidationError(
            f"release bundle contains {sorted(actual)}, expected exactly {sorted(expected)}"
        )
    for entry in dist_dir.iterdir():
        if not entry.is_file() or entry.is_symlink():
            raise ReleaseValidationError(
                f"release bundle entry {entry.name!r} must be a regular file"
            )


def validate_archives(dist_dir: Path, pyproject: Path = PYPROJECT) -> tuple[Path, Path]:
    expected = project_identity(pyproject)
    wheel, sdist = distribution_archives(dist_dir)
    _validate_dist_contents(dist_dir, (wheel, sdist), require_checksums=False)
    if not wheel.name.endswith("-py3-none-any.whl"):
        raise ReleaseValidationError(
            f"{wheel.name} is not the expected platform-independent Python 3 wheel"
        )
    for archive, actual in ((wheel, _wheel_identity(wheel)), (sdist, _sdist_identity(sdist))):
        if actual != expected:
            raise ReleaseValidationError(
                f"{archive.name} identifies {actual[0]} {actual[1]}, "
                f"but pyproject declares {expected[0]} {expected[1]}"
            )
    return wheel, sdist


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_checksums(dist_dir: Path) -> Path:
    archives = distribution_archives(dist_dir)
    _validate_dist_contents(dist_dir, archives, require_checksums=False)
    destination = dist_dir / CHECKSUMS_FILENAME
    lines = [f"{_sha256(path)}  {path.name}" for path in sorted(archives)]
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return destination


def validate_checksums(dist_dir: Path) -> None:
    checksum_file = dist_dir / CHECKSUMS_FILENAME
    if not checksum_file.is_file():
        raise ReleaseValidationError(f"missing {checksum_file}")
    archives = distribution_archives(dist_dir)
    _validate_dist_contents(dist_dir, archives, require_checksums=True)
    expected_files = {path.name for path in archives}
    seen: set[str] = set()
    for line in checksum_file.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([^/]+)", line)
        if match is None:
            raise ReleaseValidationError(f"invalid checksum line: {line!r}")
        expected_hash, filename = match.groups()
        if filename in seen:
            raise ReleaseValidationError(f"duplicate checksum entry for {filename}")
        if filename not in expected_files:
            raise ReleaseValidationError(f"unexpected checksum entry for {filename}")
        path = dist_dir / filename
        if not path.is_file() or _sha256(path) != expected_hash:
            raise ReleaseValidationError(f"checksum mismatch for {filename}")
        seen.add(filename)
    if seen != expected_files:
        raise ReleaseValidationError(
            f"checksum file covers {sorted(seen)}, expected {sorted(expected_files)}"
        )


def _venv_executable(root: Path, name: str, platform_name: str = os.name) -> Path:
    scripts = root / ("Scripts" if platform_name == "nt" else "bin")
    suffix = ".exe" if platform_name == "nt" else ""
    return scripts / f"{name}{suffix}"


def smoke_install(dist_dir: Path, pyproject: Path = PYPROJECT) -> None:
    name, version = project_identity(pyproject)
    wheel, _ = validate_archives(dist_dir, pyproject)
    validate_checksums(dist_dir)
    with tempfile.TemporaryDirectory(prefix="omm-release-smoke-") as temporary:
        root = Path(temporary)
        environment = root / "venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(environment)
        python = _venv_executable(environment, "python")
        subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                str(wheel),
            ],
            check=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
        probe = (
            "import importlib.metadata, omm, localfit_server; "
            f"assert importlib.metadata.version({name!r}) == {version!r}"
        )
        subprocess.run(
            [str(python), "-c", probe],
            check=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
        command = _venv_executable(environment, "omm")
        command_env = os.environ.copy()
        command_env["OMM_HOME"] = str(root / "omm-home")
        subprocess.run(
            [str(command), "help"],
            check=True,
            env=command_env,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    tag_parser = subparsers.add_parser("check-tag")
    tag_parser.add_argument("--tag", required=True)

    for command in ("verify-dist", "write-checksums", "verify-checksums", "smoke-install"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("--dist-dir", type=Path, default=ROOT / "dist")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "check-tag":
            _, version = project_identity()
            validate_tag(args.tag, version)
        elif args.command == "verify-dist":
            validate_archives(args.dist_dir)
        elif args.command == "write-checksums":
            print(write_checksums(args.dist_dir))
        elif args.command == "verify-checksums":
            validate_checksums(args.dist_dir)
        elif args.command == "smoke-install":
            smoke_install(args.dist_dir)
        else:  # pragma: no cover - argparse constrains the command
            raise AssertionError(args.command)
    except (OSError, ReleaseValidationError, subprocess.CalledProcessError) as error:
        print(f"release validation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
