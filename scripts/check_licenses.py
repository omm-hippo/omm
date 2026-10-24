#!/usr/bin/env python3
"""Collect, classify, and gate the licenses of every third-party dependency.

Sources:

* Python: the transitive closure of `pyproject.toml` (runtime dependencies plus
  the `server`, `nvidia` and `watch` extras) and of the standalone-binary
  requirement files, read from the installed distributions' metadata
  (`importlib.metadata`; no pip-licenses needed). Development and build-only
  roots (`dev` extra, `requirements-train.txt`, `requirements-release.txt`,
  build tools) are reported separately and never fail the check.
* npm: `web-ui/package-lock.json`, `cf-worker/package-lock.json` (both record
  each package's license, so no `node_modules` is needed), and the npm
  launcher's `package.json`, which may only depend on first-party packages.

Every license is classified as permissive, weak-copyleft, strong-copyleft, or
unknown using `scripts/license_policy.json`. A shipped dependency that is not
permissive fails the run unless the policy allows that exact package and
license with a written reason.

    python scripts/check_licenses.py           # print the report
    python scripts/check_licenses.py --write   # regenerate THIRD_PARTY_NOTICES.md
    python scripts/check_licenses.py --check   # CI: policy gate + stale-file gate

Exit status: 0 clean, 1 policy violation, 2 THIRD_PARTY_NOTICES.md is stale.

Platform- or Python-version-conditional dependencies are included for every
supported target, so the notices file does not depend on the host. A
conditional dependency that the host did not install must carry a policy
override, otherwise it is reported as unknown.
"""

from __future__ import annotations

import argparse
import importlib.metadata as metadata
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

from packaging.markers import Marker
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name


ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "scripts" / "license_policy.json"
NOTICES_PATH = ROOT / "THIRD_PARTY_NOTICES.md"
PYPROJECT = ROOT / "pyproject.toml"
SHIPPED_EXTRAS = ("server", "nvidia", "watch")
BINARY_REQUIREMENTS = ("requirements-npm-binary.txt", "requirements-windows-portable.txt")
DEV_REQUIREMENTS = ("requirements-train.txt", "requirements-release.txt")
BUILD_TOOL_NAMES = frozenset({"build", "hatchling", "pyinstaller", "pyinstaller-hooks-contrib"})
FIRST_PARTY = frozenset({"omm-model"})

PERMISSIVE = "permissive"
WEAK = "weak-copyleft"
STRONG = "strong-copyleft"
UNKNOWN = "unknown"
SEVERITY = {PERMISSIVE: 0, WEAK: 1, STRONG: 2, UNKNOWN: 3}

# Marker environments covering every target omm supports (requires-python
# >=3.10; Linux, macOS, Windows). A dependency is included when its marker
# holds for any of them.
_TARGETS = [
    ("linux", "Linux", "x86_64", "posix"),
    ("darwin", "Darwin", "arm64", "posix"),
    ("darwin", "Darwin", "x86_64", "posix"),
    ("win32", "Windows", "AMD64", "nt"),
]
MARKER_ENVIRONMENTS = [
    {
        "python_version": version,
        "python_full_version": f"{version}.0",
        "sys_platform": sys_platform,
        "platform_system": system,
        "platform_machine": machine,
        "os_name": os_name,
        "implementation_name": "cpython",
        "platform_python_implementation": "CPython",
    }
    for version in ("3.10", "3.11", "3.12", "3.13", "3.14")
    for sys_platform, system, machine, os_name in _TARGETS
]


class PolicyError(RuntimeError):
    """Raised for a malformed policy file or manifest."""


# --------------------------------------------------------------------------
# License expression classification
# --------------------------------------------------------------------------

_TOKEN = re.compile(r"\(|\)|[A-Za-z0-9.+\-:]+")


