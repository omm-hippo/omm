import pytest

from scripts import verify_managed_cask as check


def metadata(monkeypatch, sha="no_check"):
    monkeypatch.setattr(check.install_safety, "_json", lambda args: {"casks": [{
        "token": "mstystudio", "tap": "homebrew/cask", "sha256": sha}]})


def test_unhashed_cask_must_be_refused_before_installation(monkeypatch, capsys):
    metadata(monkeypatch)
    def blocked(*args):
        raise check.install_safety.SafetyError("Official fixed-version cask with HTTPS and SHA-256 is required")
    monkeypatch.setattr(check.install_safety, "prepare", blocked)
    monkeypatch.setattr(check.subprocess, "run", lambda *a, **kw: pytest.fail("installer must never execute"))
    assert check.verify("mstystudio") == "blocked"
    assert "installation/link/import NOT VERIFIED" in capsys.readouterr().out


def test_accepting_unhashed_cask_fails_the_check(monkeypatch):
    metadata(monkeypatch)
    monkeypatch.setattr(check.install_safety, "prepare", lambda *args: [])
    with pytest.raises(RuntimeError, match="accepted an unhashed cask"):
        check.verify("mstystudio")


def test_other_preflight_failure_is_not_treated_as_expected_block(monkeypatch):
    metadata(monkeypatch)
    def blocked(*args):
        raise check.install_safety.SafetyError("Gatekeeper assessment must be enabled")
    monkeypatch.setattr(check.install_safety, "prepare", blocked)
    with pytest.raises(check.install_safety.SafetyError, match="Gatekeeper"):
        check.verify("mstystudio")


def test_hashed_cask_uses_real_install_link_verifier(monkeypatch):
    metadata(monkeypatch, "a" * 64)
    calls = []
    monkeypatch.setattr(check.subprocess, "run", lambda args, **kwargs: calls.append((args, kwargs)))
    assert check.verify("mstystudio") == "installed_and_linked"
    assert calls[0][0][-1] == "mstystudio"
    assert calls[0][0][-2].endswith("verify_engine_link.py")
    assert calls[0][1]["check"] is True
