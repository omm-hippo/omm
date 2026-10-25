from __future__ import annotations

import importlib.metadata as metadata
from pathlib import Path

import pytest

from scripts import bundled_licenses as bl


ROOT = Path(__file__).resolve().parents[1]


def _dist(tmp_path: Path, name: str, files: dict[str, str], *, meta: str = "") -> metadata.Distribution:
    """Write an installed-looking distribution with a RECORD and return it."""
    info = tmp_path / f"{name}-1.0.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.4\nName: {name}\nVersion: 1.0\n{meta}", encoding="utf-8"
    )
    records = [f"{info.name}/METADATA,,"]
    for relative, text in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
        records.append(f"{relative},,")
    (info / "RECORD").write_text("\n".join(records) + "\n", encoding="utf-8")
    return metadata.PathDistribution(info)


@pytest.fixture
def policy():
    return bl._scripts_import("check_licenses").load_policy()


def test_license_and_notice_files_come_from_the_own_dist_info(tmp_path, policy):
    dist = _dist(
        tmp_path,
        "example",
        {
            "example-1.0.dist-info/licenses/LICENSE": "Apache License\r\nVersion 2.0  \r\n",
            "example-1.0.dist-info/licenses/NOTICE": "Example\nCopyright 2026 Example Authors\n",
            "example-1.0.dist-info/AUTHORS.rst": "A. Author\n",
            "example-1.0.dist-info/entry_points.txt": "[console_scripts]\n",
            "example/_vendor/other-2.0.dist-info/LICENSE": "vendored, not ours\n",
        },
        meta="License-File: LICENSE\nLicense-File: NOTICE\n",
    )

    files = bl.package_license_files("example", dist, policy)

    assert files == [
        ("AUTHORS.rst", "A. Author"),
        ("licenses/LICENSE", "Apache License\nVersion 2.0"),
        ("licenses/NOTICE", "Example\nCopyright 2026 Example Authors"),
    ]


def test_legacy_root_license_files_are_found(tmp_path, policy):
    dist = _dist(
        tmp_path,
        "legacy",
        {"legacy-1.0.dist-info/LICENSE": "MIT\n", "legacy-1.0.dist-info/NOTICE": "notice\n"},
    )
    assert [label for label, _ in bl.package_license_files("legacy", dist, policy)] == ["LICENSE", "NOTICE"]


def test_missing_license_text_fails_unless_the_policy_vendors_one(tmp_path, policy, monkeypatch):
    dist = _dist(tmp_path, "bare", {"bare-1.0.dist-info/NOTICE": "only a notice\n"})
    with pytest.raises(bl.BundledLicensesError, match="ships no license file"):
        bl.package_license_files("bare", dist, policy)

    vendored = tmp_path / "packaging" / "licenses" / "bare.txt"
    vendored.parent.mkdir(parents=True)
    vendored.write_text("BSD-3-Clause text\n", encoding="utf-8")
    monkeypatch.setattr(bl, "ROOT", tmp_path)
    policy["python"]["license_texts"]["bare"] = {"path": "packaging/licenses/bare.txt", "reason": ""}
    with pytest.raises(bl.BundledLicensesError, match="needs a reason"):
        bl.package_license_files("bare", dist, policy)

    policy["python"]["license_texts"]["bare"]["reason"] = "The wheel ships no license file."
    assert bl.package_license_files("bare", dist, policy) == [
        ("NOTICE", "only a notice"),
        ("packaging/licenses/bare.txt", "BSD-3-Clause text"),
    ]


def test_shipped_closure_skips_build_tools_and_needs_every_package_installed(tmp_path, policy, monkeypatch):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(
        "pyinstaller==1.0\nbuild==1.0\nalpha==1.0\nnever==1.0; sys_platform == 'nonexistent'\n",
        encoding="utf-8",
    )
    installed = {
        "alpha": _dist(tmp_path / "a", "alpha", {}, meta="Requires-Dist: beta\nRequires-Dist: gamma; extra == 'x'\n"),
        "beta": _dist(tmp_path / "b", "beta", {}),
        "pyinstaller": _dist(tmp_path / "p", "pyinstaller", {}),
        "pyinstaller-hooks-contrib": _dist(tmp_path / "h", "pyinstaller-hooks-contrib", {}),
    }
    check_licenses = bl._scripts_import("check_licenses")
    monkeypatch.setattr(check_licenses, "_distribution", installed.get)

    found = bl.shipped_distributions(requirements, policy)
    # Build tools are only present as embedded leaves; gamma needs an extra.
    assert list(found) == ["alpha", "beta", "pyinstaller", "pyinstaller-hooks-contrib"]

    del installed["beta"]
    with pytest.raises(bl.BundledLicensesError, match="beta is part of the standalone runtime graph"):
        bl.shipped_distributions(requirements, policy)