def load_policy(path: Path = POLICY_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _class_table(policy: dict) -> dict[str, str]:
    table = {}
    for license_class, identifiers in policy["classes"].items():
        if license_class not in SEVERITY:
            raise PolicyError(f"unknown license class in policy: {license_class}")
        for identifier in identifiers:
            table[identifier.lower()] = license_class
    return table


def classify(expression: str | None, policy: dict) -> str:
    """Classify an SPDX-style expression. OR picks the friendliest option,
    AND the strictest, `X WITH exception` counts as X (exceptions only ever
    relax a license, and relaxations must be recorded under `allow`)."""
    if not expression or not expression.strip():
        return UNKNOWN
    table = _class_table(policy)
    tokens = _TOKEN.findall(expression)
    if "".join(tokens).replace(" ", "") != re.sub(r"\s+", "", expression):
        return UNKNOWN  # characters the grammar does not know
    position = 0

    def peek() -> str | None:
        return tokens[position] if position < len(tokens) else None

    def take() -> str:
        nonlocal position
        token = tokens[position]
        position += 1
        return token

    def parse_or() -> str:
        result = parse_and()
        while (peek() or "").upper() == "OR":
            take()
            other = parse_and()
            result = min(result, other, key=SEVERITY.__getitem__)
        return result

    def parse_and() -> str:
        result = parse_with()
        while (peek() or "").upper() == "AND":
            take()
            other = parse_with()
            result = max(result, other, key=SEVERITY.__getitem__)
        return result

    def parse_with() -> str:
        result = parse_atom()
        if (peek() or "").upper() == "WITH":
            take()
            if peek() is None or peek() in "()":
                raise ValueError("WITH needs an exception id")
            take()
        return result

    def parse_atom() -> str:
        token = peek()
        if token is None:
            raise ValueError("unexpected end")
        if token == "(":
            take()
            result = parse_or()
            if peek() != ")":
                raise ValueError("unbalanced parenthesis")
            take()
            return result
        if token == ")" or token.upper() in {"AND", "OR", "WITH"}:
            raise ValueError(f"unexpected {token}")
        take()
        return table.get(token.lower(), UNKNOWN)

    try:
        result = parse_or()
    except (ValueError, IndexError):
        return UNKNOWN
    return result if position == len(tokens) else UNKNOWN


def normalize(raw: str | None, policy: dict) -> str | None:
    """Turn a metadata License/classifier string into an SPDX-style id, or
    None when it cannot be recognized (long free text, empty, ...)."""
    if raw is None:
        return None
    text = raw.strip()
    if not text or "\n" in text or len(text) > 100:
        return None
    aliases = {key.lower(): value for key, value in policy["aliases"].items()}
    if text.lower() in aliases:
        return aliases[text.lower()]
    if classify(text, policy) != UNKNOWN:
        return text
    return None


# --------------------------------------------------------------------------
# Inventory model
# --------------------------------------------------------------------------


@dataclass
class Package:
    ecosystem: str  # "python" | "npm"
    name: str
    license: str
    license_class: str
    group: str  # "shipped" | "dev"
    component: str
    source: str = "metadata"  # metadata | override | lockfile | missing
    allowed_reason: str | None = None
    note: str | None = None

    @property
    def violation(self) -> bool:
        return self.group == "shipped" and self.license_class != PERMISSIVE and not self.allowed_reason


def _apply_policy(package: Package, section: dict) -> Package:
    allow = section.get("allow", {}).get(package.name)
    if allow and allow.get("license") == package.license:
        package.allowed_reason = allow["reason"]
    package.note = section.get("notes", {}).get(package.name)
    return package


# --------------------------------------------------------------------------
# Python
# --------------------------------------------------------------------------


def _requirement_lines(path: Path) -> list[Requirement]:
    requirements = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        try:
            requirements.append(Requirement(line))
        except InvalidRequirement as error:
            raise PolicyError(f"{path.name}: cannot parse {line!r}: {error}") from error
    return requirements


def _marker_matches(marker: Marker | None, extras: set[str]) -> tuple[bool, bool]:
    """Return (holds for some target, holds for every target)."""
    if marker is None:
        return True, True
    results = [
        any(marker.evaluate({**environment, "extra": extra}) for extra in sorted(extras) or [""])
        for environment in MARKER_ENVIRONMENTS
    ]
    return any(results), all(results)


def _distribution(name: str):
    try:
        return metadata.distribution(name)
    except metadata.PackageNotFoundError:
        return None


def python_closure(roots: list[Requirement]) -> dict[str, bool]:
    """Walk installed metadata from `roots`. Returns canonical name ->
    whether the package is reached unconditionally on every target."""
    reached: dict[str, bool] = {}
    queue = [(requirement, True) for requirement in roots]
    expanded: set[tuple[str, frozenset[str], bool]] = set()
    while queue:
        requirement, parent_unconditional = queue.pop()
        some, every = _marker_matches(requirement.marker, set())
        if not some:
            continue
        name = canonicalize_name(requirement.name)
        if name in FIRST_PARTY:
            continue
        unconditional = parent_unconditional and every
        reached[name] = reached.get(name, False) or unconditional
        extras = frozenset(canonicalize_name(extra) for extra in requirement.extras)
        key = (name, extras, reached[name])
        if key in expanded:
            continue
        expanded.add(key)
        distribution = _distribution(name)
        # A conditional package is a leaf: whether the host installed it (and
        # so whether its own requirements are visible) must not change the
        # result. Its policy override covers it.
        if distribution is None or not reached[name]:
            continue
        for line in distribution.requires or []:
            child = Requirement(line)
            some, every = _marker_matches(child.marker, set(extras))
            if some:
                queue.append((child, reached[name] and every))
    return reached


def project_roots() -> tuple[list[Requirement], list[Requirement]]:
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    optional = project["project"].get("optional-dependencies", {})
    shipped = [Requirement(line) for line in project["project"]["dependencies"]]
    for extra in SHIPPED_EXTRAS:
        shipped += [Requirement(line) for line in optional.get(extra, [])]
    dev = [Requirement(line) for line in optional.get("dev", [])]
    dev += [Requirement(line) for line in project.get("build-system", {}).get("requires", [])]
    for filename in BINARY_REQUIREMENTS:
        for requirement in _requirement_lines(ROOT / filename):
            (dev if canonicalize_name(requirement.name) in BUILD_TOOL_NAMES else shipped).append(requirement)
    for filename in DEV_REQUIREMENTS:
        path = ROOT / filename
        if path.exists():
            dev += _requirement_lines(path)
    return shipped, dev


def python_license(name: str, unconditional: bool, policy: dict) -> tuple[str, str]:
    section = policy["python"]
    override = section.get("overrides", {}).get(name)
    if override:
        return override["license"], "override"
    distribution = _distribution(name)
    if distribution is None:
        return "UNKNOWN", "missing"
    if not unconditional:
        # Only some targets install it; without an override the notices file
        # would differ between a Windows and a Linux host.
        return "UNKNOWN", "conditional-without-override"
    info = distribution.metadata
    expression = info.get("License-Expression")
    if expression and classify(expression, policy) != UNKNOWN:
        return expression.strip(), "metadata"
    from_field = normalize(info.get("License"), policy)
    if from_field:
        return from_field, "metadata"
    classifiers = []
    for classifier in info.get_all("Classifier") or []:
        if classifier.startswith("License ::"):
            mapped = normalize(classifier.split("::")[-1], policy)
            if mapped and mapped not in classifiers:
                classifiers.append(mapped)
    if classifiers:
        # Several license classifiers are read conservatively (all apply).
        return " AND ".join(classifiers), "metadata"
    return (expression or "UNKNOWN").strip(), "metadata"


def collect_python(policy: dict) -> list[Package]:
    shipped_roots, dev_roots = project_roots()
    shipped = python_closure(shipped_roots)
    embedded = policy["python"].get("embedded_build_tools", {})
    for name in embedded:
        shipped.setdefault(canonicalize_name(name), True)
    dev = {name: flag for name, flag in python_closure(dev_roots).items() if name not in shipped}
    packages = []
    for group, names in (("shipped", shipped), ("dev", dev)):
        for name, unconditional in sorted(names.items()):
            license_text, source = python_license(name, unconditional, policy)
            package = Package(
                ecosystem="python",
                name=name,
                license=license_text,
                license_class=classify(license_text, policy) if source not in {"missing", "conditional-without-override"} else UNKNOWN,
                group=group,
                component="python",
                source=source,
            )
            packages.append(_apply_policy(package, policy["python"]))
    return packages


# --------------------------------------------------------------------------
# npm
# --------------------------------------------------------------------------

NPM_LOCKFILES = {"web-ui": ROOT / "web-ui" / "package-lock.json", "cf-worker": ROOT / "cf-worker" / "package-lock.json"}
NPM_LAUNCHER = ROOT / "packaging" / "npm" / "launcher" / "package.json"


def collect_npm_lock(component: str, lock: dict, policy: dict) -> list[Package]:
    section = policy["npm"]
    seen: dict[tuple[str, str], Package] = {}
    for key, entry in sorted(lock.get("packages", {}).items()):
        if not key or entry.get("link"):
            continue
        name = entry.get("name") or key.rsplit("node_modules/", 1)[-1]
        group = "dev" if entry.get("dev") or entry.get("devOptional") else "shipped"
        override = section.get("overrides", {}).get(name)
        raw = entry.get("license")
        if isinstance(raw, dict):  # very old `{"type": ...}` form
            raw = raw.get("type")
        license_text = override["license"] if override else (raw or "UNKNOWN")
        package = Package(
            ecosystem="npm",
            name=name,
            license=license_text,
            license_class=classify(license_text, policy),
            group=group,
            component=component,
            source="override" if override else "lockfile",
        )
        previous = seen.get((name, group))
        if previous is None or SEVERITY[package.license_class] > SEVERITY[previous.license_class]:
            seen[(name, group)] = _apply_policy(package, section)
    return sorted(seen.values(), key=lambda package: (package.group, package.name))


def check_npm_launcher(manifest: dict, policy: dict) -> list[str]:
    """The launcher has no lockfile, so it may only depend on first-party
    packages (its per-platform binaries)."""
    scopes = tuple(policy["npm"].get("first_party_scopes", []))
    problems = []
    for field_name in ("dependencies", "optionalDependencies", "peerDependencies", "bundleDependencies"):
        for name in manifest.get(field_name) or {}:
            if not name.startswith(scopes):
                problems.append(f"npm launcher {field_name} has third-party package {name}; add a lockfile and review it")
    if manifest.get("license") != "MIT":
        problems.append("npm launcher package.json must declare license MIT")
    return problems


def collect_npm(policy: dict) -> tuple[list[Package], list[str]]:
    packages = []
    for component, path in NPM_LOCKFILES.items():
        packages += collect_npm_lock(component, json.loads(path.read_text(encoding="utf-8")), policy)
    problems = check_npm_launcher(json.loads(NPM_LAUNCHER.read_text(encoding="utf-8")), policy)
    return packages, problems


# --------------------------------------------------------------------------
# Report and notices
# --------------------------------------------------------------------------


@dataclass
class Result:
    packages: list[Package]
    problems: list[str] = field(default_factory=list)

    @property
    def violations(self) -> list[Package]:
        return [package for package in self.packages if package.violation]


def run(policy: dict) -> Result:
    packages = collect_python(policy)
    npm_packages, problems = collect_npm(policy)
    return Result(packages + npm_packages, problems)


def _cell(package: Package) -> str:
    return f"{package.license} ({package.note})" if package.note else package.license


def _table(packages: list[Package]) -> list[str]:
    lines = ["| Package | License |", "|---|---|"]
    lines += [f"| {package.name} | {_cell(package)} |" for package in packages]
    return lines


def _summary(packages: list[Package]) -> list[str]:
    counts = Counter(package.license for package in packages)
    lines = ["| License | Packages |", "|---|---|"]
    lines += [f"| {license_text} | {count} |" for license_text, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))]
    flagged = sorted({package.name for package in packages if package.license_class != PERMISSIVE})
    if flagged:
        lines += ["", "Not permissive (build-time only, not distributed): " + ", ".join(f"`{name}`" for name in flagged) + "."]
    return lines


