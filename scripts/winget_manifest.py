#!/usr/bin/env python3
"""Generate the exact multi-file WinGet manifest for an OMM portable release."""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

try:
    from . import windows_portable
except ImportError:  # Direct execution from the scripts directory.
    import windows_portable  # type: ignore[no-redef]


PACKAGE_IDENTIFIER = "OmmHippo.OMM"
# The community repository's current manifest submission template requires 1.12.
MANIFEST_VERSION = "1.12.0"
_SHA256_PATTERN = re.compile(r"[0-9A-Fa-f]{64}")


class WingetManifestError(RuntimeError):
    """Raised when a WinGet manifest would not identify the exact OMM release."""


def installer_url(version: str) -> str:
    windows_portable._version_parts(version)
    return (
        "https://github.com/omm-hippo/omm/releases/download/"
        f"v{version}/omm-windows-x64-{version}.zip"
    )


def _validate_release_date(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise WingetManifestError(f"invalid release date: {value!r}") from error
    if parsed.isoformat() != value:
        raise WingetManifestError(f"release date is not canonical ISO format: {value!r}")
    return value


def manifest_contents(
    version: str,
    installer_sha256: str,
    release_date: str,
    release_installer_url: str,
) -> dict[str, str]:
    windows_portable._version_parts(version)
    if _SHA256_PATTERN.fullmatch(installer_sha256) is None:
        raise WingetManifestError("installer SHA-256 must contain exactly 64 hex digits")
    expected_url = installer_url(version)
    if release_installer_url != expected_url:
        raise WingetManifestError(
            f"installer URL is {release_installer_url!r}, expected {expected_url!r}"
        )
    _validate_release_date(release_date)
    digest = installer_sha256.upper()

    version_manifest = f"""# yaml-language-server: $schema=https://aka.ms/winget-manifest.version.{MANIFEST_VERSION}.schema.json

PackageIdentifier: {PACKAGE_IDENTIFIER}
PackageVersion: {version}
DefaultLocale: en-US
ManifestType: version
ManifestVersion: {MANIFEST_VERSION}
"""
    installer_manifest = f"""# yaml-language-server: $schema=https://aka.ms/winget-manifest.installer.{MANIFEST_VERSION}.schema.json

PackageIdentifier: {PACKAGE_IDENTIFIER}
PackageVersion: {version}
Commands:
- omm
ReleaseDate: {release_date}
Installers:
- Architecture: x64
  InstallerType: zip
  NestedInstallerType: portable
  NestedInstallerFiles:
  - RelativeFilePath: omm.exe
    PortableCommandAlias: omm
  InstallerUrl: {release_installer_url}
  InstallerSha256: {digest}
  UpgradeBehavior: install
ManifestType: installer
ManifestVersion: {MANIFEST_VERSION}
"""
    locale_manifest = f"""# yaml-language-server: $schema=https://aka.ms/winget-manifest.defaultLocale.{MANIFEST_VERSION}.schema.json

PackageIdentifier: {PACKAGE_IDENTIFIER}
PackageVersion: {version}
PackageLocale: en-US
Publisher: OMM Hippo
PublisherUrl: https://github.com/omm-hippo
PublisherSupportUrl: https://github.com/omm-hippo/omm/issues
Author: OMM contributors
PackageName: Open Model Manager
PackageUrl: https://github.com/omm-hippo/omm
License: MIT
LicenseUrl: https://github.com/omm-hippo/omm/blob/v{version}/LICENSE
ShortDescription: Manage local AI models across supported inference engines.
Description: Open Model Manager installs, links, and manages local AI models across supported inference engines from one command-line interface.
Moniker: omm
Tags:
- ai
- cli
- inference
- local-ai
- machine-learning
- model-manager
ReleaseNotesUrl: https://github.com/omm-hippo/omm/releases/tag/v{version}
ManifestType: defaultLocale
ManifestVersion: {MANIFEST_VERSION}
"""
    return {
        f"{PACKAGE_IDENTIFIER}.yaml": version_manifest,
        f"{PACKAGE_IDENTIFIER}.installer.yaml": installer_manifest,
        f"{PACKAGE_IDENTIFIER}.locale.en-US.yaml": locale_manifest,
    }


def write_manifest_set(
    version: str,
    archive: Path,
    release_date: str,
    release_installer_url: str,
    output_dir: Path,
) -> Path:
    windows_portable.verify_windows_archive(archive, version)
    contents = manifest_contents(
        version,
        windows_portable.sha256(archive),
        release_date,
        release_installer_url,
    )
    target = output_dir / "manifests" / "o" / "OmmHippo" / "OMM" / version
    if target.is_symlink():
        raise WingetManifestError(f"manifest directory must not be a symlink: {target}")
    if target.exists():
        existing = {path.name for path in target.iterdir()}
        unexpected = existing - set(contents)
        if unexpected:
            raise WingetManifestError(
                f"manifest directory contains unexpected entries: {sorted(unexpected)!r}"
            )
        for path in target.iterdir():
            if not path.is_file() or path.is_symlink():
                raise WingetManifestError(f"unsafe existing manifest path: {path}")
    target.mkdir(parents=True, exist_ok=True)
    for name, content in contents.items():
        (target / name).write_text(content, encoding="utf-8", newline="\n")
    verify_manifest_set(
        target,
        version,
        windows_portable.sha256(archive),
        release_date,
        release_installer_url,
    )
    return target


def verify_manifest_set(
    manifest_dir: Path,
    version: str,
    installer_sha256: str,
    release_date: str,
    release_installer_url: str,
) -> None:
    expected = manifest_contents(
        version, installer_sha256, release_date, release_installer_url
    )
    if not manifest_dir.is_dir() or manifest_dir.is_symlink():
        raise WingetManifestError(f"missing regular manifest directory: {manifest_dir}")
    actual_names = {path.name for path in manifest_dir.iterdir()}
    if actual_names != set(expected):
        raise WingetManifestError(
            f"manifest set contains {sorted(actual_names)!r}, expected {sorted(expected)!r}"
        )
    for name, content in expected.items():
        path = manifest_dir / name
        if not path.is_file() or path.is_symlink():
            raise WingetManifestError(f"unsafe manifest path: {path}")
        if path.read_text(encoding="utf-8") != content:
            raise WingetManifestError(f"manifest content does not match: {path}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--release-date", required=True)
    parser.add_argument("--installer-url", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        print(
            write_manifest_set(
                args.version,
                args.archive,
                args.release_date,
                args.installer_url,
                args.output_dir,
            )
        )
    except (
        OSError,
        ValueError,
        WingetManifestError,
        windows_portable.WindowsPortableError,
    ) as error:
        print(f"WinGet manifest generation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
