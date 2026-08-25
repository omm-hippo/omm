import errno
import threading
from types import SimpleNamespace

import pytest
import requests
from typer.testing import CliRunner

from omm import cli, config, error_report, hardware, registry
from omm.hub import ResolvedModel

runner = CliRunner()

TELEMETRY_URL = "https://localfit-8ab57-default-rtdb.firebaseio.com/telemetry.json"
ERROR_REPORT_URL = "https://localfit-8ab57-default-rtdb.firebaseio.com/error_reports.json"


@pytest.fixture(autouse=True)
def _reset_run_consent():
    error_report.set_run_consent(None)
    yield
    error_report.set_run_consent(None)


@pytest.fixture(autouse=True)
def _never_send_from_a_trigger(monkeypatch):
    """No trigger may reach the network: reports are queued and sent later,
    so a `requests.post` from a trigger site is a bug, not a slow test."""
    def _explode(*args, **kwargs):
        raise AssertionError("a trigger must never perform a network call")

    monkeypatch.setattr(requests, "post", _explode)


def _enable_endpoint(**changes):
    config.save_config(
        {
            "telemetry_endpoint": TELEMETRY_URL,
            "telemetry_backend": "firebase_legacy",
            "telemetry_send_policy": "always",
            **changes,
        }
    )


# --- omm setting error-reports ---------------------------------------------


def test_setting_error_reports_enable_saves_the_always_policy(isolated_omm_home):
    _enable_endpoint()

    result = runner.invoke(cli.app, ["setting", "error-reports", "--enable"])

    assert result.exit_code == 0, result.stdout
    assert config.load_config()["error_report_send_policy"] == "always"


def test_setting_error_reports_ask_saves_the_ask_policy(isolated_omm_home):
    _enable_endpoint()

    result = runner.invoke(cli.app, ["setting", "error-reports", "--ask"])

    assert result.exit_code == 0, result.stdout
    assert config.load_config()["error_report_send_policy"] == "ask"


def test_setting_error_reports_disable_saves_never_and_drops_the_queue(isolated_omm_home):
    _enable_endpoint(error_report_send_policy="always")
    error_report.queue_report(RuntimeError("boom"), trigger="crash")
    assert error_report.pending_count() == 1

    result = runner.invoke(cli.app, ["setting", "error-reports", "--disable"])

    assert result.exit_code == 0, result.stdout
    assert config.load_config()["error_report_send_policy"] == "never"
    assert error_report.pending_count() == 0


def test_setting_error_reports_enable_requires_a_telemetry_endpoint(isolated_omm_home):
    config.save_config({"telemetry_endpoint": None, "telemetry_backend": "local"})

    result = runner.invoke(cli.app, ["setting", "error-reports", "--enable"])

    assert result.exit_code == 1
    assert config.load_config()["error_report_send_policy"] is None


def test_setting_error_reports_rejects_more_than_one_policy_flag(isolated_omm_home):
    _enable_endpoint()

    result = runner.invoke(cli.app, ["setting", "error-reports", "--enable", "--disable"])

    assert result.exit_code == 1
    assert config.load_config()["error_report_send_policy"] is None


def test_setting_error_reports_shows_the_derived_write_only_destination(isolated_omm_home):
    _enable_endpoint()

    result = runner.invoke(cli.app, ["setting", "error-reports"])

    assert result.exit_code == 0, result.stdout
    assert "Error report policy" in result.stdout
    assert "never (default)" in " ".join(result.stdout.split())
    assert error_report.endpoint() == ERROR_REPORT_URL


# --- consent resolution at `omm contribute` start ---------------------------


def test_report_errors_flag_consents_for_one_run_without_saving_a_policy(isolated_omm_home):
    _enable_endpoint()

    assert cli._resolve_error_report_decision(True) is True
    assert config.load_config()["error_report_send_policy"] is None


def test_report_errors_flag_is_ignored_after_an_explicit_opt_out(isolated_omm_home, capsys):
    _enable_endpoint(error_report_send_policy="never")

    assert cli._resolve_error_report_decision(True) is False
    assert "ignored" in capsys.readouterr().err


def test_report_errors_flag_is_a_no_op_when_reports_are_already_always_on(isolated_omm_home):
    _enable_endpoint(error_report_send_policy="always")

    assert cli._resolve_error_report_decision(False) is True
    assert cli._resolve_error_report_decision(True) is True


