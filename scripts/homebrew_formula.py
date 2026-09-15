#!/usr/bin/env python3
"""Generate and verify the Homebrew Formula from the pinned runtime graph.

``brew install omm-hippo/omm/omm`` has repeatedly drifted from the dependency
set OMM actually ships (issue #238): a plain ``pip install`` / ``npm install``
reproduces a known-good closure, but the Homebrew Formula's ``resource`` stanzas
were hand-bumped and fell behind.

This module makes the Formula a generated artifact of that closure instead of a
second, independently maintained copy of the dependency table:

- ``render``       build ``omm.rb`` text for a given OMM version from
                    ``requirements-npm-binary.txt``, resolving each pin's sdist
                    URL/sha256 from PyPI.
- ``check``        compare an existing Formula file against what ``render``
                    would produce and fail loudly on any drift.
- ``pypi-latest``  print the latest published, non-yanked ``omm-model``
                    version on PyPI.

``requirements-npm-binary.txt`` is the curated, exactly-``==``-pinned runtime
graph the standalone npm binary embeds. ``scripts/dependency_parity.py`` keeps
it in lockstep with ``pyproject.toml``'s (now floating) dependency floors, so
generating the Formula from the same file means ``brew install`` ships the
identical dependency set as every other install path.

Homebrew compiles every ``resource`` from its sdist (the Formula
``depends_on "rust"`` / ``"openssl@3"`` to build ``cryptography`` / ``cffi``),
so the wheel-availability caveats that split a few ``requirements-npm-binary.txt``
pins by platform do not apply here - the generator always selects the mainline
pin by evaluating markers against a concrete non-Intel-macOS environment. A pin
excluded that way is listed in a Formula comment, never silently dropped.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Callable, NamedTuple
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from packaging.markers import default_environment
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name


ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
NPM_BINARY_REQUIREMENTS = ROOT / "requirements-npm-binary.txt"

PYPI_PACKAGE_NAME = "omm-model"
CLASS_NAME = "Omm"
DESC = "Package manager for local large language models"
HOMEPAGE = "https://github.com/omm-hippo/omm"
LICENSE = "MIT"

# Homebrew's declared interpreter for this Formula. Not derivable from
# pyproject.toml's `requires-python` floor (>=3.10) - it is Homebrew
# packaging policy, tracked here so environment markers can be evaluated
# against the interpreter Homebrew will actually use.
HOMEBREW_PYTHON_VERSION = (3, 14)

# Build-time-only tools listed in requirements-npm-binary.txt that are not part
# of the runtime graph a Homebrew Formula ships as `resource` stanzas. Mirrors
# scripts/dependency_parity.py BUILD_TOOL_NAMES.
BUILD_TOOL_NAMES = frozenset(
    {"build", "hatchling", "pyinstaller", "pyinstaller-hooks-contrib"}
)

# Homebrew-specific build/runtime deps needed to compile `cryptography`
# (rust) and `cffi` (libffi) from source. Not present in the requirements
# file - this is Homebrew packaging knowledge, mirrored from the tap's current
# Formula/omm.rb.
BUILD_DEPENDS_ON = ["pkgconf", "rust"]
RUNTIME_DEPENDS_ON = ["libffi", "openssl@3"]

REQUEST_TIMEOUT_SECONDS = 20
USER_AGENT = "omm-homebrew-formula-generator"

VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SOURCE_PATTERN = re.compile(
    r'^(?P<indent>  )url "(?P<url>https://files\.pythonhosted\.org/[^"\n]+/'
    r"omm_model-(?P<version>[0-9]+\.[0-9]+\.[0-9]+)\.tar\.gz)\"\n"
    r'(?P=indent)sha256 "(?P<sha256>[0-9a-f]{64})"$',
    re.MULTILINE,
)
RESOURCE_PATTERN = re.compile(
    r'^  resource "(?P<name>[a-z0-9][a-z0-9.-]*)" do\n'
    r'    url "(?P<url>https://[^\s"\\#]+)"\n'
    r'    sha256 "(?P<sha256>[0-9a-f]{64})"\n'
    r"  end$",
    re.MULTILINE,
)


class HomebrewFormulaError(RuntimeError):
    """Raised when the requirements file, PyPI, or an existing Formula is unusable."""


class Dependency(NamedTuple):
    name: str
    version: str


class Excluded(NamedTuple):
    spec: str
    reason: str


class Resource(NamedTuple):
    url: str
    sha256: str


Fetcher = Callable[[str], dict]


def default_fetch_json(url: str) -> dict:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:  # noqa: S310
            payload = json.load(response)
    except URLError as error:
        raise HomebrewFormulaError(f"could not fetch {url}: {error}") from error
    except json.JSONDecodeError as error:
        raise HomebrewFormulaError(f"PyPI returned invalid JSON for {url}: {error}") from error
    if not isinstance(payload, dict):
        raise HomebrewFormulaError(f"PyPI returned a non-object response for {url}")
    return payload


def _load_tomllib():
    try:
        import tomllib

        return tomllib
    except ImportError:  # Python 3.10
        import tomli as tomllib

        return tomllib


def normalize_resource_name(name: str) -> str:
    """PEP 503 normalization: the resource name Homebrew's PyPI tooling uses."""
    return re.sub(r"[-_.]+", "-", name).lower()


