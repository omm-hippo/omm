#!/usr/bin/env python3
"""Destination-side verification for OMM's TestPyPI and PyPI releases."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import venv
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from release_artifacts import (
    PYPROJECT,
    ReleaseValidationError,
    distribution_archives,
    project_identity,
    validate_checksums,
)


ROOT = Path(__file__).resolve().parents[1]
SUBPROCESS_TIMEOUT_SECONDS = 300
REPOSITORY_URL = "https://github.com/omm-hippo/omm"
INDEX_INSTALL_ATTEMPTS = 18
INDEX_INSTALL_DELAY_SECONDS = 10.0


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def release_json_url(repository_url: str, name: str, version: str) -> str:
    base = repository_url.rstrip("/")
    return (
        f"{base}/pypi/{urllib.parse.quote(name, safe='')}/"
        f"{urllib.parse.quote(version, safe='')}/json"
    )


def integrity_provenance_url(
    repository_url: str, name: str, version: str, filename: str
) -> str:
    base = repository_url.rstrip("/")
    return (
        f"{base}/integrity/{urllib.parse.quote(name, safe='')}/"
        f"{urllib.parse.quote(version, safe='')}/"
        f"{urllib.parse.quote(filename, safe='')}/provenance"
    )


def fetch_release_json(
    repository_url: str,
    name: str,
    version: str,
    *,
    attempts: int = 12,
    delay_seconds: float = 10,
) -> dict:
    url = release_json_url(repository_url, name, version)
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                return json.load(response)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as error:
            last_error = error
            if attempt + 1 < attempts:
                time.sleep(delay_seconds)
    raise ReleaseValidationError(
        f"release metadata did not become available at {url}: {last_error}"
    )


def fetch_url_bytes(
    url: str,
    *,
    attempts: int = 12,
    delay_seconds: float = 10,
) -> bytes:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                return response.read()
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as error:
            last_error = error
            if attempt + 1 < attempts:
                time.sleep(delay_seconds)
    raise ReleaseValidationError(
        f"repository content did not become available at {url}: {last_error}"
    )


def verify_repository_files(
    repository_url: str,
    dist_dir: Path,
    pyproject: Path = PYPROJECT,
    *,
    attempts: int = 12,
    delay_seconds: float = 10,
) -> list[str]:
    name, version = project_identity(pyproject)
    validate_checksums(dist_dir)
    archives = distribution_archives(dist_dir)
    expected = {path.name: _sha256(path) for path in archives}
    payload = fetch_release_json(
        repository_url,
        name,
        version,
        attempts=attempts,
        delay_seconds=delay_seconds,
    )
    remote = {item.get("filename"): item for item in payload.get("urls", [])}
    if set(remote) != set(expected):
        raise ReleaseValidationError(
            f"repository contains {sorted(remote)}, expected exactly {sorted(expected)}"
        )

    def _verify_one(entry: tuple[str, str]) -> str:
        filename, expected_hash = entry
        item = remote[filename]
        metadata_hash = item.get("digests", {}).get("sha256")
        if metadata_hash != expected_hash:
            raise ReleaseValidationError(
                f"repository SHA-256 for {filename} is {metadata_hash}, expected {expected_hash}"
            )
        download_url = item.get("url")
        if not isinstance(download_url, str) or not download_url.startswith("https://"):
            raise ReleaseValidationError(f"repository returned an invalid URL for {filename}")
        digest = hashlib.sha256()
        with urllib.request.urlopen(download_url, timeout=60) as response:
            for block in iter(lambda: response.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != expected_hash:
            raise ReleaseValidationError(f"downloaded bytes do not match {filename}")
        return download_url

    # Each archive is an independent full-file download+hash; run them
    # concurrently instead of paying every download's latency back to back.
    ordered_items = sorted(expected.items())
    with ThreadPoolExecutor(max_workers=max(1, len(ordered_items))) as executor:
        verified_urls = list(executor.map(_verify_one, ordered_items))
    return verified_urls


def _venv_executable(root: Path, name: str, platform_name: str = os.name) -> Path:
    scripts = root / ("Scripts" if platform_name == "nt" else "bin")
    suffix = ".exe" if platform_name == "nt" else ""
    return scripts / f"{name}{suffix}"


def _bin_executable(root: Path, name: str, platform_name: str = os.name) -> Path:
    suffix = ".exe" if platform_name == "nt" else ""
    return root / f"{name}{suffix}"


def _run_index_install_with_retry(
    command: list[str],
    *,
    attempts: int = INDEX_INSTALL_ATTEMPTS,
    delay_seconds: float = INDEX_INSTALL_DELAY_SECONDS,
) -> None:
    if attempts < 1:
        raise ValueError("attempts must be at least 1")

    for attempt in range(1, attempts + 1):
        try:
            subprocess.run(
                command,
                check=True,
                timeout=SUBPROCESS_TIMEOUT_SECONDS,
            )
            return
        except subprocess.CalledProcessError:
            if attempt == attempts:
                raise
            print(
                "package index install did not succeed; "
                f"retrying in {delay_seconds:g}s "
                f"({attempt}/{attempts})",
                file=sys.stderr,
            )
            time.sleep(delay_seconds)


def smoke_index_install(
    index_url: str, dist_dir: Path, pyproject: Path = PYPROJECT
) -> None:
    name, version = project_identity(pyproject)
    validate_checksums(dist_dir)
    wheel, _ = distribution_archives(dist_dir)
    with tempfile.TemporaryDirectory(prefix="omm-index-smoke-") as temporary:
        root = Path(temporary)
        environment = root / "venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(environment)
        python = _venv_executable(environment, "python")
        common = [
            str(python),
            "-m",
            "pip",
            "--disable-pip-version-check",
        ]
        subprocess.run(
            [*common, "install", "--no-input", "--no-cache-dir", str(wheel)],
            check=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
        subprocess.run(
            [*common, "uninstall", "--yes", name],
            check=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
        _run_index_install_with_retry(
            [
                *common,
                "install",
                "--no-input",
                "--no-cache-dir",
                "--no-deps",
                "--index-url",
                index_url,
                f"{name}=={version}",
            ],
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
        version_result = subprocess.run(
            [str(command), "--version"],
            check=True,
            env=command_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
        expected_version = f"omm {version}"
        if version_result.stdout.strip() != expected_version:
            raise ReleaseValidationError(
                "installed TestPyPI command reported "
                f"{version_result.stdout.strip()!r}, expected {expected_version!r}"
            )
        subprocess.run(
            [str(command), "help"],
            check=True,
            env=command_env,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )


def verify_attestations(
    repository_url: str,
    dist_dir: Path,
    pyproject: Path = PYPROJECT,
    *,
    source_repository: str = REPOSITORY_URL,
) -> None:
    executable = shutil.which("pypi-attestations")
    if executable is None:
        raise ReleaseValidationError("pypi-attestations is not installed")
    name, version = project_identity(pyproject)
    verify_repository_files(repository_url, dist_dir, pyproject)
    archives = distribution_archives(dist_dir)
    with tempfile.TemporaryDirectory(prefix="omm-provenance-") as temporary:
        provenance_dir = Path(temporary)
        for archive in archives:
            provenance_url = integrity_provenance_url(
                repository_url,
                name,
                version,
                archive.name,
            )
            provenance_path = provenance_dir / f"{archive.name}.provenance"
            provenance_path.write_bytes(fetch_url_bytes(provenance_url))
            # TestPyPI is a staging package index, but the official publishing
            # action signs its attestations with the public Sigstore service.
            # pypi-attestations' --staging flag selects Sigstore's staging
            # trust root instead, so both indexes must use the default here.
            command = [
                executable,
                "verify",
                "pypi",
                "--repository",
                source_repository,
                "--provenance-file",
                str(provenance_path),
                str(archive),
            ]
            subprocess.run(command, check=True, timeout=SUBPROCESS_TIMEOUT_SECONDS)


def pipx_smoke(index_url: str, pyproject: Path = PYPROJECT) -> None:
    name, version = project_identity(pyproject)
    with tempfile.TemporaryDirectory(prefix="omm-pipx-smoke-") as temporary:
        root = Path(temporary)
        bin_dir = root / "bin"
        environment = os.environ.copy()
        environment.update(
            {
                "PIPX_HOME": str(root / "pipx-home"),
                "PIPX_BIN_DIR": str(bin_dir),
                "PIPX_MAN_DIR": str(root / "man"),
                "OMM_HOME": str(root / "omm-home"),
            }
        )
        package = f"{name}=={version}"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pipx",
                "install",
                "--force",
                "--pip-args",
                f"--no-cache-dir --index-url {index_url}",
                package,
            ],
            check=True,
            env=environment,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
        command = _bin_executable(bin_dir, "omm")
        version_result = subprocess.run(
            [str(command), "--version"],
            check=True,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
        expected_version = f"omm {version}"
        if version_result.stdout.strip() != expected_version:
            raise ReleaseValidationError(
                "installed PyPI command reported "
                f"{version_result.stdout.strip()!r}, expected {expected_version!r}"
            )
        subprocess.run(
            [str(command), "help"],
            check=True,
            env=environment,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
        subprocess.run(
            [sys.executable, "-m", "pipx", "uninstall", name],
            check=True,
            env=environment,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
        # Path.exists() follows symlinks and therefore returns False for a
        # broken launcher left behind after uninstall.  lexists() verifies
        # that no directory entry remains, including a dangling symlink.
        if os.path.lexists(command):
            raise ReleaseValidationError(f"pipx uninstall left {command} behind")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    for command in ("verify-index", "smoke-index-install", "verify-attestations"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("--repository-url", required=True)
        command_parser.add_argument("--dist-dir", type=Path, default=ROOT / "dist")
        if command == "smoke-index-install":
            command_parser.add_argument("--index-url", required=True)

    pipx_parser = subparsers.add_parser("pipx-smoke")
    pipx_parser.add_argument("--index-url", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "verify-index":
            for url in verify_repository_files(args.repository_url, args.dist_dir):
                print(url)
        elif args.command == "smoke-index-install":
            smoke_index_install(args.index_url, args.dist_dir)
        elif args.command == "verify-attestations":
            verify_attestations(
                args.repository_url,
                args.dist_dir,
            )
        elif args.command == "pipx-smoke":
            pipx_smoke(args.index_url)
        else:  # pragma: no cover - argparse constrains the command
            raise AssertionError(args.command)
    except (OSError, ReleaseValidationError, subprocess.CalledProcessError) as error:
        print(f"PyPI release verification failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