def test_no_flag_and_no_saved_policy_means_no_reports(isolated_omm_home, monkeypatch):
    _enable_endpoint()
    monkeypatch.setattr(
        cli,
        "_ask_upload_choice",
        lambda prompt: (_ for _ in ()).throw(AssertionError("must not ask when opted out")),
    )

    assert cli._resolve_error_report_decision(False) is False


def test_ask_policy_previews_the_payload_before_asking(isolated_omm_home, monkeypatch, capsys):
    _enable_endpoint(error_report_send_policy="ask")
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_ask_upload_choice", lambda prompt: "yes")

    assert cli._resolve_error_report_decision(False) is True
    out = capsys.readouterr().out
    assert '"trigger"' in out
    assert '"error_message"' in out
    # A one-off "yes" is consent for this run, never a saved policy.
    assert config.load_config()["error_report_send_policy"] == "ask"


def test_ask_policy_answering_always_saves_the_policy(isolated_omm_home, monkeypatch):
    _enable_endpoint(error_report_send_policy="ask")
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_ask_upload_choice", lambda prompt: "always")

    assert cli._resolve_error_report_decision(False) is True
    assert config.load_config()["error_report_send_policy"] == "always"


def test_ask_policy_answering_no_declines_without_saving(isolated_omm_home, monkeypatch):
    _enable_endpoint(error_report_send_policy="ask")
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_ask_upload_choice", lambda prompt: "no")

    assert cli._resolve_error_report_decision(False) is False
    assert config.load_config()["error_report_send_policy"] == "ask"


def test_ask_policy_never_assumes_consent_without_a_usable_prompt(isolated_omm_home, monkeypatch):
    _enable_endpoint(error_report_send_policy="ask")
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
    monkeypatch.setattr(
        cli,
        "_ask_upload_choice",
        lambda prompt: (_ for _ in ()).throw(AssertionError("no prompt is possible")),
    )

    assert cli._resolve_error_report_decision(False) is False


def test_consent_is_refused_when_no_endpoint_can_be_derived(isolated_omm_home, capsys):
    config.save_config({"telemetry_endpoint": None, "error_report_send_policy": "always"})

    assert cli._resolve_error_report_decision(True) is False
    assert "endpoint" in capsys.readouterr().err


# --- trigger 1: contribute's catch-and-continue on a quality-eval failure ---


def _stub_install(monkeypatch):
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(
        cli, "download_file", lambda url, dest, stop_check=None, **_kw: dest.write_bytes(b"x")
    )
    monkeypatch.setattr(cli, "sha256_file", lambda dest: "deadbeef")
    monkeypatch.setattr(cli, "remote_file_sha256", lambda *args: None)
    monkeypatch.setattr(cli.linker, "is_ollama_installed", lambda: True)
    monkeypatch.setattr(cli.linker, "is_lmstudio_installed", lambda: False)
    monkeypatch.setattr(cli.linker, "is_jan_installed", lambda: False)
    monkeypatch.setattr(cli.linker, "is_anythingllm_installed", lambda: False)
    monkeypatch.setattr(cli.linker, "is_mstystudio_installed", lambda: False)
    monkeypatch.setattr(cli.linker, "is_textgenwebui_installed", lambda: False)
    monkeypatch.setattr(cli.linker, "is_koboldcpp_installed", lambda: False)
    monkeypatch.setattr(
        cli.linker, "link_ollama", lambda dest, tag, models_dir=None, **kwargs: True
    )
    monkeypatch.setattr(cli.linker, "sanitize_ollama_tag", lambda filename: "tinyllama")
    monkeypatch.setattr(cli.quality_mod, "unload_model", lambda tag, **kwargs: True)
    monkeypatch.setattr(cli.quality_mod, "ensure_model_unloaded", lambda tag: True)
    monkeypatch.setattr(cli.quality_mod, "runtime_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(
        cli.quality_mod,
        "evaluate_model",
        lambda tag, pack, speed_runs=3: (_ for _ in ()).throw(
            cli.quality_mod.QualityEvaluationError("Ollama /api/generate request failed")
        ),
    )


def _run_failing_quality_eval(monkeypatch, *, contribute_mode: bool):
    _stub_install(monkeypatch)
    return cli._install_impl(
        ResolvedModel(
            url="https://example.com/x.gguf",
            filename="tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf",
            repo_id="org/repo",
            provider=None,
        ),
        auto_upload=True,
        use_quality_eval=True,
        quality_pack={"pack_id": "pack-1", "pack_version": "1.1.0", "items": []},
        stop_event=threading.Event(),
        contribute_mode=contribute_mode,
    )


