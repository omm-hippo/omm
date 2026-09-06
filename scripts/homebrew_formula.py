#!/usr/bin/env python3
"""Generate and verify the Homebrew Formula from the frozen ``pyproject.toml``.

``brew install omm-hippo/omm/omm`` has repeatedly drifted from the dependency
table `pyproject.toml` actually pins (issue #238): a plain ``pip install`` /
``npm install`` reproduces the frozen closure, but the Homebrew Formula's
``resource`` stanzas were hand-bumped and fell behind.

This module makes the Formula a generated artifact of ``pyproject.toml``
instead of a second, independently maintained copy of the dependency table:

- ``render``       build ``omm.rb`` text for a given OMM version from the
                    current ``[project].dependencies`` closure, resolving
                    each pin's sdist URL/sha256 from PyPI.
- ``check``        compare an existing Formula file against what ``render``
                    would produce and fail loudly on any drift.
- ``pypi-latest``  print the latest published, non-yanked ``omm-model``
                    version on PyPI.

Only ``python_version`` markers are supported in ``[project].dependencies``
(the same restriction ``omm update``'s ``_dependency_spec_applies`` imposes -
see the freeze note in ``pyproject.toml``). A dependency whose marker excludes
it for Homebrew's declared Python is left out of the generated resource list
with an explanatory comment - never silently dropped without a trace.
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


ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"

PYPI_PACKAGE_NAME = "omm-model"
CLASS_NAME = "Omm"
DESC = "Package manager for local large language models"
HOMEPAGE = "https://github.com/omm-hippo/omm"
LICENSE = "MIT"

# Homebrew's declared interpreter for this Formula. Not derivable from
# pyproject.toml's `requires-python` floor (>=3.10) - it is Homebrew
# packaging policy, tracked here so `python_version` markers can be
# evaluated against the interpreter Homebrew will actually use.
HOMEBREW_PYTHON = "python@3.14"
HOMEBREW_PYTHON_VERSION = (3, 14)

# Homebrew-specific build/runtime deps needed to compile `cryptography`
# (rust) and `cffi` (libffi) from source. Not present in pyproject.toml -
# this is Homebrew packaging knowledge, mirrored from the tap's current
# Formula/omm.rb rather than derived from the frozen dependency table.
BUILD_DEPENDS_ON = ["pkgconf", "rust"]
RUNTIME_DEPENDS_ON = ["libffi", "openssl@3"]

REQUEST_TIMEOUT_SECONDS = 20
USER_AGENT = "omm-homebrew-formula-generator"

VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SPEC_PATTERN = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"==(?P<version>[0-9][0-9A-Za-z.+!-]*)"
    r"(?:\s*;\s*(?P<marker>.+))?$"
)
MARKER_PATTERN = re.compile(
    r"^python_version\s*(?P<op><=|>=|==|!=|<|>)\s*'(?P<value>[0-9]+(?:\.[0-9]+)*)'$"
)
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
    """Raised when pyproject.toml, PyPI, or an existing Formula is unusable."""


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


def read_dependency_specs(pyproject: Path = PYPROJECT) -> list[str]:
    """Raw ``[project].dependencies`` entries, e.g. ``"click==8.5.0"``.

    This is the frozen closure spelled out by commit a8cfbde (issue #239):
    every runtime dependency - direct and transitive - already listed flat,
    each hard-pinned with ``==``. No separate "frozen set" helper exists
    elsewhere in the repo to reuse; this list *is* the frozen set.
    """
    tomllib = _load_tomllib()
    try:
        with pyproject.open("rb") as handle:
            document = tomllib.load(handle)
    except OSError as error:
        raise HomebrewFormulaError(f"cannot read {pyproject}: {error}") from error
    except Exception as error:  # tomllib.TOMLDecodeError subclasses ValueError
        raise HomebrewFormulaError(f"cannot parse {pyproject}: {error}") from error

    project = document.get("project") if isinstance(document, dict) else None
    dependencies = project.get("dependencies") if isinstance(project, dict) else None
    if not isinstance(dependencies, list) or not all(
        isinstance(spec, str) and spec.strip() for spec in dependencies
    ):
        raise HomebrewFormulaError(
            f"{pyproject} has no usable [project].dependencies list"
        )
    return list(dependencies)


def read_project_version(pyproject: Path = PYPROJECT) -> str:
    tomllib = _load_tomllib()
    with pyproject.open("rb") as handle:
        document = tomllib.load(handle)
    project = document.get("project") if isinstance(document, dict) else None
    version = project.get("version") if isinstance(project, dict) else None
    if not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version):
        raise HomebrewFormulaError(f"{pyproject} has no literal [project].version")
    return version


def _marker_applies(marker: str, python_version: tuple[int, ...]) -> bool:
    match = MARKER_PATTERN.fullmatch(marker.strip())
    if match is None:
        # Fail loudly rather than silently keep or drop a dependency guarded
        # by a marker shape this generator does not understand.
        raise HomebrewFormulaError(
            f"unsupported environment marker (refusing to guess): {marker!r}"
        )
    op = match.group("op")
    target = tuple(int(part) for part in match.group("value").split("."))
    # Compare only as many components as the marker specifies.
    lhs = python_version[: len(target)]
    if op == "<":
        return lhs < target
    if op == "<=":
        return lhs <= target
    if op == ">":
        return lhs > target
    if op == ">=":
        return lhs >= target
    if op == "==":
        return lhs == target
    if op == "!=":
        return lhs != target
    raise HomebrewFormulaError(f"unsupported marker operator: {op!r}")  # pragma: no cover


def parse_dependency_specs(
    specs: list[str], *, homebrew_python: tuple[int, ...] = HOMEBREW_PYTHON_VERSION
) -> tuple[list[Dependency], list[Excluded]]:
    included: list[Dependency] = []
    excluded: list[Excluded] = []
    for spec in specs:
        match = SPEC_PATTERN.fullmatch(spec.strip())
        if match is None:
            raise HomebrewFormulaError(
                f"unsupported dependency spec (expected name==version[; marker]): {spec!r}"
            )
        name = match.group("name")
        version = match.group("version")
        marker = match.group("marker")
        if marker is not None and not _marker_applies(marker, homebrew_python):
            excluded.append(Excluded(spec=spec.strip(), reason=marker.strip()))
            continue
        included.append(Dependency(name=name, version=version))
    return included, excluded


def collect_dependencies(
    pyproject: Path = PYPROJECT,
    *,
    homebrew_python: tuple[int, ...] = HOMEBREW_PYTHON_VERSION,
) -> tuple[list[Dependency], list[Excluded]]:
    specs = read_dependency_specs(pyproject)
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


def render_formula(
    version: str,
    *,
    pyproject: Path = PYPROJECT,
    fetch: Fetcher = default_fetch_json,
    homebrew_python: tuple[int, ...] = HOMEBREW_PYTHON_VERSION,
) -> str:
    if not VERSION_PATTERN.fullmatch(version):
        raise HomebrewFormulaError(f"invalid OMM version: {version!r}")

    deps, excluded = collect_dependencies(pyproject, homebrew_python=homebrew_python)
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
    lines.append(f'  depends_on "{HOMEBREW_PYTHON}"')
    lines.append("")
    lines.append(f'  pypi_packages package_name: "{PYPI_PACKAGE_NAME}"')
    lines.append("")

    if excluded:
        lines.append(
            "  # Excluded from the resource list below - marker not satisfied for"
        )
        lines.append(f"  # {HOMEBREW_PYTHON} (Homebrew's declared interpreter here):")
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
    for match in RESOURCE_PATTERN.finditer(text):
        name = match.group("name")
        if name in resources:
            raise HomebrewFormulaError(f"duplicate resource in formula: {name}")
        resources[name] = Resource(url=match.group("url"), sha256=match.group("sha256"))
    return source.group("version"), source.group("url"), source.group("sha256"), resources


def check_formula(
    formula_path: Path,
    version: str,
    *,
    pyproject: Path = PYPROJECT,
    fetch: Fetcher = default_fetch_json,
    homebrew_python: tuple[int, ...] = HOMEBREW_PYTHON_VERSION,
    allow_version_lag: bool = False,
) -> None:
    text = formula_path.read_text(encoding="utf-8")
    actual_version, actual_url, actual_sha256, actual_resources = _parse_existing_formula(text)

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

    deps, _excluded = collect_dependencies(pyproject, homebrew_python=homebrew_python)
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
        problems.append("extra in formula (not in pyproject.toml): " + ", ".join(extra))
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
    render_parser.add_argument("--pyproject", type=Path, default=PYPROJECT)

    check_parser = subparsers.add_parser(
        "check", help="Verify an existing Formula matches pyproject.toml"
    )
    check_parser.add_argument("--formula", type=Path, required=True)
    check_parser.add_argument("--version", required=True)
    check_parser.add_argument("--pyproject", type=Path, default=PYPROJECT)
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
            text = render_formula(args.version, pyproject=args.pyproject)
            if args.output is not None:
                args.output.write_text(text, encoding="utf-8")
                print(f"Wrote {args.output}")
            else:
                sys.stdout.write(text)
        elif args.command == "check":
            check_formula(
                args.formula,
                args.version,
                pyproject=args.pyproject,
                allow_version_lag=args.allow_version_lag,
            )
            print(f"{args.formula} matches {args.pyproject}")
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
