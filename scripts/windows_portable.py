#!/usr/bin/env python3
"""Build and validate the versioned Windows portable OMM release artifact."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path


DISTRIBUTION_NAME = "omm-model"
EXECUTABLE_NAME = "omm.exe"
LICENSE_NAME = "LICENSE.txt"
SUBPROCESS_TIMEOUT_SECONDS = 900
_VERSION_PATTERN = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")


class WindowsPortableError(RuntimeError):
    """Raised when a Windows portable artifact cannot be trusted."""


def project_version(pyproject: Path) -> str:
    try:
        project = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
    except (KeyError, OSError, tomllib.TOMLDecodeError) as error:
        raise WindowsPortableError(f"cannot read project metadata from {pyproject}") from error
    if project.get("name") != DISTRIBUTION_NAME:
        raise WindowsPortableError(
            f"project name is {project.get('name')!r}, expected {DISTRIBUTION_NAME!r}"
        )
    version = project.get("version")
    if not isinstance(version, str):
        raise WindowsPortableError("project version must be a string")
    _version_parts(version)
    return version


def _version_parts(version: str) -> tuple[int, int, int, int]:
    if _VERSION_PATTERN.fullmatch(version) is None:
        raise WindowsPortableError(f"unsupported release version: {version!r}")
    parts = tuple(int(part) for part in version.split("."))
    if any(part > 65_535 for part in parts):
        raise WindowsPortableError("Windows version components must be at most 65535")
    return parts[0], parts[1], parts[2], 0


def windows_version_resource(version: str) -> str:
    """Return a deterministic PyInstaller Windows version resource."""

    parts = _version_parts(version)
    return f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={parts!r},
    prodvers={parts!r},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0),
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [
          StringStruct('CompanyName', 'OMM Hippo'),
          StringStruct('FileDescription', 'Open Model Manager'),
          StringStruct('FileVersion', '{version}'),
          StringStruct('InternalName', 'omm'),
          StringStruct('LegalCopyright', 'Copyright OMM contributors'),
          StringStruct('OriginalFilename', '{EXECUTABLE_NAME}'),
          StringStruct('ProductName', 'Open Model Manager'),
          StringStruct('ProductVersion', '{version}'),
        ],
      ),
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])]),
  ],
)
"""


def pyinstaller_command(
    entry_script: Path,
    version_file: Path,
    output_dir: Path,
    work_dir: Path,
) -> list[str]:
    return [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--console",
        "--name",
        "omm",
        "--distpath",
        str(output_dir),
        "--workpath",
        str(work_dir / "build"),
        "--specpath",
        str(work_dir / "spec"),
        "--copy-metadata",
        DISTRIBUTION_NAME,
        "--collect-data",
        "omm",
        "--version-file",
        str(version_file),
        str(entry_script),
    ]