def pypi_release_url(name: str, version: str) -> str:
    return f"https://pypi.org/pypi/{quote(name, safe='')}/{quote(version, safe='')}/json"


def homebrew_marker_environment(
    homebrew_python: tuple[int, ...] = HOMEBREW_PYTHON_VERSION,
) -> dict[str, str]:
    """The environment the generated Formula's single resource list targets.

    A concrete non-Intel-macOS interpreter: this makes
    ``sys_platform == "darwin" and platform_machine == "x86_64"`` false, so a
    pin guarded for that target alone drops out and the mainline pin wins.
    """
    minor = ".".join(str(part) for part in homebrew_python[:2])
    full = ".".join(str(part) for part in homebrew_python)
    if len(homebrew_python) < 3:
        full = f"{full}.0"
    environment = dict(default_environment())
    environment.update(
        {
            "python_version": minor,
            "python_full_version": full,
            "implementation_version": full,
            "sys_platform": "linux",
            "platform_system": "Linux",
            "platform_machine": "x86_64",
            "os_name": "posix",
        }
    )
    return environment


def read_dependency_specs(
    requirements: Path = NPM_BINARY_REQUIREMENTS,
) -> list[str]:
    """Non-comment, non-blank entries of ``requirements-npm-binary.txt``."""
    try:
        lines = requirements.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise HomebrewFormulaError(f"cannot read {requirements}: {error}") from error
    specs = [
        entry
        for entry in (line.strip() for line in lines)
        if entry and not entry.startswith("#")
    ]
    if not specs:
        raise HomebrewFormulaError(f"{requirements} lists no dependencies")
    return specs


def read_project_version(pyproject: Path = PYPROJECT) -> str:
    tomllib = _load_tomllib()
    with pyproject.open("rb") as handle:
        document = tomllib.load(handle)
    project = document.get("project") if isinstance(document, dict) else None
    version = project.get("version") if isinstance(project, dict) else None
    if not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version):
        raise HomebrewFormulaError(f"{pyproject} has no literal [project].version")
    return version


