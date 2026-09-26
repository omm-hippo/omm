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


def _stub_post(monkeypatch, *, status=200, raises=None):
    sent = []

    class _Resp:
        status_code = status
        text = ""

    class _Requests:
        class RequestException(Exception):
            pass

        @staticmethod
        def post(endpoint, json=None, timeout=None):
            if raises is not None:
                raise raises
            sent.append((endpoint, json))
            return _Resp()

    import sys

    monkeypatch.setitem(sys.modules, "requests", _Requests)
    monkeypatch.setattr("omm.telemetry._solve_proof_of_work", lambda event_json: (1, 2))
    return sent


def test_enqueue_writes_one_payload_per_row(isolated_omm_home):
    assert arena_upload.enqueue([_local_row(), _local_row()]) == 2
    assert arena_upload.pending_count() == 2
    lines = arena_upload._pending_path().read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert "prompt" not in json.loads(lines[0])


def test_enqueue_skips_a_row_it_cannot_convert(isolated_omm_home):
    assert arena_upload.enqueue([_local_row(winner="tie"), _local_row()]) == 1
    assert arena_upload.pending_count() == 1


def test_enqueue_never_raises_on_a_disk_error(isolated_omm_home, monkeypatch):
    """`omm arena`'s session end calls this; a disk problem must not change
    the command's exit code or lose the local vote row."""

    def _boom(*args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(arena_upload.Path, "mkdir", _boom)
    assert arena_upload.enqueue([_local_row()]) == 0


def test_enqueue_does_nothing_under_a_never_policy(isolated_omm_home):
    config.update_config(arena_vote_send_policy="never")
    assert arena_upload.enqueue([_local_row()]) == 0
    assert arena_upload.pending_count() == 0


def test_discard_pending_empties_the_queue(isolated_omm_home):
    arena_upload.enqueue([_local_row(), _local_row()])
    assert arena_upload.discard_pending() == 2
    assert arena_upload.pending_count() == 0
    assert not arena_upload._pending_path().exists()


def test_flush_sends_queued_rows_under_an_ask_policy(isolated_omm_home, monkeypatch):
    """The one-time-consent path: `y` queues rows and leaves the policy
    "ask". A flush that demanded "always" would strand them forever."""
    config.update_config(arena_vote_send_policy="ask")
    arena_upload.enqueue([_local_row(), _local_row()])
    sent = _stub_post(monkeypatch)
    assert arena_upload.flush_pending() == 2
    assert len(sent) == 2
    assert all(endpoint == config.VOTES_GATEWAY_ENDPOINT for endpoint, _ in sent)
    assert arena_upload.pending_count() == 0


def test_flush_discards_without_sending_when_the_policy_is_never(
    isolated_omm_home, monkeypatch
):
    arena_upload.enqueue([_local_row()])
    config.update_config(arena_vote_send_policy="never")
    _stub_post(monkeypatch, raises=AssertionError("must not POST"))
    assert arena_upload.flush_pending() == 0
    assert arena_upload.pending_count() == 0


def test_flush_on_an_empty_queue_does_no_http(isolated_omm_home, monkeypatch):
    _stub_post(monkeypatch, raises=AssertionError("must not POST"))
    assert arena_upload.flush_pending() == 0


def test_flush_refuses_a_foreign_endpoint(isolated_omm_home, monkeypatch):
    """This channel has no self-hosted variant; only the shared Worker."""
    _stub_post(monkeypatch, raises=AssertionError("must not POST"))
    assert arena_upload._post_to("https://evil.example/votes", {"a": 1}) is False


def test_an_http_error_sets_a_backoff_and_keeps_the_queue(isolated_omm_home, monkeypatch):
    arena_upload.enqueue([_local_row()])
    _stub_post(monkeypatch, status=500)
    assert arena_upload.flush_pending() == 0
    assert arena_upload.pending_count() == 1
    # Backoff is now active, so a second call sends nothing at all.
    _stub_post(monkeypatch, raises=AssertionError("must not POST during backoff"))
    assert arena_upload.flush_pending() == 0
    assert arena_upload.pending_count() == 1


def test_force_ignores_the_backoff(isolated_omm_home, monkeypatch):
    arena_upload.enqueue([_local_row()])
    _stub_post(monkeypatch, status=500)
    arena_upload.flush_pending()
    sent = _stub_post(monkeypatch)
    assert arena_upload.flush_pending(force=True) == 1
    assert len(sent) == 1


def test_a_row_queued_during_a_slow_send_survives(isolated_omm_home, monkeypatch):
    arena_upload.enqueue([_local_row()])
    late = _local_row(battle_id="11111111-2222-4333-8444-555555555555")

    class _Resp:
        status_code = 200
        text = ""

    class _Requests:
        class RequestException(Exception):
            pass

        @staticmethod
        def post(endpoint, json=None, timeout=None):
            arena_upload.enqueue([late])
            return _Resp()

    import sys

    monkeypatch.setitem(sys.modules, "requests", _Requests)
    monkeypatch.setattr("omm.telemetry._solve_proof_of_work", lambda event_json: (1, 2))
    assert arena_upload.flush_pending() == 1
    remaining = [
        json.loads(line)
        for line in arena_upload._pending_path().read_text(encoding="utf-8").splitlines()
    ]
    assert [r["battle_id"] for r in remaining] == [late["battle_id"]]


def test_a_losing_flush_race_gives_up_instead_of_waiting(isolated_omm_home, monkeypatch):
    """Two omm processes must not both POST the same queue, and the loser
    must not stall a user-facing command."""
    from omm.atomic import locked as real_locked

    arena_upload.enqueue([_local_row()])
    _stub_post(monkeypatch, raises=AssertionError("must not POST while locked"))
    pending = arena_upload._pending_path()
    flush_lock = pending.with_name(pending.name + ".flush")
    with real_locked(flush_lock, timeout=10):
        assert arena_upload.flush_pending() == 0
    assert arena_upload.pending_count() == 1


def test_a_corrupt_pending_file_does_not_crash(isolated_omm_home, monkeypatch):
    arena_upload._pending_path().parent.mkdir(parents=True, exist_ok=True)
    arena_upload._pending_path().write_text("{not json\n", encoding="utf-8")
    assert arena_upload.pending_count() == 0
    _stub_post(monkeypatch)
    assert arena_upload.flush_pending() == 0


def test_the_queue_drops_the_oldest_rows_past_the_cap(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(arena_upload, "_PENDING_MAX", 2)
    rows = [
        _local_row(battle_id=f"{i:08d}-2222-4333-8444-555555555555") for i in range(4)
    ]
    arena_upload.enqueue(rows)
    queued = [
        json.loads(line)["battle_id"]
        for line in arena_upload._pending_path().read_text(encoding="utf-8").splitlines()
    ]
    assert queued == [rows[2]["battle_id"], rows[3]["battle_id"]]


def test_the_attempt_log_is_local_and_bounded(isolated_omm_home):
    for i in range(arena_upload._MAX_LOG_LINES + 20):
        arena_upload.log_attempt("sent_ok", f"row {i}")
    lines = arena_upload._log_path().read_text(encoding="utf-8").splitlines()
    assert len(lines) <= arena_upload._MAX_LOG_LINES