def validate_executable(executable: Path, version: str) -> None:
    if not executable.is_file() or executable.is_symlink():
        raise WindowsPortableError(f"missing regular executable: {executable}")
    if executable.read_bytes()[:2] != b"MZ":
        raise WindowsPortableError(f"{executable.name} is not a Windows executable")

    version_result = subprocess.run(
        [str(executable), "--version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    version_output = f"{version_result.stdout}\n{version_result.stderr}"
    if f"omm {version}" not in version_output:
        raise WindowsPortableError(
            f"portable command did not report the expected version {version}"
        )

    help_result = subprocess.run(
        [str(executable), "--help"],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    help_output = f"{help_result.stdout}\n{help_result.stderr}"
    if "Example usage:" not in help_output:
        raise WindowsPortableError("portable command help probe failed")


def build_windows_portable(version: str, output_dir: Path) -> Path:
    _version_parts(version)
    if os.name != "nt":
        raise WindowsPortableError("the Windows portable executable must be built on Windows")
    installed_version = importlib.metadata.version(DISTRIBUTION_NAME)
    if installed_version != version:
        raise WindowsPortableError(
            f"installed {DISTRIBUTION_NAME} is {installed_version}, expected {version}"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="omm-windows-portable-") as temporary:
        work_dir = Path(temporary)
        entry_script = work_dir / "omm_entry.py"
        entry_script.write_text(
            "from omm.cli import main\n\nif __name__ == '__main__':\n    main()\n",
            encoding="utf-8",
        )
        version_file = work_dir / "version_info.txt"
        version_file.write_text(windows_version_resource(version), encoding="utf-8")
        subprocess.run(
            pyinstaller_command(entry_script, version_file, output_dir, work_dir),
            check=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )

    executable = output_dir / EXECUTABLE_NAME
    validate_executable(executable, version)
    return executable


def _zip_info(name: str, mode: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = mode << 16
    return info


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def package_windows_portable(
    executable: Path,
    license_file: Path,
    version: str,
    output_dir: Path,
) -> tuple[Path, Path]:
    _version_parts(version)
    if not executable.is_file() or executable.is_symlink():
        raise WindowsPortableError(f"missing regular executable: {executable}")
    executable_bytes = executable.read_bytes()
    if executable_bytes[:2] != b"MZ":
        raise WindowsPortableError(f"{executable.name} is not a Windows executable")
    if not license_file.is_file() or license_file.is_symlink():
        raise WindowsPortableError(f"missing regular license: {license_file}")
    license_bytes = license_file.read_bytes()
    if not license_bytes.strip():
        raise WindowsPortableError("license file is empty")

    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / f"omm-windows-x64-{version}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(_zip_info(EXECUTABLE_NAME, 0o755), executable_bytes)
        bundle.writestr(_zip_info(LICENSE_NAME, 0o644), license_bytes)

    checksum = output_dir / f"{archive.name}.sha256"
    checksum.write_text(f"{sha256(archive)}  {archive.name}\n", encoding="ascii")
    verify_windows_archive(archive, version)
    return archive, checksum


def verify_windows_archive(archive: Path, version: str) -> None:
    _version_parts(version)
    expected_name = f"omm-windows-x64-{version}.zip"
    if archive.name != expected_name:
        raise WindowsPortableError(
            f"archive is named {archive.name!r}, expected {expected_name!r}"
        )
    if not archive.is_file() or archive.is_symlink():
        raise WindowsPortableError(f"missing regular archive: {archive}")

    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        expected = [EXECUTABLE_NAME, LICENSE_NAME]
        if names != expected or len(set(names)) != len(names):
            raise WindowsPortableError(
                f"archive contains {names!r}, expected exactly {expected!r}"
            )
        if bundle.read(EXECUTABLE_NAME)[:2] != b"MZ":
            raise WindowsPortableError("archive executable has no Windows MZ header")
        if not bundle.read(LICENSE_NAME).strip():
            raise WindowsPortableError("archive license is empty")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_parser = subparsers.add_parser("build")
    build_parser.add_argument("--version", required=True)
    build_parser.add_argument("--output-dir", type=Path, required=True)

    package_parser = subparsers.add_parser("package")
    package_parser.add_argument("--version", required=True)
    package_parser.add_argument("--executable", type=Path, required=True)
    package_parser.add_argument("--license", type=Path, required=True)
    package_parser.add_argument("--output-dir", type=Path, required=True)

    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--version", required=True)
    verify_parser.add_argument("--archive", type=Path, required=True)

    project_parser = subparsers.add_parser("project-version")
    project_parser.add_argument("--pyproject", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "build":
            print(build_windows_portable(args.version, args.output_dir))
        elif args.command == "package":
            archive, checksum = package_windows_portable(
                args.executable,
                args.license,
                args.version,
                args.output_dir,
            )
            print(archive)
            print(checksum)
        elif args.command == "verify":
            verify_windows_archive(args.archive, args.version)
        elif args.command == "project-version":
            print(project_version(args.pyproject))
    except (OSError, subprocess.SubprocessError, WindowsPortableError, zipfile.BadZipFile) as error:
        print(f"Windows portable validation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
