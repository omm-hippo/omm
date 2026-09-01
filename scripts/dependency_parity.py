#!/usr/bin/env python3
"""Verify standalone binary requirements against the project runtime contract."""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

from packaging.markers import default_environment
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import (
    InvalidSdistFilename,
    canonicalize_name,
    parse_sdist_filename,
)


ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
NPM_REQUIREMENTS = ROOT / "requirements-npm-binary.txt"
WINDOWS_REQUIREMENTS = ROOT / "requirements-windows-portable.txt"
BUILD_TOOL_NAMES = frozenset(
    {
        "build",
        "hatchling",
        "pyinstaller",
        "pyinstaller-hooks-contrib",
    }
)
HOMEBREW_RESOURCE_PATTERN = re.compile(
    r'^  resource "(?P<name>[^"\n]+)" do\n'
    r'    url "(?P<url>[^"\n]+)"$',
    re.MULTILINE,
)


class DependencyParityError(RuntimeError):
    """Raised when a standalone artifact would embed the wrong runtime graph."""


@dataclass(frozen=True)
class BinaryTarget:
    name: str
    requirements: Path
    python_version: str
    sys_platform: str
    platform_machine: str
    platform_system: str

    def marker_environment(self) -> dict[str, str]:
        environment = default_environment()
        environment.update(
            {
                "python_version": self.python_version,
                "python_full_version": f"{self.python_version}.0",
                "sys_platform": self.sys_platform,
                "platform_machine": self.platform_machine,
                "platform_system": self.platform_system,
            }
        )
        return environment


@dataclass(frozen=True)
class VersionException:
    version: str
    reason: str


@dataclass(frozen=True)
class ParityResult:
    target: str
    runtime_versions: dict[str, str]
    exceptions: dict[str, VersionException]


TARGETS = (
    BinaryTarget(
        "linux-x64-gnu",
        NPM_REQUIREMENTS,
        "3.11",
        "linux",
        "x86_64",
        "Linux",
    ),
    BinaryTarget(
        "linux-arm64-gnu",
        NPM_REQUIREMENTS,
        "3.11",
        "linux",
        "aarch64",
        "Linux",
    ),
    BinaryTarget(
        "darwin-arm64",
        NPM_REQUIREMENTS,
        "3.11",
        "darwin",
        "arm64",
        "Darwin",
    ),
    BinaryTarget(
        "darwin-x64",
        NPM_REQUIREMENTS,
        "3.11",
        "darwin",
        "x86_64",
        "Darwin",
    ),
    BinaryTarget(
        "win32-x64",
        WINDOWS_REQUIREMENTS,
        "3.14",
        "win32",
        "AMD64",
        "Windows",
    ),
)

# cryptography 49+ has no Intel macOS wheel. Building it from source against the
# runner's Homebrew OpenSSL produced a frozen executable with an incompatible
# libssl, so only the darwin-x64 artifact remains on the last usable wheel.
VERSION_EXCEPTIONS = {
    ("darwin-x64", "cryptography"): VersionException(
        "48.0.0",
        "cryptography 49+ publishes no Intel macOS wheel; the source build "
        "linked an incompatible Homebrew libssl",
    )
}


def _read_project_requirements(pyproject: Path) -> list[Requirement]:
    try:
        payload = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        dependencies = payload["project"]["dependencies"]
    except (KeyError, OSError, tomllib.TOMLDecodeError) as error:
        raise DependencyParityError(
            f"cannot read project dependencies from {pyproject}"
        ) from error
    if not isinstance(dependencies, list) or not all(
        isinstance(item, str) for item in dependencies
    ):
        raise DependencyParityError("project.dependencies must be a list of strings")
    try:
        return [Requirement(item) for item in dependencies]
    except InvalidRequirement as error:
        raise DependencyParityError(f"invalid project dependency: {error}") from error


def _read_binary_requirements(requirements: Path) -> list[Requirement]:
    try:
        lines = requirements.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise DependencyParityError(f"cannot read {requirements}") from error
    entries = [
        line.strip()
        for line in lines
        if line.strip() and not line.lstrip().startswith("#")
    ]
    try:
        parsed = [Requirement(entry) for entry in entries]
    except InvalidRequirement as error:
        raise DependencyParityError(
            f"invalid requirement in {requirements}: {error}"
        ) from error
    for requirement in parsed:
        specifiers = list(requirement.specifier)
        if (
            len(specifiers) != 1
            or specifiers[0].operator != "=="
            or "*" in specifiers[0].version
        ):
            raise DependencyParityError(
                f"{requirements.name} must exactly pin {requirement.name}"
            )
    return parsed


def _active_requirements(
    requirements: list[Requirement], environment: dict[str, str], source: str
) -> dict[str, Requirement]:
    active: dict[str, Requirement] = {}
    for requirement in requirements:
        if requirement.marker is not None and not requirement.marker.evaluate(
            environment
        ):
            continue
        name = canonicalize_name(requirement.name)
        if name in active:
            raise DependencyParityError(
                f"{source} selects {name} more than once for this target"
            )
        active[name] = requirement
    return active


def _exact_version(requirement: Requirement) -> str:
    specifier = next(iter(requirement.specifier))
    return specifier.version


def _is_exact_pin(requirement: Requirement) -> bool:
    specifiers = list(requirement.specifier)
    return (
        len(specifiers) == 1
        and specifiers[0].operator == "=="
        and "*" not in specifiers[0].version
    )


