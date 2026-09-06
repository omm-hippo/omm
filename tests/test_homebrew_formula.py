from __future__ import annotations

import hashlib
import importlib.util
import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "homebrew_formula", ROOT / "scripts" / "homebrew_formula.py"
)
assert SPEC is not None and SPEC.loader is not None
homebrew_formula = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(homebrew_formula)


FETCH_URL_PATTERN = re.compile(r"^https://pypi\.org/pypi/([^/]+)/([^/]+)/json$")


def fake_sha256(name: str, version: str) -> str:
    return hashlib.sha256(f"{name}-{version}".encode()).hexdigest()


def fake_release(name: str, version: str) -> dict:
    filename = f"{name.replace('-', '_')}-{version}.tar.gz"
    return {
        "info": {"version": version},
        "urls": [
            {
                "packagetype": "sdist",
                "filename": filename,
                "yanked": False,
                "url": f"https://files.pythonhosted.org/packages/aa/bb/{filename}",
                "digests": {"sha256": fake_sha256(name, version)},
            }
        ],
    }


def fake_fetch(url: str) -> dict:
    match = FETCH_URL_PATTERN.fullmatch(url)
    assert match is not None, f"unexpected fetch URL: {url}"
    from urllib.parse import unquote

    name, version = unquote(match.group(1)), unquote(match.group(2))
    return fake_release(name, version)


FIXTURE_PYPROJECT = """\
[project]
name = "example-cli"
version = "1.2.3"
dependencies = [
    "click==8.5.0",
    "idna==3.19",
    "tomli==2.4.1; python_version < '3.11'",
]
"""


def write_fixture(tmp_path: Path) -> Path:
    path = tmp_path / "pyproject.toml"
    path.write_text(FIXTURE_PYPROJECT, encoding="utf-8")
    return path


def test_render_includes_pinned_dependencies_sorted(tmp_path):
    pyproject = write_fixture(tmp_path)

    text = homebrew_formula.render_formula(
        "9.9.9", pyproject=pyproject, fetch=fake_fetch
    )

    click_index = text.index('resource "click" do')
    idna_index = text.index('resource "idna" do')
    assert click_index < idna_index  # sorted alphabetically
    assert f'sha256 "{fake_sha256("click", "8.5.0")}"' in text
    assert 'url "https://files.pythonhosted.org/packages/aa/bb/omm_model-9.9.9.tar.gz"' in text
    assert f'sha256 "{fake_sha256("omm-model", "9.9.9")}"' in text


def test_render_excludes_marker_gated_dependency_with_a_comment(tmp_path):
    pyproject = write_fixture(tmp_path)

    text = homebrew_formula.render_formula(
        "9.9.9", pyproject=pyproject, fetch=fake_fetch
    )

    assert 'resource "tomli" do' not in text
    assert "tomli==2.4.1; python_version < '3.11'" in text  # noted, not silently dropped


def test_render_is_deterministic(tmp_path):
    pyproject = write_fixture(tmp_path)

    first = homebrew_formula.render_formula("9.9.9", pyproject=pyproject, fetch=fake_fetch)
    second = homebrew_formula.render_formula("9.9.9", pyproject=pyproject, fetch=fake_fetch)

    assert first == second


def test_unsupported_marker_fails_loudly(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "example-cli"\nversion = "1.0.0"\n'
        'dependencies = ["click==8.5.0; sys_platform == \'win32\'"]\n',
        encoding="utf-8",
    )

    with pytest.raises(homebrew_formula.HomebrewFormulaError, match="unsupported environment marker"):
        homebrew_formula.render_formula("1.0.0", pyproject=pyproject, fetch=fake_fetch)


def test_check_passes_for_a_matching_formula(tmp_path):
    pyproject = write_fixture(tmp_path)
    formula = tmp_path / "omm.rb"
    formula.write_text(
        homebrew_formula.render_formula("9.9.9", pyproject=pyproject, fetch=fake_fetch),
        encoding="utf-8",
    )

    homebrew_formula.check_formula(
        formula, "9.9.9", pyproject=pyproject, fetch=fake_fetch
    )  # must not raise


def test_check_detects_a_drifted_omm_version(tmp_path):
    pyproject = write_fixture(tmp_path)
    formula = tmp_path / "omm.rb"
    # Formula still pins the previous OMM release.
    formula.write_text(
        homebrew_formula.render_formula("9.9.8", pyproject=pyproject, fetch=fake_fetch),
        encoding="utf-8",
    )

    with pytest.raises(homebrew_formula.HomebrewFormulaError, match="OMM version"):
        homebrew_formula.check_formula(
            formula, "9.9.9", pyproject=pyproject, fetch=fake_fetch
        )


def test_check_allow_version_lag_ignores_omm_version_drift(tmp_path):
    pyproject = write_fixture(tmp_path)
    formula = tmp_path / "omm.rb"
    formula.write_text(
        homebrew_formula.render_formula("9.9.8", pyproject=pyproject, fetch=fake_fetch),
        encoding="utf-8",
    )

    # Same dependency pins, only the OMM version itself lags - allowed.
    homebrew_formula.check_formula(
        formula,
        "9.9.9",
        pyproject=pyproject,
        fetch=fake_fetch,
        allow_version_lag=True,
    )


def test_check_detects_a_drifted_sha256(tmp_path):
    pyproject = write_fixture(tmp_path)
    formula = tmp_path / "omm.rb"
    text = homebrew_formula.render_formula("9.9.9", pyproject=pyproject, fetch=fake_fetch)
    corrupted_sha256 = "0" * 64
    text = text.replace(fake_sha256("click", "8.5.0"), corrupted_sha256)
    formula.write_text(text, encoding="utf-8")

    with pytest.raises(homebrew_formula.HomebrewFormulaError, match="click"):
        homebrew_formula.check_formula(
            formula,
            "9.9.9",
            pyproject=pyproject,
            fetch=fake_fetch,
            allow_version_lag=True,
        )


def test_check_detects_a_missing_dependency(tmp_path):
    pyproject = write_fixture(tmp_path)
    formula = tmp_path / "omm.rb"
    text = homebrew_formula.render_formula("9.9.9", pyproject=pyproject, fetch=fake_fetch)
    block_start = text.index('  resource "idna" do')
    block_end = text.index("  end\n", block_start) + len("  end\n")
    text = text[:block_start] + text[block_end:]
    formula.write_text(text, encoding="utf-8")

    with pytest.raises(homebrew_formula.HomebrewFormulaError, match="missing from formula"):
        homebrew_formula.check_formula(
            formula,
            "9.9.9",
            pyproject=pyproject,
            fetch=fake_fetch,
            allow_version_lag=True,
        )


def test_pypi_latest_picks_the_highest_non_yanked_version():
    def fetch(url: str) -> dict:
        assert url == "https://pypi.org/pypi/omm-model/json"
        return {
            "releases": {
                "0.2.149": [{"yanked": False}],
                "0.3.33": [{"yanked": False}],
                "0.3.41": [{"yanked": False}],
                "0.3.50": [{"yanked": True}],  # excluded: fully yanked
            }
        }

    assert homebrew_formula.latest_pypi_version(fetch=fetch) == "0.3.41"


def test_normalize_resource_name_matches_pep503():
    assert homebrew_formula.normalize_resource_name("markdown_it_py") == "markdown-it-py"
    assert homebrew_formula.normalize_resource_name("prompt_toolkit") == "prompt-toolkit"
    assert homebrew_formula.normalize_resource_name("Pygments") == "pygments"
    assert homebrew_formula.normalize_resource_name("typing_extensions") == "typing-extensions"
