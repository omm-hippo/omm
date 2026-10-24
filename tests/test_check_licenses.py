from __future__ import annotations

from email.message import Message

import pytest
from packaging.requirements import Requirement

from scripts import check_licenses as cl


@pytest.fixture(scope="module")
def policy():
    return cl.load_policy()


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("MIT", cl.PERMISSIVE),
        ("mit", cl.PERMISSIVE),
        ("Apache-2.0 OR BSD-3-Clause", cl.PERMISSIVE),
        ("MPL-2.0", cl.WEAK),
        ("LGPL-3.0-or-later", cl.WEAK),
        ("GPL-3.0-only", cl.STRONG),
        ("AGPL-3.0", cl.STRONG),
        # OR takes the friendliest option, AND the strictest.
        ("MIT OR GPL-3.0-only", cl.PERMISSIVE),
        ("MIT AND GPL-3.0-only", cl.STRONG),
        ("Apache-2.0 AND LGPL-3.0-or-later AND MIT", cl.WEAK),
        ("(MIT OR GPL-2.0-only) AND MPL-2.0", cl.WEAK),
        # An exception never makes an expression permissive on its own.
        ("GPL-2.0-or-later WITH Bootloader-exception", cl.STRONG),
        ("BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0", cl.PERMISSIVE),
    ],
)
def test_classify_expressions(policy, expression, expected):
    assert cl.classify(expression, policy) == expected


@pytest.mark.parametrize(
    "expression",
    ["", None, "UNKNOWN", "SEE LICENSE IN LICENSE.md", "MIT OR", "(MIT", "MIT AND AND BSD", "WTFPL", "MIT WITH"],
)
def test_unrecognized_or_malformed_is_unknown(policy, expression):
    assert cl.classify(expression, policy) == cl.UNKNOWN


def test_unknown_operand_poisons_and_but_not_or(policy):
    assert cl.classify("MIT AND Custom-1.0", policy) == cl.UNKNOWN
    assert cl.classify("MIT OR Custom-1.0", policy) == cl.PERMISSIVE


def test_normalize_maps_classifiers_and_rejects_license_text(policy):
    assert cl.normalize("MIT License", policy) == "MIT"
    assert cl.normalize("Mozilla Public License 2.0 (MPL 2.0)", policy) == "MPL-2.0"
    assert cl.normalize("BSD-3-Clause", policy) == "BSD-3-Clause"
    assert cl.normalize("Copyright (c) 2001 Someone\nRedistribution and use ...", policy) is None
    assert cl.normalize("GPLv2-or-later with a special exception", policy) is None


def _package(group, license_text, policy, name="pkg"):
    package = cl.Package("python", name, license_text, cl.classify(license_text, policy), group, "python")
    return cl._apply_policy(package, policy["python"])


def test_shipped_copyleft_or_unknown_is_a_violation_dev_is_only_reported(policy):
    assert _package("shipped", "GPL-3.0-only", policy).violation
    assert _package("shipped", "MPL-2.0", policy).violation
    assert _package("shipped", "UNKNOWN", policy).violation
    assert not _package("shipped", "MIT", policy).violation
    assert not _package("dev", "GPL-3.0-only", policy).violation
    assert not _package("dev", "UNKNOWN", policy).violation


def test_allowlist_matches_exact_package_and_license(policy):
    allowed = _package("shipped", "MPL-2.0", policy, name="certifi")
    assert allowed.allowed_reason and not allowed.violation
    # The same package under a different license is flagged again.
    assert _package("shipped", "GPL-3.0-only", policy, name="certifi").violation


def test_every_policy_exception_has_a_reason(policy):
    for ecosystem in ("python", "npm"):
        for name, entry in {**policy[ecosystem]["overrides"], **policy[ecosystem]["allow"]}.items():
            assert entry.get("license") and entry.get("reason", "").strip(), name


def test_npm_lock_splits_shipped_and_dev(policy):
    lock = {
        "packages": {
            "": {"name": "app"},
            "node_modules/react": {"version": "19", "license": "MIT"},
            "node_modules/vite": {"version": "8", "license": "MIT", "dev": True},
            "node_modules/lightningcss": {"version": "1", "license": "MPL-2.0", "dev": True},
            "node_modules/opt": {"version": "1", "license": "ISC", "devOptional": True},
            "node_modules/bad": {"version": "1", "license": "AGPL-3.0-only"},
            "node_modules/a/node_modules/react": {"version": "18", "license": "MIT"},
        }
    }
    packages = {(p.name, p.group): p for p in cl.collect_npm_lock("web-ui", lock, policy)}
    assert set(packages) == {("react", "shipped"), ("vite", "dev"), ("lightningcss", "dev"), ("opt", "dev"), ("bad", "shipped")}
    assert packages[("bad", "shipped")].violation
    assert not packages[("lightningcss", "dev")].violation


def test_npm_lock_missing_license_is_unknown(policy):
    lock = {"packages": {"node_modules/x": {"version": "1"}}}
    (package,) = cl.collect_npm_lock("web-ui", lock, policy)
    assert package.license_class == cl.UNKNOWN and package.violation


def test_npm_launcher_only_allows_first_party_dependencies(policy):
    ok = {"license": "MIT", "optionalDependencies": {"@omm-hippo/omm-linux-x64-gnu": "1"}}
    assert cl.check_npm_launcher(ok, policy) == []
    bad = {"license": "MIT", "dependencies": {"left-pad": "1"}}
    assert cl.check_npm_launcher(bad, policy)


class _FakeDistribution:
    def __init__(self, requires=(), **fields):
        self.requires = list(requires)
        self.metadata = Message()
        for key, value in fields.items():
            values = value if isinstance(value, list) else [value]
            for item in values:
                self.metadata[key.replace("_", "-")] = item


def test_closure_marks_platform_conditional_packages_as_leaves(monkeypatch):
    distributions = {
        "top": _FakeDistribution(["always", 'winonly; sys_platform == "win32"', 'never; python_version < "3.0"']),
        "always": _FakeDistribution(),
        "winonly": _FakeDistribution(["winchild"]),
        "winchild": _FakeDistribution(),
    }
    monkeypatch.setattr(cl, "_distribution", distributions.get)
    reached = cl.python_closure([Requirement("top")])
    assert reached == {"top": True, "always": True, "winonly": False}


def test_conditional_package_needs_override(monkeypatch, policy):
    monkeypatch.setattr(cl, "_distribution", lambda name: _FakeDistribution(License_Expression="MIT"))
    assert cl.python_license("winonly", False, policy) == ("UNKNOWN", "conditional-without-override")
    assert cl.python_license("tomli", False, policy) == ("MIT", "override")
    assert cl.python_license("anything", True, policy) == ("MIT", "metadata")


def test_python_license_falls_back_to_field_then_classifiers(monkeypatch, policy):
    monkeypatch.setattr(cl, "_distribution", lambda name: _FakeDistribution(License="Apache 2.0"))
    assert cl.python_license("a", True, policy) == ("Apache-2.0", "metadata")
    monkeypatch.setattr(
        cl,
        "_distribution",
        lambda name: _FakeDistribution(Classifier=["License :: OSI Approved :: MIT License", "Programming Language :: Python"]),
    )
    assert cl.python_license("b", True, policy) == ("MIT", "metadata")
    monkeypatch.setattr(cl, "_distribution", lambda name: None)
    assert cl.python_license("c", True, policy) == ("UNKNOWN", "missing")