def render_notices(result: Result, policy: dict) -> str:
    def pick(ecosystem: str, group: str, component: str | None = None) -> list[Package]:
        return [
            package for package in result.packages
            if package.ecosystem == ecosystem and package.group == group and (component is None or package.component == component)
        ]

    python_shipped = pick("python", "shipped")
    web_shipped = pick("npm", "shipped", "web-ui")
    worker_shipped = pick("npm", "shipped", "cf-worker")
    exceptions = [package for package in result.packages if package.group == "shipped" and package.allowed_reason]
    out = [
        "# Third-Party Notices",
        "",
        "<!-- Generated by scripts/check_licenses.py --write. Do not edit by hand. -->",
        "",
        "`omm` is licensed under the [MIT License](LICENSE). This file lists the",
        "third-party software that `omm` ships or bundles, and the license of each.",
        "It is generated from installed package metadata and npm lockfiles by",
        "`python scripts/check_licenses.py --write`; CI runs `--check` so it cannot go",
        "stale. The verification process is described in",
        "[docs/license-verification.md](docs/license-verification.md).",
        "",
        "## Python packages",
        "",
        "Runtime dependencies plus the `server`, `nvidia`, and `watch` extras from",
        "`pyproject.toml`, and everything the standalone npm / Windows portable",
        "binaries embed (`requirements-npm-binary.txt`,",
        "`requirements-windows-portable.txt`), including transitive dependencies on",
        "every supported platform and Python version.",
        "",
        *_table(python_shipped),
        "",
        "## Local browser interface (npm, `web-ui/`)",
        "",
        "Runtime packages compiled into the bundled web assets. Their complete license",
        "texts are generated by `web-ui/scripts/licenses.mjs` into",
        "`src/omm/web/static/third-party-licenses.txt` and shipped with the assets.",
        "",
        *_table(web_shipped),
        "",
        "Build-time packages (Vite and its toolchain) are not shipped:",
        "",
        *_summary(pick("npm", "dev", "web-ui")),
        "",
        "## Telemetry gateway (npm, `cf-worker/`)",
        "",
        "The Cloudflare Worker runs as a hosted service and is not distributed to",
        "users. "
        + ("It has no runtime npm dependencies; its build and test tools are:" if not worker_shipped else "Runtime npm dependencies:"),
        "",
        *(_table(worker_shipped) + [""] if worker_shipped else []),
        *_summary(pick("npm", "dev", "cf-worker")),
        "",
        "## npm launcher (`packaging/npm/launcher`)",
        "",
        "The launcher has no third-party npm dependencies. Its optional dependencies",
        "are the first-party `@omm-hippo/omm-*` platform packages, which contain the",
        "standalone binary built from the Python packages listed above.",
        "",
        "## Bundled runtime components",
        "",
        "| Component | License | Where |",
        "|---|---|---|",
        *[f"| {item['name']} | {item['license']} | {item['where']} |" for item in policy.get("bundled_runtime_components", [])],
        "",
        "## Recorded exceptions",
        "",
        "Shipped packages that are not plainly permissive, and why they are accepted",
        "(`scripts/license_policy.json`):",
        "",
        *[f"- `{package.name}` ({package.license}): {package.allowed_reason}" for package in exceptions],
        "",
        "## Separately installed local runner applications",
        "",
        "`omm` does not bundle or link these programs. It installs them through their",
        "official channels or finds a copy the user installed, and talks to them as",
        "separate processes. They keep their own licenses and terms.",
        "",
        "| Application | License | Source |",
        "|---|---|---|",
        *[f"| {item['name']} | {item['license']} | {item['source']} |" for item in policy.get("external_tools", [])],
        "",
        "Model weights downloaded by `omm` carry their own licenses (for example",
        "Llama, Qwen, Gemma licenses). `omm info` shows the license reported by the",
        "model source, and the local browser interface shows each model's license label.",
        "",
    ]
    return "\n".join(out)


