import json

import pytest
import requests

from omm import config, error_report

TELEMETRY_URL = "https://localfit-8ab57-default-rtdb.firebaseio.com/telemetry.json"
ERROR_REPORT_URL = config.ERROR_REPORTS_ENDPOINT


class _FakeResp:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


@pytest.fixture(autouse=True)
def _reset_run_consent():
    """The one-shot consent is process-global by design (a single run has a
    single answer), so it has to be reset between tests."""
    error_report.set_run_consent(None)
    yield
    error_report.set_run_consent(None)


@pytest.fixture(autouse=True)
def _no_hardware_scan(monkeypatch):
    """Reports must never trigger a hardware scan; keep the process-wide
    snapshot empty so tests can prove the payload survives without one."""
    monkeypatch.setattr("omm.hardware._last_scan", None, raising=False)


def _write_config(**changes):
    # `telemetry_backend` has to be explicit: a config that omits it and
    # names the legacy Firebase URL is migrated to local-only on load
    # (config._merge_config), which would silently strip the endpoint these
    # tests are about.
    config.save_config(
        {
            "telemetry_endpoint": TELEMETRY_URL,
            "telemetry_backend": "firebase_legacy",
            **changes,
        }
    )


def test_scrub_paths_replaces_a_macos_home_directory_with_a_tilde():
    scrubbed = error_report.scrub_paths(
        "could not open /Users/alice/.omm/models/model-Q4.gguf"
    )

    assert scrubbed == "could not open ~/.omm/models/model-Q4.gguf"
    assert "alice" not in scrubbed


def test_scrub_paths_replaces_a_linux_home_directory_with_a_tilde():
    scrubbed = error_report.scrub_paths("open /home/bob-2/.omm/models/x.gguf failed")

    assert scrubbed == "open ~/.omm/models/x.gguf failed"
    assert "bob" not in scrubbed


def test_scrub_paths_replaces_a_windows_home_directory_with_a_tilde():
    scrubbed = error_report.scrub_paths(r"[WinError 5] C:\Users\Carol Doe\.omm\models\x.gguf")

    assert scrubbed == r"[WinError 5] ~\.omm\models\x.gguf"
    assert "Carol" not in scrubbed
    assert "Doe" not in scrubbed


def test_scrub_paths_replaces_an_exact_windows_home_directory():
    assert error_report.scrub_paths(r"C:\Users\Carol Doe") == "~"


def test_scrub_paths_handles_every_platform_in_one_message():
    scrubbed = error_report.scrub_paths(
        r"tried /Users/alice/a, /home/bob/b and D:\Users\carol\c"
    )

    assert scrubbed == r"tried ~/a, ~/b and ~\c"


def test_scrub_paths_leaves_paths_without_a_user_component_alone():
    assert error_report.scrub_paths("/opt/models/x.gguf") == "/opt/models/x.gguf"
    assert error_report.scrub_paths("") == ""


def test_endpoint_rejects_legacy_direct_firebase_destination():
    assert error_report.endpoint({"telemetry_endpoint": TELEMETRY_URL}) is None


def test_endpoint_keeps_the_host_and_only_rewrites_the_last_segment():
    derived = error_report.endpoint({"telemetry_endpoint": "https://team.example.com/v1/telemetry"})

    assert derived == "https://team.example.com/v1/error_reports"


def test_endpoint_is_none_without_a_configured_telemetry_endpoint():
    assert error_report.endpoint({"telemetry_endpoint": None}) is None
    assert error_report.enabled({"telemetry_endpoint": None}) is False


def test_endpoint_is_none_when_the_telemetry_path_is_not_recognized():
    assert error_report.endpoint({"telemetry_endpoint": "https://example.com/collect"}) is None


def test_endpoint_is_none_for_an_insecure_telemetry_endpoint():
    assert error_report.endpoint({"telemetry_endpoint": "http://example.com/telemetry.json"}) is None


@pytest.mark.parametrize(
    "telemetry_endpoint",
    [
        "https://project.firebaseio.com/telemetry.json",
        "https://project-default-rtdb.firebasedatabase.app/telemetry.json",
    ],
)
def test_legacy_firebase_error_reports_never_attempt_direct_writes(
    telemetry_endpoint, monkeypatch
):
    monkeypatch.setattr(requests, "post", _unexpected_post)

    assert error_report._post_report(
        error_report.build_report(RuntimeError("boom"), trigger="crash"),
        {"telemetry_endpoint": telemetry_endpoint},
    ) is False


