import importlib.metadata
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from omm import package_metadata


class _FakeDistribution:
    def __init__(self, version="0.2.119", entry_points=(), direct_url=None):
        self.version = version
        self.entry_points = entry_points
        self._direct_url = direct_url

    def read_text(self, name):
        assert name == "direct_url.json"
        return self._direct_url

    def locate_file(self, name):
        return Path("/venv/site-packages") / name


def _entry_point(value="omm.cli:main"):
    return SimpleNamespace(group="console_scripts", name="omm", value=value)


def test_current_distribution_name_is_preferred_over_legacy(monkeypatch):
    current = _FakeDistribution(version="0.2.119")
    calls = []

    def fake_distribution(name):
        calls.append(name)
        if name == package_metadata.DISTRIBUTION_NAME:
            return current
        raise AssertionError("legacy lookup must not run when the current name exists")

    monkeypatch.setattr(importlib.metadata, "distribution", fake_distribution)

    assert package_metadata.find_distribution() == ("omm-model", current)
    assert package_metadata.version() == "0.2.119"
    assert calls == ["omm-model", "omm-model"]


def test_validated_legacy_distribution_is_used_as_fallback(monkeypatch):
    legacy = _FakeDistribution(version="0.2.118", entry_points=[_entry_point()])

    def fake_distribution(name):
        if name == "omm-model":
            raise importlib.metadata.PackageNotFoundError(name)
        return legacy

    monkeypatch.setattr(importlib.metadata, "distribution", fake_distribution)

    assert package_metadata.find_distribution() == ("omm", legacy)
    assert package_metadata.version() == "0.2.118"


def test_unrelated_legacy_omm_distribution_is_rejected(monkeypatch):
    unrelated = _FakeDistribution(entry_points=[_entry_point("other_package.cli:main")])

    def fake_distribution(name):
        if name == "omm-model":
            raise importlib.metadata.PackageNotFoundError(name)
        return unrelated

    monkeypatch.setattr(importlib.metadata, "distribution", fake_distribution)

    assert package_metadata.find_distribution() is None
    with pytest.raises(importlib.metadata.PackageNotFoundError):
        package_metadata.version()


def test_direct_url_uses_the_validated_distribution(monkeypatch):
    current = _FakeDistribution(
        direct_url='{"url":"https://github.com/omm-hippo/omm.git",'
        '"vcs_info":{"vcs":"git","commit_id":"deadbeef"}}'
    )
    monkeypatch.setattr(package_metadata, "distribution", lambda: current)

    assert package_metadata.direct_url()["vcs_info"]["commit_id"] == "deadbeef"


def test_git_vcs_metadata_is_positive_source_install_evidence(monkeypatch, tmp_path):
    current = _FakeDistribution()
    monkeypatch.setattr(package_metadata, "_package_checkout", lambda: tmp_path)
    monkeypatch.setattr(
        package_metadata, "find_distribution", lambda: ("omm-model", current)
    )
    monkeypatch.setattr(
        package_metadata,
        "direct_url",
        lambda distribution=None: {
            "url": "https://github.com/omm-hippo/omm.git",
            "vcs_info": {"vcs": "git", "commit_id": "abc"},
        },
    )

    assert package_metadata.install_source() is package_metadata.InstallSource.GIT


def test_checkout_origin_uses_a_bounded_exact_origin_query(monkeypatch, tmp_path):
    calls = []

    class _Process:
        returncode = 0

        def communicate(self, timeout=None):
            calls.append(("communicate", timeout))
            return "https://github.com/omm-hippo/omm.git\n", ""

    def fake_popen(args, **kwargs):
        calls.append((args, kwargs))
        return _Process()

    monkeypatch.setattr(package_metadata, "_ORIGIN_POPEN", fake_popen)

    assert package_metadata._checkout_origin(tmp_path) == (
        "https://github.com/omm-hippo/omm.git"
    )
    assert calls[0][0] == [
        "git",
        "-C",
        str(tmp_path),
        "remote",
        "get-url",
        "origin",
    ]
    assert calls[1] == ("communicate", 5)


@pytest.mark.parametrize(
    "origin",
    [
        "https://github.com/omm-hippo/omm.git",
        "git@github.com:omm-hippo/omm.git",
        "ssh://git@github.com/minigu5/Omm.git",
        "https://github.com/minigu5/Localfit.git",
    ],
)
def test_source_checkout_requires_an_exact_canonical_or_legacy_origin(
    monkeypatch, tmp_path, origin
):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(package_metadata, "_package_checkout", lambda: tmp_path)
    monkeypatch.setattr(package_metadata, "_checkout_origin", lambda checkout: origin)

    assert package_metadata.install_source() is package_metadata.InstallSource.GIT


@pytest.mark.parametrize(
    "origin",
    [
        "https://github.com/example/omm.git",
        "https://github.com/omm-hippo/omm.git?ref=fork",
        "https://github.com.evil.invalid/omm-hippo/omm.git",
    ],
)
def test_source_checkout_with_untrusted_origin_is_unknown(monkeypatch, tmp_path, origin):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(package_metadata, "_package_checkout", lambda: tmp_path)
    monkeypatch.setattr(package_metadata, "_checkout_origin", lambda checkout: origin)
    monkeypatch.setattr(
        package_metadata,
        "find_distribution",
        lambda: (_ for _ in ()).throw(AssertionError("must fail closed before PyPI detection")),
    )

    assert package_metadata.install_source() is package_metadata.InstallSource.UNKNOWN