def parse_dependency_specs(
    specs: list[str], *, homebrew_python: tuple[int, ...] = HOMEBREW_PYTHON_VERSION
) -> tuple[list[Dependency], list[Excluded]]:
    environment = homebrew_marker_environment(homebrew_python)
    included: list[Dependency] = []
    excluded: list[Excluded] = []
    names: set[str] = set()
    for spec in specs:
        try:
            requirement = Requirement(spec)
        except InvalidRequirement as error:
            raise HomebrewFormulaError(
                f"unsupported dependency spec: {spec!r} ({error})"
            ) from error
        if canonicalize_name(requirement.name) in BUILD_TOOL_NAMES:
            continue
        if requirement.marker is not None and not requirement.marker.evaluate(environment):
            excluded.append(Excluded(spec=spec, reason=str(requirement.marker)))
            continue
        specifiers = list(requirement.specifier)
        if (
            len(specifiers) != 1
            or specifiers[0].operator != "=="
            or specifiers[0].version.endswith("*")
        ):
            raise HomebrewFormulaError(
                f"dependency must be pinned with a single ==: {spec!r}"
            )
        normalized = normalize_resource_name(requirement.name)
        if normalized in names:
            raise HomebrewFormulaError(f"duplicate active dependency: {normalized}")
        names.add(normalized)
        included.append(Dependency(name=requirement.name, version=specifiers[0].version))
    return included, excluded


def collect_dependencies(
    requirements: Path = NPM_BINARY_REQUIREMENTS,
    *,
    homebrew_python: tuple[int, ...] = HOMEBREW_PYTHON_VERSION,
) -> tuple[list[Dependency], list[Excluded]]:
    specs = read_dependency_specs(requirements)
    return parse_dependency_specs(specs, homebrew_python=homebrew_python)


def select_sdist(release: dict, name: str, version: str) -> Resource:
    info = release.get("info")
    if not isinstance(info, dict) or info.get("version") != version:
        raise HomebrewFormulaError(f"PyPI returned a different version for {name}")
    files = release.get("urls")
    if not isinstance(files, list):
        raise HomebrewFormulaError(f"PyPI release for {name}=={version} has no file list")
    candidates = [
        file
        for file in files
        if isinstance(file, dict)
        and file.get("packagetype") == "sdist"
        and file.get("yanked") is not True
    ]
    if len(candidates) != 1:
        raise HomebrewFormulaError(
            f"expected exactly one non-yanked sdist for {name}=={version}, "
            f"found {len(candidates)}"
        )
    candidate = candidates[0]
    url = candidate.get("url")
    digests = candidate.get("digests")
    sha256 = digests.get("sha256") if isinstance(digests, dict) else None
    if not isinstance(url, str) or not url.startswith("https://"):
        raise HomebrewFormulaError(f"PyPI sdist for {name}=={version} has no HTTPS URL")
    if not isinstance(sha256, str) or not SHA256_PATTERN.fullmatch(sha256):
        raise HomebrewFormulaError(f"PyPI sdist for {name}=={version} has an invalid SHA-256")
    return Resource(url=url, sha256=sha256)


def resolve_resource(name: str, version: str, fetch: Fetcher) -> Resource:
    release = fetch(pypi_release_url(name, version))
    return select_sdist(release, name, version)


def latest_pypi_version(fetch: Fetcher = default_fetch_json) -> str:
    release = fetch(f"https://pypi.org/pypi/{PYPI_PACKAGE_NAME}/json")
    releases = release.get("releases")
    if not isinstance(releases, dict) or not releases:
        raise HomebrewFormulaError(f"PyPI has no releases for {PYPI_PACKAGE_NAME}")
    published: list[tuple[int, ...]] = []
    by_tuple: dict[tuple[int, ...], str] = {}
    for version, files in releases.items():
        if not VERSION_PATTERN.fullmatch(version):
            continue
        if not isinstance(files, list) or not files:
            continue
        if all(isinstance(f, dict) and f.get("yanked") is True for f in files):
            continue
        key = tuple(int(part) for part in version.split("."))
        published.append(key)
        by_tuple[key] = version
    if not published:
        raise HomebrewFormulaError(
            f"PyPI has no published, non-yanked {PYPI_PACKAGE_NAME} release"
        )
    return by_tuple[max(published)]


def _python_formula(python_version: tuple[int, ...]) -> str:
    if len(python_version) != 2 or any(
        type(part) is not int or part < 0 for part in python_version
    ):
        raise HomebrewFormulaError("Homebrew Python must have a major and minor version")
    return "python@" + ".".join(map(str, python_version))