def render_report(result: Result) -> str:
    lines = []
    for group in ("shipped", "dev"):
        for ecosystem in ("python", "npm"):
            packages = [package for package in result.packages if package.group == group and package.ecosystem == ecosystem]
            counts = Counter(package.license_class for package in packages)
            summary = ", ".join(f"{name} {counts[name]}" for name in SEVERITY if counts[name])
            lines.append(f"{group:7} {ecosystem:6} {len(packages):4} packages: {summary or '-'}")
    lines.append("")
    for package in result.packages:
        if package.license_class == PERMISSIVE:
            continue
        status = "VIOLATION" if package.violation else ("allowed" if package.allowed_reason else "report")
        lines.append(
            f"{status:9} {package.group:7} {package.component:9} {package.name}: {package.license} "
            f"[{package.license_class}, {package.source}]"
        )
    for problem in result.problems:
        lines.append(f"VIOLATION {problem}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="regenerate THIRD_PARTY_NOTICES.md")
    mode.add_argument("--check", action="store_true", help="fail when THIRD_PARTY_NOTICES.md is stale")
    args = parser.parse_args(argv)

    policy = load_policy()
    result = run(policy)
    print(render_report(result))
    status = 1 if result.violations or result.problems else 0
    if status:
        print("\nLicense policy violations found. Fix the dependency or record an exception with a reason "
              "in scripts/license_policy.json.", file=sys.stderr)
    notices = render_notices(result, policy)
    if args.write:
        NOTICES_PATH.write_text(notices, encoding="utf-8", newline="\n")
        print(f"\nwrote {NOTICES_PATH.relative_to(ROOT)}")
    elif args.check:
        current = NOTICES_PATH.read_text(encoding="utf-8").replace("\r\n", "\n") if NOTICES_PATH.exists() else ""
        if current != notices:
            print("\nTHIRD_PARTY_NOTICES.md is stale. Run: python scripts/check_licenses.py --write", file=sys.stderr)
            return status or 2
    return status


if __name__ == "__main__":
    sys.exit(main())
