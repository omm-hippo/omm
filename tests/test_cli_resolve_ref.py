"""F066: `_resolve_ref` used `str.isdigit()` to detect a numbered result
reference, which is also true for characters like the superscript "²"
that `int()` cannot parse - crashing `omm install ²` (and any other command
routed through `_resolve_ref`) with an unhandled ValueError whenever the
session cache was non-empty. `str.isdecimal()` is true only for the digits
`int()` actually accepts (plain ASCII and full-width digits included), so it
closes the gap without needing a try/except around `int(arg)`."""

from __future__ import annotations

from omm import cli


def test_resolve_ref_passes_superscript_digits_through_instead_of_crashing(monkeypatch):
    monkeypatch.setattr(cli.session_cache, "load_last_results", lambda: ["model.gguf"])

    assert cli._resolve_ref("²") == "²"
    assert cli._resolve_ref("1") == "model.gguf"
    # Full-width digits are isdecimal()==True and int() parses them, so the
    # existing by-index behavior for them is preserved.
    assert cli._resolve_ref("１") == "model.gguf"