def render_formula(
    version: str,
    *,
    requirements: Path = NPM_BINARY_REQUIREMENTS,
    fetch: Fetcher = default_fetch_json,
    homebrew_python: tuple[int, ...] = HOMEBREW_PYTHON_VERSION,
) -> str:
    if not VERSION_PATTERN.fullmatch(version):
        raise HomebrewFormulaError(f"invalid OMM version: {version!r}")

    python_formula = _python_formula(homebrew_python)
    deps, excluded = collect_dependencies(requirements, homebrew_python=homebrew_python)
    main = resolve_resource(PYPI_PACKAGE_NAME, version, fetch)

    lines: list[str] = []
    lines.append(f"class {CLASS_NAME} < Formula")
    lines.append("  include Language::Python::Virtualenv")
    lines.append("")
    lines.append(f'  desc "{DESC}"')
    lines.append(f'  homepage "{HOMEPAGE}"')
    lines.append(f'  url "{main.url}"')
    lines.append(f'  sha256 "{main.sha256}"')
    lines.append(f'  license "{LICENSE}"')
    lines.append("")
    lines.append("  livecheck do")
    lines.append("    url :stable")
    lines.append("    strategy :pypi")
    lines.append("  end")
    lines.append("")
    for dep in BUILD_DEPENDS_ON:
        lines.append(f'  depends_on "{dep}" => :build')
    for dep in RUNTIME_DEPENDS_ON:
        lines.append(f'  depends_on "{dep}"')
    lines.append(f'  depends_on "{python_formula}"')
    lines.append("")
    lines.append(f'  pypi_packages package_name: "{PYPI_PACKAGE_NAME}"')
    lines.append("")

    if excluded:
        lines.append(
            "  # Excluded from the resource list below - marker not satisfied for"
        )
        lines.append(f"  # {python_formula} (Homebrew's declared interpreter here):")
        for item in sorted(excluded, key=lambda e: e.spec):
            lines.append(f"  #   {item.spec}")
        lines.append("")

    resolved = {
        normalize_resource_name(dep.name): resolve_resource(dep.name, dep.version, fetch)
        for dep in deps
    }
    for resource_name in sorted(resolved):
        resource = resolved[resource_name]
        lines.append(f'  resource "{resource_name}" do')
        lines.append(f'    url "{resource.url}"')
        lines.append(f'    sha256 "{resource.sha256}"')
        lines.append("  end")
        lines.append("")

    lines.append("  def install")
    lines.append("    virtualenv_install_with_resources")
    lines.append("  end")
    lines.append("")
    lines.append("  test do")
    lines.append('    assert_match version.to_s, shell_output("#{bin}/omm --version")')
    lines.append('    assert_match "Example usage:", shell_output("#{bin}/omm --help")')
    lines.append("  end")
    lines.append("end")
    lines.append("")
    return "\n".join(lines)


def _parse_existing_formula(
    text: str,
) -> tuple[str | None, str | None, str | None, dict[str, Resource]]:
    """Return (version, url, sha256, {resource_name: Resource}) from Formula text."""
    source_matches = list(SOURCE_PATTERN.finditer(text))
    if len(source_matches) != 1:
        raise HomebrewFormulaError(
            f"expected exactly one top-level OMM source block, found {len(source_matches)}"
        )
    source = source_matches[0]
    resources: dict[str, Resource] = {}
    matches = list(RESOURCE_PATTERN.finditer(text))
    declarations = list(re.finditer(r"^[ \t]*resource\b", text, re.MULTILINE))
    if len(declarations) != len(matches):
        raise HomebrewFormulaError("unsupported resource block in formula; refusing a partial check")
    for match in matches:
        name = match.group("name")
        if name in resources:
            raise HomebrewFormulaError(f"duplicate resource in formula: {name}")
        resources[name] = Resource(url=match.group("url"), sha256=match.group("sha256"))
    return source.group("version"), source.group("url"), source.group("sha256"), resources