def test_unset_policy_queues_nothing_at_all(isolated_omm_home):
    _write_config()

    queued = error_report.queue_report(RuntimeError("boom"), trigger="crash")

    assert queued is None
    assert error_report.pending_count() == 0
    assert not error_report._log_path().exists()


def test_never_policy_silently_skips_the_trigger(isolated_omm_home):
    _write_config(error_report_send_policy="never")

    queued = error_report.queue_report(RuntimeError("boom"), trigger="crash")

    assert queued is None
    assert error_report.pending_count() == 0


def test_ask_policy_queues_without_sending_anything(isolated_omm_home, monkeypatch):
    _write_config(error_report_send_policy="ask")
    monkeypatch.setattr(requests, "post", _unexpected_post)

    queued = error_report.queue_report(RuntimeError("boom"), trigger="crash")

    assert queued is not None
    assert error_report.pending_count() == 1


def test_always_policy_queues_without_sending_from_the_trigger(isolated_omm_home, monkeypatch):
    _write_config(error_report_send_policy="always")
    monkeypatch.setattr(requests, "post", _unexpected_post)

    error_report.queue_report(RuntimeError("boom"), trigger="crash")

    assert error_report.pending_count() == 1


def test_one_run_consent_lets_an_unconfigured_policy_queue(isolated_omm_home):
    _write_config()
    error_report.set_run_consent(True)

    assert error_report.queue_report(RuntimeError("boom"), trigger="crash") is not None
    assert error_report.pending_count() == 1


def test_one_run_consent_cannot_override_an_explicit_never(isolated_omm_home):
    _write_config(error_report_send_policy="never")
    error_report.set_run_consent(True)

    assert error_report.queue_report(RuntimeError("boom"), trigger="crash") is None
    assert error_report.pending_count() == 0


def test_declining_the_prompt_stops_queueing_for_the_rest_of_the_run(isolated_omm_home):
    _write_config(error_report_send_policy="ask")
    error_report.set_run_consent(False)

    assert error_report.queue_report(RuntimeError("boom"), trigger="crash") is None
    assert error_report.pending_count() == 0


def test_queueing_is_skipped_when_no_endpoint_can_be_derived(isolated_omm_home):
    config.save_config({"telemetry_endpoint": None, "error_report_send_policy": "always"})

    assert error_report.queue_report(RuntimeError("boom"), trigger="crash") is None
    assert error_report.pending_count() == 0


def test_a_queued_report_carries_only_allow_listed_fields(isolated_omm_home):
    _write_config(error_report_send_policy="always")

    report = error_report.queue_report(
        ValueError("no such file /Users/alice/models/x.gguf"),
        trigger="install_quality_eval",
        catalog_ref=error_report.catalog_ref("org/model-GGUF", "model-Q4.gguf"),
        engine="ollama",
    )

    assert set(report) <= {
        "schema_version",
        "error_type",
        "error_message",
        "trigger",
        "subcommand",
        "client_version",
        "os_name",
        "os_version",
        "cpu_arch",
        "cpu_score",
        "cpu_tier",
        "gpu_score",
        "gpu_tier",
        "catalog_ref",
        "engine",
        "recorded_at",
    }
    assert report["error_type"] == "ValueError"
    assert report["trigger"] == "install_quality_eval"
    assert report["catalog_ref"] == "org/model-GGUF:model-Q4.gguf"


def test_client_version_uses_central_distribution_metadata(monkeypatch):
    monkeypatch.setattr(error_report.package_metadata, "version", lambda: "0.2.119")

    assert error_report._client_version() == "0.2.119"


def test_a_queued_report_never_carries_a_user_directory(isolated_omm_home):
    _write_config(error_report_send_policy="always")

    report = error_report.queue_report(
        OSError(r"cannot read C:\Users\alice\.omm\models\x.gguf"), trigger="crash"
    )

    assert "alice" not in json.dumps(report)


