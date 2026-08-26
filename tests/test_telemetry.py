import json

import pytest
import requests

from omm import config, firebase_auth, telemetry


class _FakeResp:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


def test_send_event_skips_when_not_opted_in_and_not_forced(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(
        telemetry,
        "load_config",
        lambda: {"telemetry_send_policy": "ask", "telemetry_endpoint": "https://example.com"},
    )
    called = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: called.append((a, k)))

    result = telemetry.send_event({"x": 1})

    assert called == []
    assert result is False


def test_send_event_sends_when_forced_even_if_not_opted_in(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(
        telemetry,
        "load_config",
        lambda: {"telemetry_send_policy": "ask", "telemetry_endpoint": "https://example.com"},
    )
    called = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: called.append((a, k)) or _FakeResp(200))

    result = telemetry.send_event({"x": 1}, force=True)

    assert len(called) == 1
    assert result is True


def test_send_event_forced_still_requires_endpoint(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(
        telemetry,
        "load_config",
        lambda: {"telemetry_send_policy": "ask", "telemetry_endpoint": None},
    )
    called = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: called.append((a, k)))

    result = telemetry.send_event({"x": 1}, force=True)

    assert called == []
    assert result is False


def test_one_shot_forced_failure_is_not_queued_for_unattended_retry(
    isolated_omm_home, monkeypatch
):
    monkeypatch.setattr(
        telemetry,
        "load_config",
        lambda: {"telemetry_send_policy": "ask", "telemetry_endpoint": "https://example.com"},
    )
    monkeypatch.setattr(
        requests,
        "post",
        lambda *a, **k: (_ for _ in ()).throw(requests.RequestException("boom")),
    )

    assert telemetry.send_event({"x": 1}, force=True) is False
    assert not (isolated_omm_home / "telemetry_pending.json").exists()
    diagnostic = json.loads((isolated_omm_home / "telemetry_last_failed.json").read_text())
    assert diagnostic["event"] == {"x": 1}
    assert diagnostic["failure"]["outcome"] == "send_failed_network"


def test_contribute_retries_only_transient_failures_within_current_consent(
    isolated_omm_home, monkeypatch
):
    monkeypatch.setattr(
        telemetry,
        "load_config",
        lambda: {"telemetry_send_policy": "ask", "telemetry_endpoint": "https://example.com"},
    )
    responses = iter([_FakeResp(500), _FakeResp(429), _FakeResp(200)])
    calls = []
    sleeps = []
    monkeypatch.setattr(
        requests, "post", lambda *a, **k: calls.append((a, k)) or next(responses)
    )
    monkeypatch.setattr(telemetry.time, "sleep", sleeps.append)
    event = {"benchmark_version": 9, "measurement_profile": "contribute-v1"}

    assert telemetry.send_event(event, force=True) is True
    assert len(calls) == 3
    assert sleeps == [0.5, 1.0]
    status = telemetry.last_send_status()
    assert status is not None
    assert status.outcome == "sent_ok"
    assert status.attempts == 3
    assert not (isolated_omm_home / "telemetry_last_failed.json").exists()


def test_contribute_does_not_retry_permanent_401_and_saves_exact_payload(
    isolated_omm_home, monkeypatch
):
    monkeypatch.setattr(
        telemetry,
        "load_config",
        lambda: {"telemetry_send_policy": "ask", "telemetry_endpoint": "https://example.com"},
    )
    calls = []
    monkeypatch.setattr(
        requests,
        "post",
        lambda *a, **k: calls.append((a, k)) or _FakeResp(401, "Permission denied"),
    )
    event = {
        "benchmark_version": 9,
        "measurement_profile": "contribute-v1",
        "model_installed": "model.gguf",
    }

    assert telemetry.send_event(event, force=True) is False
    assert len(calls) == 1
    status = telemetry.last_send_status()
    assert status is not None
    assert status.status_code == 401
    assert status.detail == "Permission denied"
    assert status.retryable is False
    diagnostic = json.loads((isolated_omm_home / "telemetry_last_failed.json").read_text())
    assert diagnostic["event"] == event
    assert diagnostic["failure"]["status_code"] == 401


def test_post_omits_optional_none_values_from_wire_payload(
    isolated_omm_home, monkeypatch
):
    monkeypatch.setattr(
        telemetry,
        "load_config",
        lambda: {"telemetry_endpoint": "https://example.com"},
    )
    calls = []
    monkeypatch.setattr(
        requests,
        "post",
        lambda *args, **kwargs: calls.append((args, kwargs)) or _FakeResp(200),
    )

    assert telemetry._post_event(
        {"benchmark_version": 9, "vram_gb": None, "gpu_tflops": None}
    )

    assert calls[0][1]["json"] == {"benchmark_version": 9}


def test_send_event_sends_when_opted_in_without_force(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(
        telemetry,
        "load_config",
        lambda: {"telemetry_send_policy": "always", "telemetry_endpoint": "https://example.com"},
    )
    called = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: called.append((a, k)) or _FakeResp(200))

    result = telemetry.send_event({"x": 1})

    assert len(called) == 1
    assert result is True


