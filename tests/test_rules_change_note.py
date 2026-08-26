import json

import pytest

from omm import rules as rules_mod


RULE = {"name": "a", "min_ram_gb": 1, "min_vram_gb": 0, "description": "A"}


def test_refresh_rules_with_change_note_flags_new_data_as_changed(monkeypatch, tmp_path):
    monkeypatch.setattr(rules_mod, "RULES_PATH", tmp_path / "rules.json")
    monkeypatch.setattr(rules_mod, "fetch_rules", lambda url: [RULE])

    fetched, changed = rules_mod.refresh_rules_with_change_note("http://example.com/rules.json")

    assert changed is True
    assert fetched == [RULE]


def test_refresh_rules_with_change_note_flags_identical_refetch_as_unchanged(monkeypatch, tmp_path):
    rules_path = tmp_path / "rules.json"
    rules_path.write_text(json.dumps([RULE]))
    monkeypatch.setattr(rules_mod, "RULES_PATH", rules_path)
    monkeypatch.setattr(rules_mod, "fetch_rules", lambda url: [RULE])

    fetched, changed = rules_mod.refresh_rules_with_change_note("http://example.com/rules.json")

    assert changed is False
    assert fetched == [RULE]


def test_load_rules_falls_back_to_defaults_on_corrupt_file(monkeypatch, tmp_path):
    rules_path = tmp_path / "rules.json"
    rules_path.write_text("{not valid json")
    monkeypatch.setattr(rules_mod, "RULES_PATH", rules_path)

    rules = rules_mod.load_rules()

    assert rules == rules_mod.DEFAULT_RULES
    backups = list(tmp_path.glob("rules.json.corrupt-*"))
    assert len(backups) == 1


def test_refresh_rules_with_change_note_treats_corrupt_previous_file_as_no_prior_data(
    monkeypatch, tmp_path
):
    rules_path = tmp_path / "rules.json"
    rules_path.write_text("{not valid json")
    monkeypatch.setattr(rules_mod, "RULES_PATH", rules_path)
    monkeypatch.setattr(rules_mod, "fetch_rules", lambda url: [RULE])

    fetched, changed = rules_mod.refresh_rules_with_change_note("http://example.com/rules.json")

    assert changed is True
    assert fetched == [RULE]


def test_fetch_rules_writes_atomically(monkeypatch, tmp_path):
    """A crash/interruption mid-write must never leave a corrupt rules.json -
    fetch_rules should go through atomic_write_text like registry/config do,
    not a plain write_text."""
    rules_path = tmp_path / "rules.json"
    monkeypatch.setattr(rules_mod, "RULES_PATH", rules_path)
    write_calls = []
    original_atomic_write_text = rules_mod.atomic_write_text

    def spy(path, content):
        write_calls.append(path)
        return original_atomic_write_text(path, content)

    monkeypatch.setattr(rules_mod, "atomic_write_text", spy)

    import requests

    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return [RULE]

    monkeypatch.setattr(requests, "get", lambda url, timeout: _FakeResponse())

    rules_mod.fetch_rules("http://example.com/rules.json")

    assert write_calls == [rules_path]
    assert rules_path.exists()


def test_load_rules_falls_back_when_cached_shape_is_invalid(monkeypatch, tmp_path):
    rules_path = tmp_path / "rules.json"
    rules_path.write_text('{"not": "a list"}')
    monkeypatch.setattr(rules_mod, "RULES_PATH", rules_path)

    assert rules_mod.load_rules() == rules_mod.DEFAULT_RULES
    assert len(list(tmp_path.glob("rules.json.corrupt-*"))) == 1


def test_fetch_rules_rejects_invalid_rules_without_replacing_cache(monkeypatch, tmp_path):
    rules_path = tmp_path / "rules.json"
    rules_path.write_text(json.dumps([RULE]))
    monkeypatch.setattr(rules_mod, "RULES_PATH", rules_path)

    import requests

    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return [{"name": "missing thresholds"}]

    monkeypatch.setattr(requests, "get", lambda url, timeout: _FakeResponse())

    with pytest.raises(ValueError):
        rules_mod.fetch_rules("https://example.test/rules.json")
    assert json.loads(rules_path.read_text()) == [RULE]
