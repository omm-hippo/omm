from __future__ import annotations

import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import npm_package
import npm_release


def _magic(target: str) -> bytes:
    os_name = npm_package.targets()[target]["os"]
    return next(iter(npm_package.MAGIC_PREFIXES[os_name]))


def _pack(source: Path, destination: Path) -> None:
    subprocess.run(
        ["npm", "pack", str(source), "--pack-destination", str(destination), "--json"],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _bundle(tmp_path: Path) -> Path:
    stage = tmp_path / "stage"
    pack = tmp_path / "pack"
    pack.mkdir()
    launcher = npm_package.stage_launcher(stage, publishable=True)
    _pack(launcher, pack)
    for target in npm_package.targets():
        binary = tmp_path / f"binary-{target}"
        binary.write_bytes(_magic(target) + b" OMM standalone")
        platform_package = npm_package.stage_platform_package(
            target,
            binary,
            stage,
            publishable=True,
        )
        _pack(platform_package, pack)
    return pack


def test_bundle_has_every_publishable_package_and_exact_checksums(tmp_path):
    pack = _bundle(tmp_path)

    packages = npm_release.verify_bundle(pack, write_checksums=True)
    npm_release.verify_bundle(pack)

    assert {package.name for package in packages} == {
        npm_package.LAUNCHER_NAME,
        *(target["package"] for target in npm_package.targets().values()),
    }
    checksum_lines = (pack / npm_release.CHECKSUMS_NAME).read_text(
        encoding="ascii"
    ).splitlines()
    assert len(checksum_lines) == len(packages)
    assert all("  " in line and line.endswith(".tgz") for line in checksum_lines)


def test_bundle_rejects_unexpected_file_and_private_tarball(tmp_path):
    pack = _bundle(tmp_path)
    npm_release.verify_bundle(pack, write_checksums=True)
    (pack / "unexpected.exe").write_bytes(b"MZ")
    with pytest.raises(npm_release.NpmReleaseError, match="unexpected files"):
        npm_release.verify_bundle(pack)

    (pack / "unexpected.exe").unlink()
    (pack / "unexpected-directory").mkdir()
    with pytest.raises(npm_release.NpmReleaseError, match="unexpected files"):
        npm_release.verify_bundle(pack)

    (pack / "unexpected-directory").rmdir()
    private_stage = tmp_path / "private"
    private_pack = tmp_path / "private-pack"
    private_pack.mkdir()
    _pack(npm_package.stage_launcher(private_stage), private_pack)
    private_tarball = next(private_pack.glob("*.tgz"))
    with pytest.raises(npm_release.NpmReleaseError, match="not explicitly publishable"):
        npm_release.inspect_tarball(private_tarball)


def test_tarball_rejects_path_traversal(tmp_path):
    archive = tmp_path / "bad.tgz"
    source = tmp_path / "payload"
    source.write_text("bad", encoding="utf-8")
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(source, arcname="package/../outside")

    with pytest.raises(npm_release.NpmReleaseError, match="unsafe npm tar member"):
        npm_release.inspect_tarball(archive)


def test_existing_registry_package_must_have_identical_bytes(tmp_path, monkeypatch):
    pack = _bundle(tmp_path)
    npm_release.verify_bundle(pack, write_checksums=True)
    monkeypatch.setattr(npm_release, "_registry_integrity", lambda package, registry: "sha512-wrong")

    with pytest.raises(npm_release.NpmReleaseError, match="different bytes"):
        npm_release.publish_bundle(pack)