def test_a_failed_quality_evaluation_queues_a_report_during_contribute(
    isolated_omm_home, monkeypatch
):
    _enable_endpoint(error_report_send_policy="always")

    _run_failing_quality_eval(monkeypatch, contribute_mode=True)

    queued = error_report._load_pending()
    assert len(queued) == 1
    assert queued[0]["trigger"] == "install_quality_eval"
    assert queued[0]["error_type"] == "QualityEvaluationError"
    assert "api/generate" in queued[0]["error_message"]
    assert queued[0]["catalog_ref"] == "org/repo:tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf"


def test_a_failed_quality_evaluation_outside_contribute_queues_nothing(
    isolated_omm_home, monkeypatch
):
    _enable_endpoint(error_report_send_policy="always")

    _run_failing_quality_eval(monkeypatch, contribute_mode=False)

    assert error_report.pending_count() == 0


def test_a_failed_quality_evaluation_queues_nothing_when_reports_are_off(
    isolated_omm_home, monkeypatch
):
    _enable_endpoint(error_report_send_policy="never")

    _run_failing_quality_eval(monkeypatch, contribute_mode=True)

    assert error_report.pending_count() == 0


# --- trigger 2: giving up after the daemon could not be restarted ----------


class _FakeQueue:
    def __init__(self, candidates):
        self._candidates = list(candidates)
        self.marked_seen = []

    def next_candidate(self, refetch=None, fetch_siblings=None):
        while self._candidates:
            candidate = self._candidates.pop(0)
            if cli.contribute_mod.ref(candidate) not in self.marked_seen:
                return candidate
        return None

    def mark_seen(self, ref):
        self.marked_seen.append(ref)


def _run_loop_with_unrestartable_daemon(monkeypatch):
    candidate = {
        "repo_id": "org/repo",
        "filename": "model.gguf",
        "name": "model",
        "provider": "huggingface",
    }
    stop_event = threading.Event()
    reachable = [True, False]
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: reachable.pop(0))
    monkeypatch.setattr(cli.benchmark, "start_ollama_daemon", lambda: None)
    monkeypatch.setattr(cli.benchmark, "last_daemon_start_error", lambda: "port 11434 in use")
    monkeypatch.setattr(cli, "_remove_one", lambda filename, entry: None)
    registry.upsert_entry(
        "model.gguf",
        sha256="deadbeef",
        version="deadbee",
        linked={"lmstudio": False, "ollama": True},
    )

    def fake_install_impl(resolved, **kwargs):
        stop_event.set()
        return cli.InstallOutcome(
            filename="model.gguf",
            repo_id="org/repo",
            linked={"lmstudio": False, "ollama": True},
            tokens_per_sec=None,
            telemetry_sent=False,
        )

    monkeypatch.setattr(cli, "_install_impl", fake_install_impl)
    return cli._run_contribution_loop(_FakeQueue([candidate]), stop_event, refetch=None)


def test_giving_up_after_a_failed_daemon_restart_queues_a_report(isolated_omm_home, monkeypatch):
    _enable_endpoint(error_report_send_policy="always")

    _run_loop_with_unrestartable_daemon(monkeypatch)

    queued = error_report._load_pending()
    assert len(queued) == 1
    assert queued[0]["trigger"] == "daemon_restart_giveup"
    assert "port 11434 in use" in queued[0]["error_message"]
    assert queued[0]["catalog_ref"] == "org/repo:model.gguf"


def test_giving_up_after_a_failed_daemon_restart_queues_nothing_when_reports_are_off(
    isolated_omm_home, monkeypatch
):
    _enable_endpoint(error_report_send_policy="never")

    _run_loop_with_unrestartable_daemon(monkeypatch)

    assert error_report.pending_count() == 0


# --- trigger 3: the top-level crash hook, on every command -----------------


def _crash_in(monkeypatch, argv, error):
    monkeypatch.setattr(cli.sys, "argv", argv)

    def _raise():
        raise error

    monkeypatch.setattr(cli, "app", _raise)


def test_the_crash_hook_queues_a_report_for_a_non_contribute_command(
    isolated_omm_home, monkeypatch
):
    _enable_endpoint(error_report_send_policy="always")
    _crash_in(monkeypatch, ["omm", "search", "qwen3"], ValueError("some genuine bug"))

    with pytest.raises(ValueError):
        cli.main()

    queued = error_report._load_pending()
    assert len(queued) == 1
    assert queued[0]["trigger"] == "crash"
    assert queued[0]["subcommand"] == "search"
    assert queued[0]["error_type"] == "ValueError"


