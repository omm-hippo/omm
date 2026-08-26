from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "distribution_versions", ROOT / "scripts" / "distribution_versions.py"
)
assert SPEC is not None and SPEC.loader is not None
distribution_versions = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(distribution_versions)


def test_pypi_version_reads_project_metadata(monkeypatch):
    monkeypatch.setattr(
        distribution_versions,
        "_fetch",
        lambda _url: json.dumps({"info": {"version": "1.2.3"}}).encode(),
    )

    assert distribution_versions.pypi_version("https://example.test/pypi") == "1.2.3"


def test_homebrew_version_reads_omm_source_archive(monkeypatch):
    formula = b'class Omm < Formula\n  url "https://example/omm_model-1.2.3.tar.gz"\n'
    monkeypatch.setattr(distribution_versions, "_fetch", lambda _url: formula)

    assert distribution_versions.homebrew_version("https://example.test/omm.rb") == "1.2.3"


def test_homebrew_version_rejects_missing_or_ambiguous_omm_archive(monkeypatch):
    monkeypatch.setattr(
        distribution_versions,
        "_fetch",
        lambda _url: b'url "https://example/other-1.2.3.tar.gz"',
    )

    with pytest.raises(distribution_versions.DistributionVersionError, match="exactly one"):
        distribution_versions.homebrew_version("https://example.test/omm.rb")


def test_check_versions_rejects_channel_drift(monkeypatch):
    monkeypatch.setattr(distribution_versions, "local_version", lambda _path: "1.2.3")
    monkeypatch.setattr(distribution_versions, "pypi_version", lambda _url: "1.2.3")
    monkeypatch.setattr(distribution_versions, "homebrew_version", lambda _url: "1.2.2")

    with pytest.raises(distribution_versions.DistributionVersionError, match="not synchronized"):
        distribution_versions.check_versions(
            pyproject=Path("pyproject.toml"),
            pypi_url="https://pypi.test/json",
            homebrew_url="https://brew.test/omm.rb",
        )


def test_check_versions_accepts_exact_match(monkeypatch):
    monkeypatch.setattr(distribution_versions, "local_version", lambda _path: "1.2.3")
    monkeypatch.setattr(distribution_versions, "pypi_version", lambda _url: "1.2.3")
    monkeypatch.setattr(distribution_versions, "homebrew_version", lambda _url: "1.2.3")

    assert distribution_versions.check_versions(
        pyproject=Path("pyproject.toml"),
        pypi_url="https://pypi.test/json",
        homebrew_url="https://brew.test/omm.rb",
    ) == {"local": "1.2.3", "PyPI": "1.2.3", "Homebrew": "1.2.3"}


def test_fetch_rejects_insecure_or_credentialed_release_url():
    for url in ("http://example.test/release", "https://user:secret@example.test/release"):
        with pytest.raises(
            distribution_versions.DistributionVersionError,
            match="credential-free HTTPS",
        ):
            distribution_versions._fetch(url)