def test_untrusted_pep610_git_url_is_unknown_even_inside_pipx(monkeypatch, tmp_path):
    current = _FakeDistribution()
    monkeypatch.setattr(package_metadata, "_package_checkout", lambda: tmp_path)
    monkeypatch.setattr(
        package_metadata, "find_distribution", lambda: ("omm-model", current)
    )
    monkeypatch.setattr(
        package_metadata,
        "direct_url",
        lambda distribution=None: {
            "url": "https://github.com/example/omm.git",
            "vcs_info": {"vcs": "git", "commit_id": "abc"},
        },
    )
    monkeypatch.setattr(
        package_metadata,
        "_installation_paths",
        lambda distribution: [tmp_path / "pipx" / "venvs" / "omm-model"],
    )

    assert package_metadata.install_source() is package_metadata.InstallSource.UNKNOWN


@pytest.mark.parametrize(
    ("install_path", "expected"),
    [
        (
            "/Users/alice/.local/share/pipx/venvs/omm-model/lib/python3.14/site-packages",
            package_metadata.InstallSource.PIPX,
        ),
        (
            "/opt/homebrew/Cellar/omm/0.2.119/libexec/lib/python3.14/site-packages",
            package_metadata.InstallSource.HOMEBREW,
        ),
        (
            "C:/Users/Alice/AppData/Local/Microsoft/WinGet/Packages/"
            "OmmHippo.OMM_Microsoft.Winget.Source_x64",
            package_metadata.InstallSource.WINGET,
        ),
        ("/ordinary/venv/lib/python3.14/site-packages", package_metadata.InstallSource.PYPI),
    ],
)
def test_package_managed_install_sources_are_classified(
    monkeypatch, tmp_path, install_path, expected
):
    current = _FakeDistribution()
    monkeypatch.setattr(package_metadata, "_package_checkout", lambda: tmp_path)
    monkeypatch.setattr(
        package_metadata, "find_distribution", lambda: ("omm-model", current)
    )
    monkeypatch.setattr(package_metadata, "direct_url", lambda distribution=None: None)
    monkeypatch.setattr(
        package_metadata, "_installation_paths", lambda distribution: [Path(install_path)]
    )

    assert package_metadata.install_source() is expected


def test_unrecognized_install_without_distribution_stays_unknown(monkeypatch, tmp_path):
    monkeypatch.setattr(package_metadata, "_package_checkout", lambda: tmp_path)
    monkeypatch.setattr(package_metadata, "find_distribution", lambda: None)
    monkeypatch.setattr(
        package_metadata, "_installation_paths", lambda distribution: [Path("/unknown/place")]
    )

    assert package_metadata.install_source() is package_metadata.InstallSource.UNKNOWN


def _write_npm_package(tmp_path, monkeypatch, *, overrides=None):
    current = _FakeDistribution(version="0.2.142")
    package_name = "@omm-hippo/omm-darwin-arm64"
    target = "darwin-arm64"
    binary_name = "bin/omm"
    binary = tmp_path / binary_name
    binary.parent.mkdir()
    binary.write_bytes(b"standalone omm")
    manifest = {
        "name": package_name,
        "version": "0.2.142",
        "os": ["darwin"],
        "cpu": ["arm64"],
        "omm": {
            "distribution": "omm-model",
            "target": target,
            "binary": binary_name,
        },
    }
    for key, value in (overrides or {}).items():
        if key.startswith("omm."):
            manifest["omm"][key.removeprefix("omm.")] = value
        else:
            manifest[key] = value
    (tmp_path / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(package_metadata, "_package_checkout", lambda: tmp_path / "checkout")
    monkeypatch.setattr(
        package_metadata, "find_distribution", lambda: ("omm-model", current)
    )
    monkeypatch.setattr(package_metadata, "direct_url", lambda distribution=None: None)
    monkeypatch.setattr(
        package_metadata,
        "_installation_paths",
        lambda distribution: [Path("/ordinary/site-packages")],
    )
    monkeypatch.setattr(
        package_metadata,
        "_npm_target",
        lambda: (package_name, target, binary_name, "darwin", "arm64"),
    )
    monkeypatch.setenv("OMM_NPM_PACKAGE_ROOT", str(tmp_path))
    monkeypatch.setenv("OMM_NPM_LAUNCHER_PACKAGE", "@omm-hippo/omm")
    monkeypatch.setattr(package_metadata.sys, "executable", str(binary))
    return binary


def test_verified_npm_package_is_classified(monkeypatch, tmp_path):
    _write_npm_package(tmp_path, monkeypatch)

    assert package_metadata.install_source() is package_metadata.InstallSource.NPM


@pytest.mark.parametrize(
    "overrides",
    [
        {"name": "@example/omm-darwin-arm64"},
        {"version": "9.9.9"},
        {"os": ["linux"]},
        {"cpu": ["x64"]},
        {"omm.distribution": "unrelated"},
        {"omm.target": "darwin-x64"},
        {"omm.binary": "bin/other"},
    ],
)
def test_invalid_npm_manifest_fails_closed(monkeypatch, tmp_path, overrides):
    _write_npm_package(tmp_path, monkeypatch, overrides=overrides)

    assert package_metadata.install_source() is package_metadata.InstallSource.UNKNOWN


def test_npm_claim_requires_exact_launcher_and_executable(monkeypatch, tmp_path):
    _write_npm_package(tmp_path, monkeypatch)
    monkeypatch.setenv("OMM_NPM_LAUNCHER_PACKAGE", "@example/omm")
    assert package_metadata.install_source() is package_metadata.InstallSource.UNKNOWN

    monkeypatch.setenv("OMM_NPM_LAUNCHER_PACKAGE", "@omm-hippo/omm")
    other = tmp_path / "other-omm"
    other.write_bytes(b"other")
    monkeypatch.setattr(package_metadata.sys, "executable", str(other))
    assert package_metadata.install_source() is package_metadata.InstallSource.UNKNOWN