def test_generate_is_deterministic_and_validates(tmp_path, policy, monkeypatch):
    dist = _dist(
        tmp_path,
        "requests",
        {
            "requests-1.0.dist-info/licenses/LICENSE": "Apache License 2.0\n",
            "requests-1.0.dist-info/licenses/NOTICE": "Requests\nCopyright 2019 Kenneth Reitz\n",
        },
        meta="License-Expression: Apache-2.0\n",
    )
    check_licenses = bl._scripts_import("check_licenses")
    monkeypatch.setattr(check_licenses, "_distribution", {"requests": dist}.get)
    monkeypatch.setattr(bl, "shipped_distributions", lambda _path, _policy: {"requests": dist})
    cpython = tmp_path / "LICENSE.txt"
    cpython.write_text("PYTHON SOFTWARE FOUNDATION LICENSE VERSION 2\n", encoding="utf-8")
    monkeypatch.setattr(bl, "cpython_license_path", lambda: cpython)
    web = tmp_path / "third-party-licenses.txt"
    web.write_text("Third-party licenses for OMM's local web interface\n", encoding="utf-8")
    monkeypatch.setattr(bl, "web_licenses_path", lambda: web)

    first = bl.generate("linux-x64-gnu", policy)
    assert first == bl.generate("linux-x64-gnu", policy)
    bl.validate_text(first.encode("utf-8"))
    assert "requests 1.0: Apache-2.0" in first
    assert "Copyright 2019 Kenneth Reitz" in first
    assert "PYTHON SOFTWARE FOUNDATION LICENSE VERSION 2" in first
    assert "Apache License" in first.split(bl.OPENSSL_MARKER, 1)[1]
    assert "local web interface" in first


def test_generate_refuses_a_shipped_copyleft_package_without_exception(tmp_path, policy, monkeypatch):
    dist = _dist(tmp_path, "gpl-thing", {"gpl-thing-1.0.dist-info/COPYING": "GPL\n"}, meta="License-Expression: GPL-3.0-only\n")
    check_licenses = bl._scripts_import("check_licenses")
    monkeypatch.setattr(check_licenses, "_distribution", {"gpl-thing": dist}.get)
    monkeypatch.setattr(bl, "shipped_distributions", lambda _path, _policy: {"gpl-thing": dist})
    with pytest.raises(bl.BundledLicensesError, match="GPL-3.0-only"):
        bl.generate("linux-x64-gnu", policy)


@pytest.mark.parametrize(
    ("data", "match"),
    [
        (b"licenses: see the website\n", "generated header"),
        (bl.TITLE.encode() + b"\n\n== OpenSSL ==\n", "CPython"),
        (bl.TITLE.encode() + b"\n\n== CPython 3 ==\n", "OpenSSL"),
        (bl.TITLE.encode() + b"\r\n\n== CPython 3 ==\n== OpenSSL ==\n", "generated header"),
        (bl.TITLE.encode() + b"\n\r\n== CPython 3 ==\n== OpenSSL ==\n", "LF line endings"),
        (b"\xff\xfe", "UTF-8"),
    ],
)
def test_validate_text_rejects_hand_written_files(data, match):
    with pytest.raises(bl.BundledLicensesError, match=match):
        bl.validate_text(data)


def test_requirement_files_and_policy_components_exist(policy):
    assert bl.requirements_file("win32-x64").name == "requirements-windows-portable.txt"
    assert bl.requirements_file("darwin-arm64").name == "requirements-npm-binary.txt"
    openssl = bl._component(policy, "openssl")
    text = (ROOT / openssl["text"]).read_text(encoding="utf-8")
    assert "Apache License" in text and "END OF TERMS AND CONDITIONS" in text
    assert openssl["source"].startswith("https://")
    assert bl._component(policy, "cpython")["license"] == "PSF-2.0"


def test_build_interpreter_ships_a_cpython_license():
    # The same lookup runs on every CI build runner.
    assert "PYTHON SOFTWARE FOUNDATION" in bl.cpython_license_path().read_text(encoding="utf-8", errors="replace")