def test_send_event_logs_sent_ok_on_success(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(
        telemetry,
        "load_config",
        lambda: {"telemetry_send_policy": "always", "telemetry_endpoint": "https://example.com"},
    )
    monkeypatch.setattr(requests, "post", lambda *a, **k: _FakeResp(200))

    telemetry.send_event({"x": 1})

    lines = (isolated_omm_home / "telemetry.log").read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["outcome"] == "sent_ok"


def test_send_event_queues_and_logs_on_network_failure(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(
        telemetry,
        "load_config",
        lambda: {"telemetry_send_policy": "always", "telemetry_endpoint": "https://example.com"},
    )

    def raise_network_error(*a, **k):
        raise requests.RequestException("boom")

    monkeypatch.setattr(requests, "post", raise_network_error)

    result = telemetry.send_event({"model": "x"})

    assert result is False
    pending = json.loads((isolated_omm_home / "telemetry_pending.json").read_text())
    assert pending == [{"model": "x"}]
    log_lines = (isolated_omm_home / "telemetry.log").read_text().splitlines()
    assert json.loads(log_lines[0])["outcome"] == "send_failed_network"


def test_send_event_queues_and_logs_on_http_error(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(
        telemetry,
        "load_config",
        lambda: {"telemetry_send_policy": "always", "telemetry_endpoint": "https://example.com"},
    )
    monkeypatch.setattr(requests, "post", lambda *a, **k: _FakeResp(500))

    result = telemetry.send_event({"model": "y"})

    assert result is False
    pending = json.loads((isolated_omm_home / "telemetry_pending.json").read_text())
    assert pending == [{"model": "y"}]
    log_lines = (isolated_omm_home / "telemetry.log").read_text().splitlines()
    assert json.loads(log_lines[0])["outcome"] == "send_failed_http_500"


def test_flush_pending_returns_zero_when_empty(isolated_omm_home):
    assert telemetry.flush_pending() == 0


def test_flush_pending_resends_and_clears_on_success(isolated_omm_home, monkeypatch):
    config.update_config(
        telemetry_endpoint="https://example.com", telemetry_send_policy="always"
    )
    (isolated_omm_home / "telemetry_pending.json").write_text(
        json.dumps([{"model": "a"}, {"model": "b"}])
    )
    monkeypatch.setattr(requests, "post", lambda *a, **k: _FakeResp(200))

    resent = telemetry.flush_pending()

    assert resent == 2
    assert json.loads((isolated_omm_home / "telemetry_pending.json").read_text()) == []


def test_flush_preserves_event_appended_while_send_is_in_progress(
    isolated_omm_home, monkeypatch
):
    config.update_config(
        telemetry_endpoint="https://example.com", telemetry_send_policy="always"
    )
    pending_path = isolated_omm_home / "telemetry_pending.json"
    pending_path.write_text(json.dumps([{"model": "old"}]))

    def send_and_append(event):
        telemetry._append_pending({"model": "new"})
        return True

    monkeypatch.setattr(telemetry, "_post_event", send_and_append)

    assert telemetry.flush_pending() == 1
    assert json.loads(pending_path.read_text()) == [{"model": "new"}]


def test_full_queue_flush_does_not_remove_identical_new_append(
    isolated_omm_home, monkeypatch
):
    config.update_config(
        telemetry_endpoint="https://example.com", telemetry_send_policy="always"
    )
    sent = {"model": "same"}
    events = [sent, *({"model": str(index)} for index in range(999))]
    pending_path = isolated_omm_home / "telemetry_pending.json"
    pending_path.write_text(json.dumps(events))

    def send_and_append(event):
        telemetry._append_pending(dict(sent))
        return True

    monkeypatch.setattr(telemetry, "_post_event", send_and_append)

    assert telemetry.flush_pending(max_retries=1) == 1
    pending = json.loads(pending_path.read_text())
    assert len(pending) == telemetry._MAX_PENDING_EVENTS
    assert pending[-1] == sent
    assert pending.count(sent) == 1


def test_flush_pending_keeps_events_that_still_fail(isolated_omm_home, monkeypatch):
    config.update_config(
        telemetry_endpoint="https://example.com", telemetry_send_policy="always"
    )
    (isolated_omm_home / "telemetry_pending.json").write_text(json.dumps([{"model": "a"}]))
    monkeypatch.setattr(requests, "post", lambda *a, **k: _FakeResp(500))

    resent = telemetry.flush_pending()

    assert resent == 0
    assert json.loads((isolated_omm_home / "telemetry_pending.json").read_text()) == [{"model": "a"}]


def test_flush_pending_caps_attempts_per_call(isolated_omm_home, monkeypatch):
    config.update_config(
        telemetry_endpoint="https://example.com", telemetry_send_policy="always"
    )
    events = [{"model": str(i)} for i in range(5)]
    (isolated_omm_home / "telemetry_pending.json").write_text(json.dumps(events))
    calls = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: calls.append(1) or _FakeResp(200))

    resent = telemetry.flush_pending(max_retries=3)

    assert resent == 3
    assert len(calls) == 3
    remaining = json.loads((isolated_omm_home / "telemetry_pending.json").read_text())
    assert len(remaining) == 2


@pytest.mark.parametrize("max_retries", [0, -1, True, 1.5])
def test_flush_pending_rejects_invalid_retry_limits(
    isolated_omm_home, monkeypatch, max_retries
):
    config.update_config(
        telemetry_endpoint="https://example.com", telemetry_send_policy="always"
    )
    pending_path = isolated_omm_home / "telemetry_pending.json"
    pending_path.write_text(json.dumps([{"model": "private"}]))
    monkeypatch.setattr(requests, "post", lambda *a, **k: pytest.fail("must not send"))

    assert telemetry.flush_pending(max_retries=max_retries) == 0
    assert json.loads(pending_path.read_text()) == [{"model": "private"}]


def test_flush_pending_never_sends_after_user_opts_out(isolated_omm_home, monkeypatch):
    config.update_config(
        telemetry_endpoint="https://example.com", telemetry_send_policy="never"
    )
    pending_path = isolated_omm_home / "telemetry_pending.json"
    pending_path.write_text(json.dumps([{"model": "private"}]))
    calls = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: calls.append(1) or _FakeResp(200))

    assert telemetry.flush_pending() == 0
    assert calls == []
    assert json.loads(pending_path.read_text()) == [{"model": "private"}]


def test_post_event_skips_the_closed_direct_firebase_endpoint(isolated_omm_home, monkeypatch):
    """omm-hippo/omm#133: telemetry/$event denies every direct write now, so
    a config still pointed at raw Firebase (stale, or migrated away from
    mid-flight) must fail cleanly rather than send a doomed request."""
    monkeypatch.setattr(
        telemetry,
        "load_config",
        lambda: {
            "telemetry_send_policy": "always",
            "telemetry_endpoint": "https://x-default-rtdb.firebaseio.com/telemetry.json",
        },
    )
    calls = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: calls.append((a, k)))

    result = telemetry.send_event({"x": 1})

    assert result is False
    assert calls == []
    log_lines = (isolated_omm_home / "telemetry.log").read_text().splitlines()
    assert json.loads(log_lines[0])["outcome"] == "skipped_legacy_endpoint"


