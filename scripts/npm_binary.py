#!/usr/bin/env python3
"""Build and validate native standalone OMM binaries for npm packages.

Windows is intentionally absent. Its portable artifact is owned and verified
by the separate Winget workflow and must be consumed only after that public
artifact has been verified.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import os
import platform
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DISTRIBUTION_NAME = "omm-model"
EXECUTABLE_NAME = "omm"
SUBPROCESS_TIMEOUT_SECONDS = 900
TARGETS = {
    ("darwin", "arm64"): "darwin-arm64",
    ("darwin", "x64"): "darwin-x64",
    ("linux", "arm64"): "linux-arm64-gnu",
    ("linux", "x64"): "linux-x64-gnu",
}
MAGIC_PREFIXES = {
    "darwin": {
        bytes.fromhex("cffaedfe"),
        bytes.fromhex("feedfacf"),
        bytes.fromhex("cafebabe"),
        bytes.fromhex("cafebabf"),
    },
    "linux": {bytes.fromhex("7f454c46")},
}


class NpmBinaryError(RuntimeError):
    """Raised when a native executable cannot be tied to the OMM release."""


def _machine(raw: str | None = None) -> str | None:
    machine = (raw or platform.machine()).casefold()
    if machine in {"amd64", "x86_64"}:
        return "x64"
    if machine in {"aarch64", "arm64"}:
        return "arm64"
    return None


def current_target(
    platform_name: str | None = None,
    machine_name: str | None = None,
    libc_name: str | None = None,
) -> str:
    platform_name = platform_name or sys.platform
    machine = _machine(machine_name)
    target = TARGETS.get((platform_name, machine or ""))
    if target is None:
        raise NpmBinaryError(
            f"npm standalone builds do not support {platform_name}/{machine_name or platform.machine()}"
        )
    if platform_name == "linux":
        libc = libc_name or platform.libc_ver()[0]
        if libc != "glibc":
            raise NpmBinaryError("npm standalone Linux builds currently require glibc")
    return target


def project_version() -> str:
    try:
        version = importlib.metadata.version(DISTRIBUTION_NAME)
    except importlib.metadata.PackageNotFoundError as error:
        raise NpmBinaryError(
            f"install {DISTRIBUTION_NAME} before building its standalone binary"
        ) from error
    return version


def pyinstaller_command(entry_script: Path, output_dir: Path, work_dir: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--console",
        "--name",
        EXECUTABLE_NAME,
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
        str(entry_script),
    ]


def _validate_magic(executable: Path, target: str) -> None:
    if not executable.is_file() or executable.is_symlink():
        raise NpmBinaryError(f"missing regular executable: {executable}")
    with executable.open("rb") as stream:
        prefix = stream.read(4)
    os_name = "darwin" if target.startswith("darwin-") else "linux"
    if not any(prefix.startswith(magic) for magic in MAGIC_PREFIXES[os_name]):
        raise NpmBinaryError(f"executable does not match target {target}")


def validate_executable(executable: Path, target: str, version: str) -> None:
    _validate_magic(executable, target)
    version_result = subprocess.run(
        [str(executable), "--version"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    if f"omm {version}" not in f"{version_result.stdout}\n{version_result.stderr}":
        raise NpmBinaryError("standalone command reported the wrong version")
    help_result = subprocess.run(
        [str(executable), "--help"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    if "Example usage:" not in f"{help_result.stdout}\n{help_result.stderr}":
        raise NpmBinaryError("standalone command help probe failed")


def build(output_dir: Path, expected_target: str | None = None) -> Path:
    target = current_target()
    if expected_target is not None and expected_target != target:
        raise NpmBinaryError(
            f"runner target is {target}, workflow expected {expected_target}"
        )
    version = project_version()
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="omm-npm-binary-") as temporary:
        work_dir = Path(temporary)
        entry_script = work_dir / "omm_entry.py"
        entry_script.write_text(
            "from omm.cli import main\n\nif __name__ == '__main__':\n    main()\n",
            encoding="utf-8",
        )
        subprocess.run(
            pyinstaller_command(entry_script, output_dir, work_dir),
            check=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
    executable = output_dir / EXECUTABLE_NAME
    executable.chmod(0o755)
    validate_executable(executable, target, version)
    return executable


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("target")
    commands.add_parser("version")
    build_parser = commands.add_parser("build")
    build_parser.add_argument("--output-dir", type=Path, required=True)
    build_parser.add_argument("--expected-target")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "target":
        print(current_target())
    elif args.command == "version":
        print(project_version())
    elif args.command == "build":
        build(args.output_dir, args.expected_target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