def test_the_crash_hook_never_records_the_arguments_of_a_command(isolated_omm_home, monkeypatch):
    _enable_endpoint(error_report_send_policy="always")
    _crash_in(
        monkeypatch,
        ["omm", "search", "a-very-private-query"],
        ValueError("some genuine bug"),
    )

    with pytest.raises(ValueError):
        cli.main()

    assert "a-very-private-query" not in str(error_report._load_pending())


def test_the_crash_hook_reports_no_subcommand_for_an_unrecognized_argument(
    isolated_omm_home, monkeypatch
):
    _enable_endpoint(error_report_send_policy="always")
    _crash_in(monkeypatch, ["omm", "definitely-not-a-command"], ValueError("bug"))

    with pytest.raises(ValueError):
        cli.main()

    assert "subcommand" not in error_report._load_pending()[0]


def test_the_crash_hook_queues_a_non_enospc_oserror_and_still_reraises_it(
    isolated_omm_home, monkeypatch
):
    _enable_endpoint(error_report_send_policy="always")
    _crash_in(monkeypatch, ["omm", "list"], OSError(errno.ECONNREFUSED, "Connection refused"))

    with pytest.raises(OSError):
        cli.main()

    assert error_report._load_pending()[0]["error_type"] == "ConnectionRefusedError"


def test_the_crash_hook_does_not_queue_a_permission_error(isolated_omm_home, monkeypatch):
    # PermissionError (issue #191) gets a clean cause+fix message and a
    # SystemExit instead of the generic crash-report treatment - it's a
    # user-actionable condition, not a bug to report.
    _enable_endpoint(error_report_send_policy="always")
    _crash_in(monkeypatch, ["omm", "list"], OSError(errno.EACCES, "Permission denied"))

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    assert error_report._load_pending() == []


def test_a_normal_command_exit_is_not_reported_as_a_crash(isolated_omm_home, monkeypatch):
    _enable_endpoint(error_report_send_policy="always")
    _crash_in(monkeypatch, ["omm", "search", "qwen3"], SystemExit(1))

    with pytest.raises(SystemExit):
        cli.main()

    assert error_report.pending_count() == 0


def test_a_disk_space_error_keeps_its_friendly_message_instead_of_a_report(
    isolated_omm_home, monkeypatch
):
    _enable_endpoint(error_report_send_policy="always")
    _crash_in(
        monkeypatch,
        ["omm", "install", "model"],
        cli.InsufficientDiskSpaceError("model.gguf needs 5.0GB but only 1.0GB free"),
    )

    with pytest.raises(SystemExit):
        cli.main()

    assert error_report.pending_count() == 0


def test_the_crash_hook_queues_nothing_when_reports_are_off(isolated_omm_home, monkeypatch):
    _enable_endpoint(error_report_send_policy="never")
    _crash_in(monkeypatch, ["omm", "search", "qwen3"], ValueError("some genuine bug"))

    with pytest.raises(ValueError):
        cli.main()

    assert error_report.pending_count() == 0


def test_crash_hardware_scores_come_from_a_scan_the_command_already_did(
    isolated_omm_home, monkeypatch
):
    """Reports never start a hardware scan of their own - a scan takes
    seconds, and nobody should wait for one after a crash."""
    _enable_endpoint(error_report_send_policy="always")
    monkeypatch.setattr(
        hardware,
        "_scan_hardware",
        lambda: (_ for _ in ()).throw(AssertionError("no scan may be started for a report")),
    )
    monkeypatch.setattr(hardware, "_last_scan", None, raising=False)
    _crash_in(monkeypatch, ["omm", "search", "qwen3"], ValueError("bug"))

    with pytest.raises(ValueError):
        cli.main()

    assert "cpu_score" not in error_report._load_pending()[0]


def test_crash_reports_reuse_the_scan_a_command_already_performed(isolated_omm_home, monkeypatch):
    _enable_endpoint(error_report_send_policy="always")
    monkeypatch.setattr(
        hardware,
        "_last_scan",
        SimpleNamespace(cpu="AMD Ryzen 5 5600X", gpu_name="NVIDIA RTX 4090", cpu_arch="x86_64"),
        raising=False,
    )
    _crash_in(monkeypatch, ["omm", "search", "qwen3"], ValueError("bug"))

    with pytest.raises(ValueError):
        cli.main()

    report = error_report._load_pending()[0]
    assert report["cpu_score"] == 5600
    assert report["gpu_score"] == 4090
    assert "Ryzen" not in str(report)