def test_post_event_solves_proof_of_work_for_the_gateway_endpoint(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(
        telemetry,
        "load_config",
        lambda: {
            "telemetry_send_policy": "always",
            "telemetry_endpoint": config.TELEMETRY_GATEWAY_ENDPOINT,
        },
    )
    calls = []
    monkeypatch.setattr(
        requests, "post", lambda *a, **k: calls.append((a, k)) or _FakeResp(200)
    )

    result = telemetry.send_event({"x": 1})

    assert result is True
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == config.TELEMETRY_GATEWAY_ENDPOINT
    body = kwargs["json"]
    assert set(body) == {"event_json", "timestamp", "nonce"}
    assert body["event_json"] == '{"x":1}'
    import hashlib

    digest = hashlib.sha256(
        f"{body['event_json']}:{body['timestamp']}:{body['nonce']}".encode()
    ).hexdigest()
    assert digest.startswith("0" * telemetry._POW_DIFFICULTY_PREFIX_LENGTH)


def test_post_event_reports_gateway_rejection(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(
        telemetry,
        "load_config",
        lambda: {
            "telemetry_send_policy": "always",
            "telemetry_endpoint": config.TELEMETRY_GATEWAY_ENDPOINT,
        },
    )
    monkeypatch.setattr(
        requests, "post", lambda *a, **k: _FakeResp(400, text='{"error":"invalid event"}')
    )

    result = telemetry.send_event({"x": 1})

    assert result is False
    log_lines = (isolated_omm_home / "telemetry.log").read_text().splitlines()
    assert json.loads(log_lines[0])["outcome"] == "send_failed_http_400"


def test_post_event_skips_auth_for_non_firebase_endpoint(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(
        telemetry,
        "load_config",
        lambda: {"telemetry_send_policy": "always", "telemetry_endpoint": "https://example.com"},
    )

    def fail_if_called():
        raise AssertionError("should not fetch a firebase auth token for a non-firebase endpoint")

    monkeypatch.setattr(firebase_auth, "get_id_token", lambda: fail_if_called())
    monkeypatch.setattr(requests, "post", lambda *a, **k: _FakeResp(200))

    assert telemetry.send_event({"x": 1}) is True


def test_pending_queue_is_bounded(isolated_omm_home):
    telemetry._save_pending([{"id": index} for index in range(telemetry._MAX_PENDING_EVENTS + 5)])

    pending = json.loads((isolated_omm_home / "telemetry_pending.json").read_text())
    assert len(pending) == telemetry._MAX_PENDING_EVENTS
    assert pending[0] == {"id": 5}
