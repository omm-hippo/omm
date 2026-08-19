import importlib.metadata
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
