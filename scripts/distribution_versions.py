#!/usr/bin/env python3
"""Verify that OMM's published package channels carry one release version."""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from release_artifacts import project_identity


PYPROJECT = ROOT / "pyproject.toml"
PYPI_JSON_URL = "https://pypi.org/pypi/omm-model/json"
HOMEBREW_FORMULA_URL = (
    "https://raw.githubusercontent.com/omm-hippo/homebrew-omm/main/Formula/omm.rb"
)
VERSION_PATTERN = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
OMM_ARCHIVE_PATTERN = re.compile(
    r"omm[_-]model[-_]([0-9]+\.[0-9]+\.[0-9]+)\.tar\.gz"
)
FETCH_TIMEOUT_SECONDS = 30
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class DistributionVersionError(RuntimeError):
    """Raised when a package channel cannot be verified or is out of sync."""


def _fetch(url: str) -> bytes:
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise DistributionVersionError(f"invalid release URL {url!r}") from error
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port not in (None, 443)
    ):
        raise DistributionVersionError("release URLs must be credential-free HTTPS")
    request = urllib.request.Request(url, headers={"User-Agent": "omm-release-check"})
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
            content = response.read(MAX_RESPONSE_BYTES + 1)
            if len(content) > MAX_RESPONSE_BYTES:
                raise DistributionVersionError(f"response from {url} is too large")
            return content
    except (OSError, urllib.error.URLError) as error:
        raise DistributionVersionError(f"could not fetch {url}: {error}") from error


def pypi_version(url: str = PYPI_JSON_URL) -> str:
    try:
        payload = json.loads(_fetch(url).decode("utf-8"))
        version = payload["info"]["version"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise DistributionVersionError(f"invalid PyPI metadata from {url}") from error
    if not isinstance(version, str) or VERSION_PATTERN.fullmatch(version) is None:
        raise DistributionVersionError(f"invalid PyPI version from {url}: {version!r}")
    return version


def homebrew_version(url: str = HOMEBREW_FORMULA_URL) -> str:
    try:
        formula = _fetch(url).decode("utf-8")
    except UnicodeDecodeError as error:
        raise DistributionVersionError(f"Homebrew formula is not UTF-8: {url}") from error

    versions = set(OMM_ARCHIVE_PATTERN.findall(formula))
    if len(versions) != 1:
        raise DistributionVersionError(
            f"could not identify exactly one OMM version in Homebrew formula {url}; "
            f"found {sorted(versions)}"
        )
    return versions.pop()


def local_version(pyproject: Path = PYPROJECT) -> str:
    _, version = project_identity(pyproject)
    return version


def check_versions(
    *,
    expected_version: str | None = None,
    pyproject: Path = PYPROJECT,
    pypi_url: str = PYPI_JSON_URL,
    homebrew_url: str = HOMEBREW_FORMULA_URL,
) -> dict[str, str]:
    expected = expected_version or local_version(pyproject)
    if VERSION_PATTERN.fullmatch(expected) is None:
        raise DistributionVersionError(f"invalid expected version: {expected!r}")

    # PyPI and Homebrew are independent network fetches; run them concurrently
    # rather than paying both round-trip latencies back to back.
    with ThreadPoolExecutor(max_workers=2) as executor:
        pypi_future = executor.submit(pypi_version, pypi_url)
        homebrew_future = executor.submit(homebrew_version, homebrew_url)
        observed = {
            "local": expected,
            "PyPI": pypi_future.result(),
            "Homebrew": homebrew_future.result(),
        }
    mismatches = {
        channel: version for channel, version in observed.items() if version != expected
    }
    if mismatches:
        details = ", ".join(f"{channel}={version}" for channel, version in observed.items())
        raise DistributionVersionError(
            f"distribution versions are not synchronized ({details}); "
            f"expected {expected}"
        )
    return observed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--expected-version",
        help="version to require; defaults to pyproject.toml [project].version",
    )
    parser.add_argument("--pyproject", type=Path, default=PYPROJECT)
    parser.add_argument("--pypi-url", default=PYPI_JSON_URL)
    parser.add_argument("--homebrew-url", default=HOMEBREW_FORMULA_URL)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        versions = check_versions(
            expected_version=args.expected_version,
            pyproject=args.pyproject,
            pypi_url=args.pypi_url,
            homebrew_url=args.homebrew_url,
        )
    except (OSError, DistributionVersionError) as error:
        print(f"distribution version check failed: {error}", file=sys.stderr)
        return 1
    print(
        "Distribution versions synchronized: "
        + ", ".join(f"{channel}={version}" for channel, version in versions.items())
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