def test_a_queued_report_never_carries_a_traceback(isolated_omm_home):
    _write_config(error_report_send_policy="always")
    try:
        raise RuntimeError("boom")
    except RuntimeError as error:
        report = error_report.queue_report(error, trigger="crash")

    assert "traceback" not in report
    assert "Traceback" not in json.dumps(report)


def test_catalog_ref_uses_catalog_coordinates_not_a_local_path():
    assert error_report.catalog_ref("org/model", "x.gguf") == "org/model:x.gguf"
    assert error_report.catalog_ref(None, "x.gguf") == "x.gguf"
    assert error_report.catalog_ref("org/model", None) is None


def test_a_long_error_message_is_truncated_to_the_documented_cap(isolated_omm_home):
    _write_config(error_report_send_policy="always")

    report = error_report.queue_report(RuntimeError("x" * 5000), trigger="crash")

    assert len(report["error_message"]) == 2000


def test_flush_sends_queued_reports_to_the_derived_endpoint(isolated_omm_home, monkeypatch):
    _write_config(error_report_send_policy="always")
    error_report.queue_report(RuntimeError("boom"), trigger="crash")
    calls = []
    monkeypatch.setattr(
        requests, "post", lambda url, **kwargs: calls.append((url, kwargs)) or _FakeResp(200)
    )
    monkeypatch.setattr("omm.firebase_auth.get_id_token", lambda: "token")

    sent = error_report.flush_pending()

    assert sent == 1
    assert calls[0][0] == ERROR_REPORT_URL
    body = calls[0][1]["json"]
    assert set(body) == {"event_json", "timestamp", "nonce"}
    assert error_report.pending_count() == 0


def test_flush_keeps_a_failed_report_queued_for_a_later_run(isolated_omm_home, monkeypatch):
    _write_config(error_report_send_policy="always")
    error_report.queue_report(RuntimeError("boom"), trigger="crash")
    monkeypatch.setattr(requests, "post", lambda *a, **k: _FakeResp(500, "server error"))
    monkeypatch.setattr("omm.firebase_auth.get_id_token", lambda: "token")

    assert error_report.flush_pending() == 0
    assert error_report.pending_count() == 1


def test_flush_sends_nothing_under_ask_until_the_user_is_asked(isolated_omm_home, monkeypatch):
    _write_config(error_report_send_policy="ask")
    error_report.queue_report(RuntimeError("boom"), trigger="crash")
    monkeypatch.setattr(requests, "post", _unexpected_post)

    assert error_report.flush_pending() == 0
    assert error_report.pending_count() == 1


def test_flush_sends_the_backlog_once_a_run_grants_consent(isolated_omm_home, monkeypatch):
    _write_config(error_report_send_policy="ask")
    error_report.queue_report(RuntimeError("boom"), trigger="crash")
    monkeypatch.setattr(requests, "post", lambda *a, **k: _FakeResp(200))
    monkeypatch.setattr("omm.firebase_auth.get_id_token", lambda: "token")

    assert error_report.flush_pending(force=True) == 1


def test_flush_refuses_to_send_even_when_forced_after_an_explicit_opt_out(
    isolated_omm_home, monkeypatch
):
    _write_config(error_report_send_policy="always")
    error_report.queue_report(RuntimeError("boom"), trigger="crash")
    config.update_config(error_report_send_policy="never")
    monkeypatch.setattr(requests, "post", _unexpected_post)

    assert error_report.flush_pending(force=True) == 0


def test_discarding_the_queue_reports_how_many_were_dropped(isolated_omm_home):
    _write_config(error_report_send_policy="always")
    error_report.queue_report(RuntimeError("one"), trigger="crash")
    error_report.queue_report(RuntimeError("two"), trigger="crash")

    assert error_report.discard_pending() == 2
    assert error_report.pending_count() == 0


def test_preview_shows_a_queued_report_rather_than_an_example(isolated_omm_home):
    _write_config(error_report_send_policy="always")
    error_report.queue_report(RuntimeError("the real one"), trigger="crash")

    report, is_example = error_report.preview_report()

    assert is_example is False
    assert report["error_message"] == "the real one"
    assert json.loads(error_report.preview_text(report)) == report


def _unexpected_post(*args, **kwargs):
    raise AssertionError("a trigger must never perform a network call")
