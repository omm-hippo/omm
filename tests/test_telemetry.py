import json

import requests

from omm import config, telemetry


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


def test_pending_queue_is_bounded(isolated_omm_home):
    telemetry._save_pending([{"id": index} for index in range(telemetry._MAX_PENDING_EVENTS + 5)])

    pending = json.loads((isolated_omm_home / "telemetry_pending.json").read_text())
    assert len(pending) == telemetry._MAX_PENDING_EVENTS
    assert pending[0] == {"id": 5}
