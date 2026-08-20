from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("pypi_release", SCRIPTS / "pypi_release.py")
assert SPEC is not None and SPEC.loader is not None
pypi_release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pypi_release)


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def test_release_json_url_quotes_distribution_identity():
    assert pypi_release.release_json_url(
        "https://test.pypi.org/", "omm-model", "1.2.3+test"
    ) == "https://test.pypi.org/pypi/omm-model/1.2.3%2Btest/json"


def test_integrity_provenance_url_quotes_distribution_identity():
    assert pypi_release.integrity_provenance_url(
        "https://test.pypi.org/",
        "omm-model",
        "1.2.3+test",
        "omm model-1.2.3+test.tar.gz",
    ) == (
        "https://test.pypi.org/integrity/omm-model/1.2.3%2Btest/"
        "omm%20model-1.2.3%2Btest.tar.gz/provenance"
    )


def test_repository_verification_checks_metadata_and_downloaded_bytes(tmp_path, monkeypatch):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "omm-model"\nversion = "1.2.3"\n',
        encoding="utf-8",
    )
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    wheel = dist_dir / "omm_model-1.2.3-py3-none-any.whl"
    sdist = dist_dir / "omm_model-1.2.3.tar.gz"
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"sdist")
    release_artifacts = sys.modules["release_artifacts"]
    release_artifacts.write_checksums(dist_dir)

    files = {wheel.name: wheel.read_bytes(), sdist.name: sdist.read_bytes()}
    urls = {name: f"https://files.example/{name}" for name in files}
    payload = {
        "urls": [
            {
                "filename": name,
                "url": urls[name],
                "digests": {"sha256": hashlib.sha256(data).hexdigest()},
            }
            for name, data in files.items()
        ]
    }

    def fake_urlopen(url, timeout):
        if url.endswith("/json"):
            return _Response(json.dumps(payload).encode())
        filename = url.rsplit("/", 1)[-1]
        return _Response(files[filename])

    monkeypatch.setattr(pypi_release.urllib.request, "urlopen", fake_urlopen)

    assert set(
        pypi_release.verify_repository_files(
            "https://test.pypi.org", dist_dir, pyproject, attempts=1
        )
    ) == set(urls.values())


def test_repository_verification_rejects_an_extra_remote_file(tmp_path, monkeypatch):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "example"\nversion = "1.0"\n', encoding="utf-8")
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    (dist_dir / "example-1.0-py3-none-any.whl").write_bytes(b"wheel")
    (dist_dir / "example-1.0.tar.gz").write_bytes(b"sdist")
    release_artifacts = sys.modules["release_artifacts"]
    release_artifacts.write_checksums(dist_dir)
    monkeypatch.setattr(
        pypi_release,
        "fetch_release_json",
        lambda *_args, **_kwargs: {
            "urls": [
                {"filename": "example-1.0-py3-none-any.whl"},
                {"filename": "example-1.0.tar.gz"},
                {"filename": "unexpected.exe"},
            ]
        },
    )

    with pytest.raises(pypi_release.ReleaseValidationError):
        pypi_release.verify_repository_files(
            "https://pypi.org", dist_dir, pyproject, attempts=1
        )


def test_attestation_verification_uses_local_archives_and_repository_provenance(
    tmp_path, monkeypatch
):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "omm-model"\nversion = "1.2.3"\n',
        encoding="utf-8",
    )
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    wheel = dist_dir / "omm_model-1.2.3-py3-none-any.whl"
    sdist = dist_dir / "omm_model-1.2.3.tar.gz"
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"sdist")
    release_artifacts = sys.modules["release_artifacts"]
    release_artifacts.write_checksums(dist_dir)

    monkeypatch.setattr(
        pypi_release,
        "verify_repository_files",
        lambda *_args, **_kwargs: [
            "https://test-files.pythonhosted.org/wheel",
            "https://test-files.pythonhosted.org/sdist",
        ],
    )
    monkeypatch.setattr(
        pypi_release.shutil,
        "which",
        lambda _name: "/tools/pypi-attestations",
    )
    fetched: list[str] = []

    def fake_fetch(url, **_kwargs):
        fetched.append(url)
        return b"provenance"

    commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        commands.append(command)
        assert kwargs == {
            "check": True,
            "timeout": pypi_release.SUBPROCESS_TIMEOUT_SECONDS,
        }
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(pypi_release, "fetch_url_bytes", fake_fetch)
    monkeypatch.setattr(pypi_release.subprocess, "run", fake_run)

    pypi_release.verify_attestations(
        "https://test.pypi.org",
        dist_dir,
        pyproject,
    )

    assert fetched == [
        "https://test.pypi.org/integrity/omm-model/1.2.3/"
        "omm_model-1.2.3-py3-none-any.whl/provenance",
        "https://test.pypi.org/integrity/omm-model/1.2.3/"
        "omm_model-1.2.3.tar.gz/provenance",
    ]
    assert [command[-1] for command in commands] == [str(wheel), str(sdist)]
    for command, archive in zip(commands, (wheel, sdist), strict=True):
        assert command[:6] == [
            "/tools/pypi-attestations",
            "verify",
            "pypi",
            "--repository",
            "https://github.com/omm-hippo/omm",
            "--provenance-file",
        ]
        assert command[6].endswith(f"{archive.name}.provenance")
        assert "--staging" not in command
        assert "test-files.pythonhosted.org" not in " ".join(command)


def test_windows_executable_paths_are_explicit():
    root = Path("C:/temp/release")

    assert pypi_release._venv_executable(root, "python", "nt") == (
        root / "Scripts" / "python.exe"
    )
    assert pypi_release._bin_executable(root, "omm", "nt") == root / "omm.exe"


def test_pipx_smoke_rejects_a_dangling_launcher(tmp_path, monkeypatch):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "omm-model"\nversion = "1.2.3"\n',
        encoding="utf-8",
    )
    launcher = tmp_path / "omm"

    monkeypatch.setattr(
        pypi_release.tempfile,
        "TemporaryDirectory",
        lambda **_kwargs: nullcontext(str(tmp_path)),
    )
    monkeypatch.setattr(pypi_release, "_bin_executable", lambda *_args: launcher)
    monkeypatch.setattr(
        pypi_release.os.path,
        "lexists",
        lambda path: Path(path) == launcher,
    )

    def fake_run(command, **_kwargs):
        if command == [str(launcher), "--version"]:
            return subprocess.CompletedProcess(command, 0, stdout="omm 1.2.3\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(pypi_release.subprocess, "run", fake_run)

    with pytest.raises(
        pypi_release.ReleaseValidationError,
        match="pipx uninstall left .*omm behind",
    ):
        pypi_release.pipx_smoke("https://pypi.example/simple/", pyproject)
