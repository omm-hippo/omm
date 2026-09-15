from omm import linker


def test_existing_ownership_key_exact_match_case_sensitive():
    records = {"D:/Models/m.gguf": {"kind": "hardlink"}}
    assert (
        linker._existing_ownership_key(records, "D:/Models/m.gguf", case_insensitive=False)
        == "D:/Models/m.gguf"
    )


def test_existing_ownership_key_folds_case_when_policy_allows_it():
    records = {"D:/Models/m.gguf": {"kind": "hardlink"}}
    assert (
        linker._existing_ownership_key(records, "d:/models/m.gguf", case_insensitive=True)
        == "D:/Models/m.gguf"
    )


def test_existing_ownership_key_never_folds_case_when_policy_forbids_it():
    records = {"D:/Models/m.gguf": {"kind": "hardlink"}}
    assert (
        linker._existing_ownership_key(records, "d:/models/m.gguf", case_insensitive=False) is None
    )


def test_existing_ownership_key_does_not_match_a_different_path():
    records = {"D:/Models/m.gguf": {"kind": "hardlink"}}
    assert (
        linker._existing_ownership_key(records, "D:/Other/m.gguf", case_insensitive=True) is None
    )


def test_case_insensitive_reregistration_does_not_leave_a_ghost_record(
    isolated_omm_home, tmp_path, monkeypatch
):
    """Regression for the ghost-record bug: writing then clearing ownership
    under two different casings of the same path must leave zero records,
    not one stale entry under the original casing."""
    monkeypatch.setattr(linker, "_ownership_keys_are_case_insensitive", lambda: True)
    upper = tmp_path / "Models" / "m.gguf"
    lower = tmp_path / "models" / "m.gguf"

    linker._update_link_ownership(upper, {"kind": "hardlink", "source": None})
    linker._update_link_ownership(lower, None)

    assert linker._load_link_ownership() == {}

    # Bulk-clear path must follow the same policy.
    linker._update_link_ownership(upper, {"kind": "hardlink", "source": None})
    linker._bulk_clear_link_ownership([lower])

    assert linker._load_link_ownership() == {}


def test_case_sensitive_policy_never_clears_another_casings_record(
    isolated_omm_home, tmp_path, monkeypatch
):
    """When the policy is off (a case-sensitive filesystem), a differently
    cased path must never be treated as the same record."""
    monkeypatch.setattr(linker, "_ownership_keys_are_case_insensitive", lambda: False)
    upper = tmp_path / "Models" / "m.gguf"
    lower = tmp_path / "models" / "m.gguf"

    linker._update_link_ownership(upper, {"kind": "hardlink", "source": None})
    linker._update_link_ownership(lower, None)

    assert linker._load_link_ownership() == {linker._link_key(upper): {"kind": "hardlink", "source": None}}
