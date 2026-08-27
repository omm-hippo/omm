from __future__ import annotations

import importlib.util
import io
import re
import stat
import tarfile
import zipfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "release_artifacts", ROOT / "scripts" / "release_artifacts.py"
)
assert SPEC is not None and SPEC.loader is not None
release_artifacts = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_artifacts)


def test_project_identity_reads_literal_name_and_version(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[build-system]\nrequires = []\n\n[project]\nname = "example-cli"\nversion = "1.2.3"\n',
        encoding="utf-8",
    )

    assert release_artifacts.project_identity(pyproject) == ("example-cli", "1.2.3")


def test_release_tag_must_match_project_version_exactly():
    release_artifacts.validate_tag("v1.2.3", "1.2.3")

    with pytest.raises(release_artifacts.ReleaseValidationError):
        release_artifacts.validate_tag("1.2.3", "1.2.3")
    with pytest.raises(release_artifacts.ReleaseValidationError):
        release_artifacts.validate_tag("v1.2.4", "1.2.3")


def test_release_smoke_uses_windows_venv_executable_names():
    root = Path("C:/temp/release")

    assert release_artifacts._venv_executable(root, "python", "nt") == (
        root / "Scripts" / "python.exe"
    )
    assert release_artifacts._venv_executable(root, "omm", "nt") == (
        root / "Scripts" / "omm.exe"
    )


def test_checksum_manifest_detects_archive_changes(tmp_path):
    wheel = tmp_path / "example-1.0-py3-none-any.whl"
    sdist = tmp_path / "example-1.0.tar.gz"
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"sdist")

    release_artifacts.write_checksums(tmp_path)
    release_artifacts.validate_checksums(tmp_path)

    wheel.write_bytes(b"changed")
    with pytest.raises(release_artifacts.ReleaseValidationError):
        release_artifacts.validate_checksums(tmp_path)


def test_release_bundle_rejects_unexpected_files(tmp_path):
    wheel = tmp_path / "example-1.0-py3-none-any.whl"
    sdist = tmp_path / "example-1.0.tar.gz"
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"sdist")
    (tmp_path / "unexpected-installer.exe").write_bytes(b"unexpected")

    with pytest.raises(release_artifacts.ReleaseValidationError):
        release_artifacts.write_checksums(tmp_path)


def test_sdist_identity_rejects_nested_pkg_info(tmp_path):
    sdist = tmp_path / "example-1.0.tar.gz"
    payload = b"Name: example\nVersion: 1.0\n\n"
    with tarfile.open(sdist, "w:gz") as archive:
        member = tarfile.TarInfo("example-1.0/nested/PKG-INFO")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))

    with pytest.raises(release_artifacts.ReleaseValidationError, match="top-level"):
        release_artifacts._sdist_identity(sdist)


def test_wheel_identity_accepts_standard_permission_only_mode(tmp_path):
    wheel = tmp_path / "example-1.0-py3-none-any.whl"
    metadata = zipfile.ZipInfo("example-1.0.dist-info/METADATA")
    metadata.create_system = 3
    metadata.external_attr = 0o644 << 16
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(metadata, b"Name: example\nVersion: 1.0\n\n")

    assert release_artifacts._wheel_identity(wheel) == ("example", "1.0")


def test_wheel_identity_rejects_a_symlink_member(tmp_path):
    wheel = tmp_path / "example-1.0-py3-none-any.whl"
    metadata = zipfile.ZipInfo("example-1.0.dist-info/METADATA")
    metadata.create_system = 3
    metadata.external_attr = 0o644 << 16
    linked = zipfile.ZipInfo("example/link")
    linked.create_system = 3
    linked.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(metadata, b"Name: example\nVersion: 1.0\n\n")
        archive.writestr(linked, b"../../outside")

    with pytest.raises(release_artifacts.ReleaseValidationError, match="unsafe member"):
        release_artifacts._wheel_identity(wheel)


def test_sdist_identity_rejects_links(tmp_path):
    sdist = tmp_path / "example-1.0.tar.gz"
    payload = b"Name: example\nVersion: 1.0\n\n"
    with tarfile.open(sdist, "w:gz") as archive:
        metadata = tarfile.TarInfo("example-1.0/PKG-INFO")
        metadata.size = len(payload)
        archive.addfile(metadata, io.BytesIO(payload))
        linked = tarfile.TarInfo("example-1.0/link")
        linked.type = tarfile.SYMTYPE
        linked.linkname = "../../outside"
        archive.addfile(linked)

    with pytest.raises(release_artifacts.ReleaseValidationError, match="unsafe member"):
        release_artifacts._sdist_identity(sdist)


def test_sdist_identity_rejects_duplicate_members(tmp_path):
    sdist = tmp_path / "example-1.0.tar.gz"
    payload = b"Name: example\nVersion: 1.0\n\n"
    with tarfile.open(sdist, "w:gz") as archive:
        metadata = tarfile.TarInfo("example-1.0/PKG-INFO")
        metadata.size = len(payload)
        archive.addfile(metadata, io.BytesIO(payload))
        first = tarfile.TarInfo("example-1.0/module.py")
        first.size = 1
        archive.addfile(first, io.BytesIO(b"a"))
        duplicate = tarfile.TarInfo("example-1.0/module.py")
        duplicate.size = 1
        archive.addfile(duplicate, io.BytesIO(b"b"))

    with pytest.raises(release_artifacts.ReleaseValidationError, match="duplicate path"):
        release_artifacts._sdist_identity(sdist)