def _check_runtime_versions(
    target_name: str,
    project: dict[str, Requirement],
    runtime_versions: dict[str, str],
    version_exceptions: dict[tuple[str, str], VersionException],
) -> ParityResult:
    all_project_pins_are_exact = all(
        _is_exact_pin(item) for item in project.values()
    )
    missing = project.keys() - runtime_versions.keys()
    unexpected = (
        runtime_versions.keys() - project.keys() if all_project_pins_are_exact else set()
    )
    if missing or unexpected:
        details = []
        if missing:
            details.append(f"missing {sorted(missing)}")
        if unexpected:
            details.append(f"unexpected {sorted(unexpected)}")
        raise DependencyParityError(
            f"{target_name} runtime package set differs from pyproject.toml: "
            + "; ".join(details)
        )

    applied_exceptions: dict[str, VersionException] = {}
    for name, project_requirement in project.items():
        actual_version = runtime_versions[name]
        if project_requirement.specifier.contains(actual_version, prereleases=True):
            continue
        exception = version_exceptions.get((target_name, name))
        if exception is None or exception.version != actual_version:
            raise DependencyParityError(
                f"{target_name} pins {name}=={actual_version}, which does not satisfy "
                f"pyproject.toml {project_requirement.specifier}"
            )
        applied_exceptions[name] = exception

    return ParityResult(target_name, runtime_versions, applied_exceptions)


def check_target(
    target: BinaryTarget,
    *,
    pyproject: Path = PYPROJECT,
    build_tool_names: frozenset[str] = BUILD_TOOL_NAMES,
    version_exceptions: dict[tuple[str, str], VersionException] = VERSION_EXCEPTIONS,
) -> ParityResult:
    environment = target.marker_environment()
    project = _active_requirements(
        _read_project_requirements(pyproject), environment, pyproject.name
    )
    binary = _active_requirements(
        _read_binary_requirements(target.requirements),
        environment,
        target.requirements.name,
    )

    missing_build_tools = build_tool_names - binary.keys()
    if missing_build_tools:
        raise DependencyParityError(
            f"{target.name} is missing build tools: {sorted(missing_build_tools)}"
        )

    runtime = {
        name: requirement
        for name, requirement in binary.items()
        if name not in build_tool_names
    }
    versions = {
        name: _exact_version(requirement) for name, requirement in runtime.items()
    }
    return _check_runtime_versions(target.name, project, versions, version_exceptions)


def check_all(
    *, pyproject: Path = PYPROJECT, targets: tuple[BinaryTarget, ...] = TARGETS
) -> dict[str, ParityResult]:
    return {target.name: check_target(target, pyproject=pyproject) for target in targets}


def _homebrew_resource_versions(formula: str) -> dict[str, str]:
    resources: dict[str, str] = {}
    for match in HOMEBREW_RESOURCE_PATTERN.finditer(formula):
        declared_name = canonicalize_name(match.group("name"))
        url = urlsplit(match.group("url"))
        if (
            url.scheme != "https"
            or url.hostname != "files.pythonhosted.org"
            or url.username is not None
            or url.password is not None
            or url.port not in (None, 443)
            or url.query
            or url.fragment
        ):
            raise DependencyParityError(
                f"Homebrew resource {declared_name} does not use an immutable PyPI URL"
            )
        filename = Path(unquote(url.path)).name
        try:
            archive_name, version = parse_sdist_filename(filename)
        except InvalidSdistFilename as error:
            raise DependencyParityError(
                f"Homebrew resource {declared_name} is not a source archive: {filename}"
            ) from error
        archive_name = canonicalize_name(archive_name)
        if archive_name != declared_name:
            raise DependencyParityError(
                f"Homebrew resource {declared_name} URL contains {archive_name}"
            )
        if declared_name in resources:
            raise DependencyParityError(
                f"Homebrew Formula selects {declared_name} more than once"
            )
        resources[declared_name] = str(version)
    if not resources:
        raise DependencyParityError("Homebrew Formula contains no Python resources")
    return resources


def check_homebrew_formula(
    formula: Path, *, pyproject: Path = PYPROJECT
) -> ParityResult:
    environment = BinaryTarget(
        "homebrew-python3.14",
        formula,
        "3.14",
        "darwin",
        "arm64",
        "Darwin",
    ).marker_environment()
    project = _active_requirements(
        _read_project_requirements(pyproject), environment, pyproject.name
    )
    try:
        formula_contents = formula.read_text(encoding="utf-8")
    except OSError as error:
        raise DependencyParityError(f"cannot read {formula}") from error
    return _check_runtime_versions(
        "homebrew-python3.14",
        project,
        _homebrew_resource_versions(formula_contents),
        {},
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pyproject", type=Path, default=PYPROJECT)
    parser.add_argument(
        "--homebrew-formula",
        type=Path,
        help="also compare a checked-out Homebrew Formula with the project runtime",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        results = check_all(pyproject=args.pyproject)
        if args.homebrew_formula is not None:
            homebrew = check_homebrew_formula(
                args.homebrew_formula, pyproject=args.pyproject
            )
            results[homebrew.target] = homebrew
    except DependencyParityError as error:
        print(f"dependency parity check failed: {error}", file=sys.stderr)
        return 1
    for result in results.values():
        detail = f"{len(result.runtime_versions)} runtime pins match"
        if result.exceptions:
            exceptions = ", ".join(
                f"{name}=={exception.version} ({exception.reason})"
                for name, exception in sorted(result.exceptions.items())
            )
            detail += f"; explicit exception: {exceptions}"
        print(f"{result.target}: {detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
