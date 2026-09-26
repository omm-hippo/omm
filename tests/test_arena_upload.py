from __future__ import annotations

import json

import pytest

from omm import arena_upload, config


def _local_row(**overrides):
    """A full sub-project A votes.jsonl row."""
    row = {
        "battle_id": "0a8c1f22-5c1e-4a0e-9d3b-1f2e3d4c5b6a",
        "timestamp": "2026-09-26T04:05:06.700000+00:00",
        "prompt": "our internal deploy runbook, summarize it",
        "model_a": "alpha-4b-Q4_K_M.gguf",
        "model_b": "beta-8b-Q5_K_M.gguf",
        "engine_a": "ollama",
        "engine_b": "ollama",
        "elapsed_a": 3.25,
        "elapsed_b": 9.5,
        "tokens_a": 120,
        "tokens_b": 300,
        "memory_gb_a": 3.4,
        "memory_gb_b": None,
        "tokens_per_second_a": 41.2,
        "tokens_per_second_b": None,
        "watt_a": None,
        "watt_b": None,
        "winner": "a",
        "pinned": False,
    }
    row.update(overrides)
    return row


def _registry():
    return {
        "alpha-4b-Q4_K_M.gguf": {
            "provider": "huggingface",
            "repo_id": "org/alpha-gguf",
            "sha256": "a" * 64,
            "quantization_bits": 4,
        },
        "beta-8b-Q5_K_M.gguf": {"provider": "modelscope"},
    }


def test_payload_never_carries_the_prompt(isolated_omm_home):
    """The single most important assertion in this sub-project."""
    payload = arena_upload.build_payload(_local_row(), registry_entries=_registry())
    assert "prompt" not in payload
    serialized = json.dumps(payload)
    assert "internal deploy runbook" not in serialized
    # ...and no hash or length of it either.
    assert not [k for k in payload if "prompt" in k]


def test_payload_fields_are_all_allow_listed(isolated_omm_home):
    payload = arena_upload.build_payload(_local_row(), registry_entries=_registry())
    assert set(payload) <= arena_upload.WIRE_FIELDS
    assert payload["schema_version"] == 1
    assert payload["battle_id"] == "0a8c1f22-5c1e-4a0e-9d3b-1f2e3d4c5b6a"
    assert payload["recorded_at"] == "2026-09-26T04:05:06.700000+00:00"
    assert payload["engine"] == "ollama"
    assert payload["winner"] == "a"
    assert payload["pinned"] is False
    assert payload["client_id"] == config.client_id()


def test_payload_ignores_an_unknown_field_from_a_future_local_schema(isolated_omm_home):
    """A's local row will gain fields. A copy-then-delete builder would
    forward each new one to a validator that rejects unknown keys."""
    payload = arena_upload.build_payload(
        _local_row(some_future_field="x", another={"nested": 1}),
        registry_entries=_registry(),
    )
    assert "some_future_field" not in payload
    assert "another" not in payload
    assert set(payload) <= arena_upload.WIRE_FIELDS


def test_payload_fills_model_identity_from_the_registry(isolated_omm_home):
    payload = arena_upload.build_payload(_local_row(), registry_entries=_registry())
    assert payload["model_filename_a"] == "alpha-4b-Q4_K_M.gguf"
    assert payload["model_provider_a"] == "huggingface"
    assert payload["model_repo_id_a"] == "org/alpha-gguf"
    assert payload["model_digest_a"] == "a" * 64
    assert payload["quant_bits_a"] == 4


def test_payload_omits_identity_fields_the_registry_lacks(isolated_omm_home):
    payload = arena_upload.build_payload(_local_row(), registry_entries=_registry())
    assert payload["model_filename_b"] == "beta-8b-Q5_K_M.gguf"
    assert payload["model_provider_b"] == "modelscope"
    # The key is absent, not present-and-None: usage._post_to strips None
    # before signing, so a None here would silently vanish anyway - being
    # explicit keeps the wire shape checkable.
    assert "model_repo_id_b" not in payload
    assert "model_digest_b" not in payload
    assert "quant_bits_b" not in payload


def test_payload_never_sends_watts(isolated_omm_home):
    """Sub-project B measures no power; C is the first consumer."""
    payload = arena_upload.build_payload(
        _local_row(watt_a=180.0, watt_b=200.0), registry_entries=_registry()
    )
    assert "watt_a" not in payload
    assert "watt_b" not in payload


def test_payload_omits_optional_measurements_symmetrically(isolated_omm_home):
    """The Worker validator rejects an asymmetric row, so a measurement
    missing on one side must drop the other side's too - otherwise the row
    is silently dropped server-side forever."""
    payload = arena_upload.build_payload(_local_row(), registry_entries=_registry())
    # memory_gb_b and tokens_per_second_b are None in the fixture.
    assert "memory_gb_a" not in payload and "memory_gb_b" not in payload
    assert "tokens_per_second_a" not in payload
    assert "tokens_per_second_b" not in payload
    # Required-on-both fields stay.
    assert payload["elapsed_a"] == 3.25 and payload["elapsed_b"] == 9.5
    assert payload["tokens_a"] == 120 and payload["tokens_b"] == 300


def test_payload_keeps_a_symmetric_optional_measurement(isolated_omm_home):
    payload = arena_upload.build_payload(
        _local_row(memory_gb_b=7.1, tokens_per_second_b=31.5),
        registry_entries=_registry(),
    )
    assert payload["memory_gb_a"] == 3.4 and payload["memory_gb_b"] == 7.1
    assert payload["tokens_per_second_a"] == 41.2
    assert payload["tokens_per_second_b"] == 31.5


def test_payload_is_none_for_a_row_missing_a_required_field(isolated_omm_home):
    for missing in ("battle_id", "winner", "model_a", "engine_a"):
        row = _local_row()
        del row[missing]
        assert arena_upload.build_payload(row, registry_entries=_registry()) is None


def test_payload_is_none_when_the_two_engines_disagree(isolated_omm_home):
    """A session is single-engine. A row saying otherwise came from a
    future cross-engine mode this wire schema cannot express (one `engine`)."""
    row = _local_row(engine_b="lmstudio")
    assert arena_upload.build_payload(row, registry_entries=_registry()) is None


def test_payload_is_none_for_an_unknown_winner(isolated_omm_home):
    assert (
        arena_upload.build_payload(_local_row(winner="tie"), registry_entries=_registry())
        is None
    )


def test_policy_reads_the_config_key(isolated_omm_home):
    assert arena_upload.policy({"arena_vote_send_policy": "always"}) == "always"
    assert arena_upload.policy({"arena_vote_send_policy": "never"}) == "never"
    assert arena_upload.policy({}) == "ask"
    assert arena_upload.policy({"arena_vote_send_policy": "nonsense"}) == "ask"