def test_archives_reject_paths_that_collide_after_normalization(tmp_path):
    payload = b"Name: example\nVersion: 1.0\n\n"
    wheel = tmp_path / "example-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("example-1.0.dist-info/METADATA", payload)
        archive.writestr("example/module.py", b"a")
        archive.writestr("example//module.py", b"b")
    with pytest.raises(release_artifacts.ReleaseValidationError, match="unsafe path"):
        release_artifacts._wheel_identity(wheel)

    sdist = tmp_path / "example-1.0.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        metadata = tarfile.TarInfo("example-1.0/PKG-INFO")
        metadata.size = len(payload)
        archive.addfile(metadata, io.BytesIO(payload))
        for name, content in (
            ("example-1.0/module.py", b"a"),
            ("example-1.0//module.py", b"b"),
        ):
            member = tarfile.TarInfo(name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    with pytest.raises(release_artifacts.ReleaseValidationError, match="unsafe path"):
        release_artifacts._sdist_identity(sdist)


def test_wheel_identity_rejects_oversized_metadata(tmp_path):
    wheel = tmp_path / "example-1.0-py3-none-any.whl"
    metadata = zipfile.ZipInfo("example-1.0.dist-info/METADATA")
    metadata.create_system = 3
    metadata.external_attr = 0o644 << 16
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(metadata, b"x" * (release_artifacts.MAX_METADATA_BYTES + 1))

    with pytest.raises(release_artifacts.ReleaseValidationError, match="size limit"):
        release_artifacts._wheel_identity(wheel)


def test_checksum_manifest_rejects_unexpected_entries(tmp_path):
    wheel = tmp_path / "example-1.0-py3-none-any.whl"
    sdist = tmp_path / "example-1.0.tar.gz"
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"sdist")
    checksum_file = release_artifacts.write_checksums(tmp_path)
    checksum_file.write_text(
        checksum_file.read_text(encoding="utf-8")
        + ("0" * 64)
        + "  ..\\unexpected-installer.exe\n",
        encoding="utf-8",
    )

    with pytest.raises(release_artifacts.ReleaseValidationError):
        release_artifacts.validate_checksums(tmp_path)


def test_release_workflow_builds_smoke_installs_and_gates_publishing():
    workflow_path = ROOT / ".github" / "workflows" / "release.yml"
    if not workflow_path.is_file():
        pytest.skip("GitHub workflow files are excluded from the Docker build context")
    workflow = workflow_path.read_text(encoding="utf-8")

    assert "python -m build" in workflow
    assert "python -m twine check dist/*.whl dist/*.tar.gz" in workflow
    assert "scripts/release_artifacts.py check-tag" in workflow
    assert "scripts/distribution_versions.py" in workflow
    assert "git merge-base --is-ancestor" in workflow
    assert "scripts/release_artifacts.py smoke-install" in workflow
    assert "ubuntu-latest, macos-latest, windows-latest" in workflow
    assert 'python: ["3.10", "3.14"]' in workflow
    assert re.search(r"(?m)^  release-tests:\n(?:    .*\n)*?    needs: smoke-install$", workflow)
    assert "pypa/gh-action-pypi-publish@dc37677" in workflow
    assert "environment:\n      name: testpypi" in workflow
    assert "environment:\n      name: pypi" in workflow
    assert workflow.count("id-token: write") == 2
    assert workflow.count(
        "if: github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')"
    ) == 5
    assert "needs: [smoke-install, release-tests]" in workflow
    assert "needs: publish-testpypi" in workflow
    assert "needs: verify-testpypi" in workflow
    assert "needs: publish-pypi" in workflow
    assert "needs: verify-pypi-files" in workflow
    assert "name: Verify PyPI and Homebrew version sync" in workflow
    assert re.search(
        r"(?m)^  sync-homebrew:\n(?:    .*\n)*?    needs: verify-pypi-install$",
        workflow,
    )
    assert "secrets.HOMEBREW_TAP_DISPATCH_TOKEN" in workflow
    assert '"repos/omm-hippo/homebrew-omm/dispatches"' in workflow
    assert "event_type=pypi_release_verified" in workflow
    assert "client_payload[source_sha]=${GITHUB_SHA}" in workflow
    assert "--staging" not in workflow
    assert "python-release-packages" in workflow
    assert "python-release-dist" in workflow
    assert "persist-credentials: false" in workflow
    assert "cache: pip" not in workflow
    assert not re.search(r"uses:\s+[^\s#]+@v\d", workflow)


def test_release_build_dependencies_are_pinned():
    requirements = (ROOT / "requirements-release.txt").read_text(encoding="utf-8").splitlines()
    build_system = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert requirements == ["build==1.5.0", "twine==7.0.0"]
    assert 'requires = ["hatchling==1.32.0"]' in build_system
