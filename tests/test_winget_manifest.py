from __future__ import annotations

from pathlib import Path

import pytest

from scripts import winget_manifest


def _archive(tmp_path: Path, version: str = "1.2.3") -> Path:
    executable = tmp_path / "input.exe"
    executable.write_bytes(b"MZ portable")
    license_file = tmp_path / "LICENSE"
    license_file.write_text("MIT License\n", encoding="utf-8")
    archive, _ = winget_manifest.windows_portable.package_windows_portable(
        executable, license_file, version, tmp_path / "release"
    )
    return archive


def test_manifest_set_is_exact_and_uses_the_release_archive_hash(tmp_path):
    archive = _archive(tmp_path)
    url = winget_manifest.installer_url("1.2.3")

    manifest_dir = winget_manifest.write_manifest_set(
        "1.2.3", archive, "2026-08-20", url, tmp_path / "output"
    )

    assert manifest_dir == (
        tmp_path / "output/manifests/o/OmmHippo/OMM/1.2.3"
    )
    assert {path.name for path in manifest_dir.iterdir()} == {
        "OmmHippo.OMM.yaml",
        "OmmHippo.OMM.installer.yaml",
        "OmmHippo.OMM.locale.en-US.yaml",
    }
    installer = (manifest_dir / "OmmHippo.OMM.installer.yaml").read_text(
        encoding="utf-8"
    )
    assert "ManifestVersion: 1.12.0" in installer
    assert "InstallerType: zip" in installer
    assert "NestedInstallerType: portable" in installer
    assert "RelativeFilePath: omm.exe" in installer
    assert "PortableCommandAlias: omm" in installer
    assert "Scope: user" in installer
    assert f"InstallerUrl: {url}" in installer
    assert (
        f"InstallerSha256: {winget_manifest.windows_portable.sha256(archive).upper()}"
        in installer
    )


@pytest.mark.parametrize(
    ("digest", "release_date", "url", "message"),
    [
        ("0" * 63, "2026-08-20", winget_manifest.installer_url("1.2.3"), "64 hex"),
        ("0" * 64, "2026-8-20", winget_manifest.installer_url("1.2.3"), "date"),
        ("0" * 64, "2026-08-20", "https://example.com/omm.zip", "URL"),
    ],
)
def test_manifest_rejects_noncanonical_release_identity(
    digest, release_date, url, message
):
    with pytest.raises(winget_manifest.WingetManifestError, match=message):
        winget_manifest.manifest_contents("1.2.3", digest, release_date, url)


def test_manifest_writer_rejects_unexpected_existing_files(tmp_path):
    archive = _archive(tmp_path)
    target = tmp_path / "output/manifests/o/OmmHippo/OMM/1.2.3"
    target.mkdir(parents=True)
    (target / "unexpected.ps1").write_text("unsafe", encoding="utf-8")

    with pytest.raises(winget_manifest.WingetManifestError, match="unexpected"):
        winget_manifest.write_manifest_set(
            "1.2.3",
            archive,
            "2026-08-20",
            winget_manifest.installer_url("1.2.3"),
            tmp_path / "output",
        )