def check_formula(
    formula_path: Path,
    version: str,
    *,
    requirements: Path = NPM_BINARY_REQUIREMENTS,
    fetch: Fetcher = default_fetch_json,
    homebrew_python: tuple[int, ...] = HOMEBREW_PYTHON_VERSION,
    allow_version_lag: bool = False,
) -> None:
    text = formula_path.read_text(encoding="utf-8")
    actual_version, actual_url, actual_sha256, actual_resources = _parse_existing_formula(text)
    expected_python = _python_formula(homebrew_python)
    python_declarations = re.findall(
        r"^[ \t]*depends_on\s+[^\n]*\bpython[^\n]*$", text, re.MULTILINE,
    )
    if python_declarations != [f'  depends_on "{expected_python}"']:
        raise HomebrewFormulaError(
            f"Homebrew Python must be declared exactly once as {expected_python}"
        )

    problems: list[str] = []

    if not allow_version_lag:
        main = resolve_resource(PYPI_PACKAGE_NAME, version, fetch)
        if actual_version != version:
            problems.append(f"OMM version: formula={actual_version!r} expected={version!r}")
        elif actual_url != main.url or actual_sha256 != main.sha256:
            problems.append(
                "main package pin drifted:\n"
                f"    formula:  url={actual_url} sha256={actual_sha256}\n"
                f"    expected: url={main.url} sha256={main.sha256}"
            )

    deps, _excluded = collect_dependencies(requirements, homebrew_python=homebrew_python)
    expected_resources = {
        normalize_resource_name(dep.name): resolve_resource(dep.name, dep.version, fetch)
        for dep in deps
    }

    missing = sorted(expected_resources.keys() - actual_resources.keys())
    extra = sorted(actual_resources.keys() - expected_resources.keys())
    changed = sorted(
        name
        for name in expected_resources.keys() & actual_resources.keys()
        if expected_resources[name] != actual_resources[name]
    )

    if missing:
        problems.append("missing from formula: " + ", ".join(missing))
    if extra:
        problems.append("extra in formula (not in requirements-npm-binary.txt): " + ", ".join(extra))
    for name in changed:
        problems.append(
            f"{name} pin drifted:\n"
            f"    formula:  url={actual_resources[name].url} sha256={actual_resources[name].sha256}\n"
            f"    expected: url={expected_resources[name].url} sha256={expected_resources[name].sha256}"
        )

    if problems:
        header = f"Homebrew formula drift detected in {formula_path}:"
        raise HomebrewFormulaError(header + "\n  - " + "\n  - ".join(problems))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    render_parser = subparsers.add_parser("render", help="Render omm.rb for a given OMM version")
    render_parser.add_argument("--version", required=True)
    render_parser.add_argument("--output", type=Path, default=None)
    render_parser.add_argument("--requirements", type=Path, default=NPM_BINARY_REQUIREMENTS)

    check_parser = subparsers.add_parser(
        "check", help="Verify an existing Formula matches requirements-npm-binary.txt"
    )
    check_parser.add_argument("--formula", type=Path, required=True)
    check_parser.add_argument("--version", required=True)
    check_parser.add_argument("--requirements", type=Path, default=NPM_BINARY_REQUIREMENTS)
    check_parser.add_argument(
        "--allow-version-lag",
        action="store_true",
        help="Compare only the dependency pin set/hashes, not the OMM version/main sdist",
    )

    subparsers.add_parser("pypi-latest", help="Print the latest published omm-model version")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "render":
            text = render_formula(args.version, requirements=args.requirements)
            if args.output is not None:
                args.output.write_text(text, encoding="utf-8")
                print(f"Wrote {args.output}")
            else:
                sys.stdout.write(text)
        elif args.command == "check":
            check_formula(
                args.formula,
                args.version,
                requirements=args.requirements,
                allow_version_lag=args.allow_version_lag,
            )
            print(f"{args.formula} matches {args.requirements}")
        elif args.command == "pypi-latest":
            print(latest_pypi_version())
        else:  # pragma: no cover - argparse constrains the command
            raise HomebrewFormulaError(f"unknown command: {args.command}")
    except HomebrewFormulaError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
