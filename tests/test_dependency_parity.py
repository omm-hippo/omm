from __future__ import annotations

import pytest

from scripts import dependency_parity


def test_checked_in_binary_requirements_match_project_runtime_contract():
    results = dependency_parity.check_all()

    assert set(results) == {
        "linux-x64-gnu",
        "linux-arm64-gnu",
        "darwin-arm64",
        "darwin-x64",
        "win32-x64",
    }
    assert results["darwin-x64"].runtime_versions["cryptography"] == "48.0.0"
    assert set(results["darwin-x64"].exceptions) == {"cryptography"}
    for target in (
        "linux-x64-gnu",
        "linux-arm64-gnu",
        "darwin-arm64",
        "win32-x64",
    ):
        assert results[target].runtime_versions["cryptography"] == "50.0.1"
        assert results[target].exceptions == {}


def test_checker_rejects_a_drifted_runtime_pin(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    requirements = tmp_path / "requirements.txt"
    pyproject.write_text(
        '[project]\nname = "example"\nversion = "1.0.0"\n'
        'dependencies = ["click==8.5.0"]\n',
        encoding="utf-8",
    )
    requirements.write_text("click==8.4.2\n", encoding="utf-8")
    target = dependency_parity.BinaryTarget(
        "test-target", requirements, "3.12", "linux", "x86_64", "Linux"
    )

    with pytest.raises(
        dependency_parity.DependencyParityError, match="does not satisfy"
    ):
        dependency_parity.check_target(
            target,
            pyproject=pyproject,
            build_tool_names=frozenset(),
            version_exceptions={},
        )


def test_checker_rejects_a_missing_frozen_dependency(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    requirements = tmp_path / "requirements.txt"
    pyproject.write_text(
        '[project]\nname = "example"\nversion = "1.0.0"\n'
        'dependencies = ["click==8.5.0", "colorama==0.4.6"]\n',
        encoding="utf-8",
    )
    requirements.write_text("click==8.5.0\n", encoding="utf-8")
    target = dependency_parity.BinaryTarget(
        "test-target", requirements, "3.12", "linux", "x86_64", "Linux"
    )

    with pytest.raises(dependency_parity.DependencyParityError, match="missing"):
        dependency_parity.check_target(
            target,
            pyproject=pyproject,
            build_tool_names=frozenset(),
            version_exceptions={},
        )


def test_checker_rejects_duplicate_active_marker_entries(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    requirements = tmp_path / "requirements.txt"
    pyproject.write_text(
        '[project]\nname = "example"\nversion = "1.0.0"\n'
        'dependencies = ["click==8.5.0"]\n',
        encoding="utf-8",
    )
    requirements.write_text(
        'click==8.5.0; sys_platform == "linux"\n'
        'click==8.4.2; platform_machine == "x86_64"\n',
        encoding="utf-8",
    )
    target = dependency_parity.BinaryTarget(
        "test-target", requirements, "3.12", "linux", "x86_64", "Linux"
    )

    with pytest.raises(dependency_parity.DependencyParityError, match="more than once"):
        dependency_parity.check_target(
            target,
            pyproject=pyproject,
            build_tool_names=frozenset(),
            version_exceptions={},
        )


def test_homebrew_formula_resources_match_the_project_contract(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    formula = tmp_path / "omm.rb"
    pyproject.write_text(
        '[project]\nname = "example"\nversion = "1.0.0"\n'
        'dependencies = ["click==8.5.0", "colorama==0.4.6"]\n',
        encoding="utf-8",
    )
    formula.write_text(
        '  resource "click" do\n'
        '    url "https://files.pythonhosted.org/packages/click-8.5.0.tar.gz"\n'
        '  end\n\n'
        '  resource "colorama" do\n'
        '    url "https://files.pythonhosted.org/packages/colorama-0.4.6.tar.gz"\n'
        '  end\n',
        encoding="utf-8",
    )

    result = dependency_parity.check_homebrew_formula(formula, pyproject=pyproject)

    assert result.runtime_versions == {"click": "8.5.0", "colorama": "0.4.6"}


def test_homebrew_formula_checker_rejects_version_drift(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    formula = tmp_path / "omm.rb"
    pyproject.write_text(
        '[project]\nname = "example"\nversion = "1.0.0"\n'
        'dependencies = ["click==8.5.0"]\n',
        encoding="utf-8",
    )
    formula.write_text(
        '  resource "click" do\n'
        '    url "https://files.pythonhosted.org/packages/click-8.4.2.tar.gz"\n'
        '  end\n',
        encoding="utf-8",
    )

    with pytest.raises(
        dependency_parity.DependencyParityError, match="does not satisfy"
    ):
        dependency_parity.check_homebrew_formula(formula, pyproject=pyproject)
