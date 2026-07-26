import requests
from typer.testing import CliRunner

from omm import cli, config

runner = CliRunner()


class _FakeListener:
    """Stands in for _EscListener: sets stop_event immediately so the real
    loop body (already covered by test_contribute_loop.py) runs at most
    zero/one iterations in these command-level tests."""

    def __init__(self):
        self.stop_event = cli.threading.Event()

    def start(self):
        self.stop_event.set()


def test_declining_consent_cancels_before_any_other_check(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: False)
    monkeypatch.setattr(
        cli.benchmark,
        "ollama_daemon_reachable",
        lambda: (_ for _ in ()).throw(AssertionError("should not check daemon before consent")),
    )

    result = runner.invoke(cli.app, ["contribute"])

    assert result.exit_code == 0, result.stdout
    assert "Cancelled" in result.stderr


def test_requires_ollama_daemon(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: True)
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: False)

    result = runner.invoke(cli.app, ["contribute"])

    assert result.exit_code == 1
    assert "Ollama daemon" in result.stderr


def test_declines_starting_ollama_daemon_when_prompted(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: False)
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)

    def fake_confirm(message, **k):
        return "Ollama isn't running" not in message

    monkeypatch.setattr(cli, "_ask_confirm", fake_confirm)
    monkeypatch.setattr(
        cli.benchmark,
        "start_ollama_daemon",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not start")),
    )

    result = runner.invoke(cli.app, ["contribute"])

    assert result.exit_code == 1
    assert "requires a running Ollama daemon" in result.stderr


def test_starts_and_stops_ollama_daemon_when_confirmed(isolated_omm_home, monkeypatch):
    config.update_config(telemetry_endpoint="https://example.com/telemetry.json")
    reachable = {"value": False}
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: reachable["value"])
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: True)
    monkeypatch.setattr(
        cli.predictor,
        "load_model_with_change_note",
        lambda url: ({"trees": [{}], "candidates": [{"repo_id": "o", "filename": "m.gguf"}]}, False),
    )
    monkeypatch.setattr(cli, "scan_hardware", lambda: object())
    monkeypatch.setattr(cli.predictor, "rank_candidates", lambda artifact, hw: [])
    monkeypatch.setattr(cli.benchmark_history, "loaded_refs", lambda: set())
    monkeypatch.setattr(cli, "_EscListener", _FakeListener)
    monkeypatch.setattr(cli, "_telemetry_row_count", lambda endpoint: 100)
    monkeypatch.setattr(cli, "_run_contribution_loop", lambda *a, **k: cli._ContributionStats(benchmarked=[]))
    monkeypatch.setattr(cli, "autoremove", lambda: None)

    started = object()
    stopped = []

    def _start(*a, **k):
        reachable["value"] = True
        return started

    monkeypatch.setattr(cli.benchmark, "start_ollama_daemon", _start)
    monkeypatch.setattr(cli.benchmark, "stop_ollama_daemon", lambda proc: stopped.append(proc))

    result = runner.invoke(cli.app, ["contribute"])

    assert result.exit_code == 0, result.stdout
    assert stopped == [started]


def test_yes_flag_auto_starts_ollama_daemon_without_prompting(isolated_omm_home, monkeypatch):
    config.update_config(telemetry_endpoint="https://example.com/telemetry.json")
    reachable = {"value": False}
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: reachable["value"])
    monkeypatch.setattr(
        cli, "_ask_confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not prompt"))
    )
    monkeypatch.setattr(
        cli.predictor,
        "load_model_with_change_note",
        lambda url: ({"trees": [{}], "candidates": [{"repo_id": "o", "filename": "m.gguf"}]}, False),
    )
    monkeypatch.setattr(cli, "scan_hardware", lambda: object())
    monkeypatch.setattr(cli.predictor, "rank_candidates", lambda artifact, hw: [])
    monkeypatch.setattr(cli.benchmark_history, "loaded_refs", lambda: set())
    monkeypatch.setattr(cli, "_EscListener", _FakeListener)
    monkeypatch.setattr(cli, "_telemetry_row_count", lambda endpoint: 100)
    monkeypatch.setattr(cli, "_run_contribution_loop", lambda *a, **k: cli._ContributionStats(benchmarked=[]))
    monkeypatch.setattr(cli, "autoremove", lambda: None)

    started = object()
    stopped = []

    def _start(*a, **k):
        reachable["value"] = True
        return started

    monkeypatch.setattr(cli.benchmark, "start_ollama_daemon", _start)
    monkeypatch.setattr(cli.benchmark, "stop_ollama_daemon", lambda proc: stopped.append(proc))

    result = runner.invoke(cli.app, ["contribute", "--yes"])

    assert result.exit_code == 0, result.stdout
    assert stopped == [started]


def test_requires_trained_recommendation_model(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: True)
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(cli.predictor, "load_model_with_change_note", lambda url: (None, False))

    result = runner.invoke(cli.app, ["contribute"])

    assert result.exit_code == 1
    assert "No trained recommendation model" in result.stderr


def test_happy_path_runs_loop_cleans_up_and_prints_summary(isolated_omm_home, monkeypatch):
    config.update_config(telemetry_endpoint="https://example.com/telemetry.json")
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: True)
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(
        cli.predictor,
        "load_model_with_change_note",
        lambda url: ({"trees": [{}], "candidates": [{"repo_id": "o", "filename": "m.gguf"}]}, False),
    )
    monkeypatch.setattr(cli, "scan_hardware", lambda: object())
    monkeypatch.setattr(cli.predictor, "rank_candidates", lambda artifact, hw: [])
    monkeypatch.setattr(cli.benchmark_history, "loaded_refs", lambda: set())
    monkeypatch.setattr(cli, "_EscListener", _FakeListener)
    monkeypatch.setattr(cli, "_telemetry_row_count", lambda endpoint: 100)

    loop_calls = []

    def fake_loop(queue, stop_event, refetch, quality_pack=None):
        loop_calls.append(1)
        return cli._ContributionStats(benchmarked=[("m", 12.5)], skipped_unfit=1, attempted_not_uploaded=0)

    monkeypatch.setattr(cli, "_run_contribution_loop", fake_loop)
    autoremove_calls = []
    monkeypatch.setattr(cli, "autoremove", lambda: autoremove_calls.append(1))

    result = runner.invoke(cli.app, ["contribute"])

    assert result.exit_code == 0, result.stdout
    assert loop_calls == [1]
    assert autoremove_calls == [1]
    assert "session summary" in result.stdout.lower()
    assert "m" in result.stdout and "12.5" in result.stdout
    assert "100 -> 100" in result.stdout


def test_contribute_yes_flag_skips_prompt_without_a_tty(isolated_omm_home, monkeypatch):
    config.update_config(telemetry_endpoint="https://example.com/telemetry.json")
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(
        cli.predictor,
        "load_model_with_change_note",
        lambda url: ({"trees": [{}], "candidates": [{"repo_id": "o", "filename": "m.gguf"}]}, False),
    )
    monkeypatch.setattr(cli, "scan_hardware", lambda: object())
    monkeypatch.setattr(cli.predictor, "rank_candidates", lambda artifact, hw: [])
    monkeypatch.setattr(cli.benchmark_history, "loaded_refs", lambda: set())
    monkeypatch.setattr(cli, "_EscListener", _FakeListener)
    monkeypatch.setattr(cli, "_telemetry_row_count", lambda endpoint: 100)
    monkeypatch.setattr(cli, "_run_contribution_loop", lambda *a, **k: cli._ContributionStats(benchmarked=[]))
    monkeypatch.setattr(cli, "autoremove", lambda: None)

    result = runner.invoke(cli.app, ["contribute", "--yes"])

    assert result.exit_code == 0, result.stdout


def test_contribute_without_yes_errors_without_a_tty(isolated_omm_home):
    result = runner.invoke(cli.app, ["contribute"])

    assert result.exit_code == 1


def test_telemetry_row_count_returns_none_on_network_error(monkeypatch):
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: (_ for _ in ()).throw(requests.RequestException("boom")),
    )

    assert cli._telemetry_row_count("https://example.com/telemetry.json") is None


def test_telemetry_row_count_counts_dict_entries(monkeypatch):
    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"a": {}, "b": {}, "c": {}}

    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResp())

    assert cli._telemetry_row_count("https://example.com/telemetry.json") == 3


def test_contribute_loads_quality_pack_and_passes_it_to_loop(isolated_omm_home, monkeypatch):
    config.update_config(telemetry_endpoint="https://example.com/telemetry.json")
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: True)
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(
        cli.predictor,
        "load_model_with_change_note",
        lambda url: ({"trees": [{}], "candidates": [{"repo_id": "o", "filename": "m.gguf"}]}, False),
    )
    monkeypatch.setattr(cli, "scan_hardware", lambda: object())
    monkeypatch.setattr(cli.predictor, "rank_candidates", lambda artifact, hw: [])
    monkeypatch.setattr(cli.benchmark_history, "loaded_refs", lambda: set())
    monkeypatch.setattr(cli, "_EscListener", _FakeListener)
    monkeypatch.setattr(cli, "_telemetry_row_count", lambda endpoint: 0)
    monkeypatch.setattr(cli, "autoremove", lambda: None)
    fake_pack = {"pack_id": "pack-1", "pack_version": "1.1.0", "items": []}
    monkeypatch.setattr(cli.quality_mod, "load_pack", lambda: (fake_pack, "sha"))

    captured = {}

    def fake_loop(queue, stop_event, refetch, quality_pack=None):
        captured["quality_pack"] = quality_pack
        return cli._ContributionStats(benchmarked=[])

    monkeypatch.setattr(cli, "_run_contribution_loop", fake_loop)

    result = runner.invoke(cli.app, ["contribute"])

    assert result.exit_code == 0, result.stdout
    assert captured["quality_pack"] == fake_pack


def test_contribute_refuses_when_policy_never(isolated_omm_home, monkeypatch):
    config.update_config(telemetry_send_policy="never")
    monkeypatch.setattr(
        cli, "_ask_confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no prompt"))
    )

    result = runner.invoke(cli.app, ["contribute"])

    assert result.exit_code == 1
    assert "requires benchmark uploads" in result.stderr


def test_contribute_warns_once_when_policy_always(isolated_omm_home, monkeypatch):
    config.update_config(telemetry_send_policy="always")
    confirms = []

    def fake_confirm(message, **k):
        confirms.append(message)
        return True

    monkeypatch.setattr(cli, "_ask_confirm", fake_confirm)
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(
        cli.predictor,
        "load_model_with_change_note",
        lambda url: ({"trees": [{}], "candidates": [{"repo_id": "o", "filename": "m.gguf"}]}, False),
    )
    monkeypatch.setattr(cli, "scan_hardware", lambda: object())
    monkeypatch.setattr(cli.predictor, "rank_candidates", lambda artifact, hw: [])
    monkeypatch.setattr(cli.benchmark_history, "loaded_refs", lambda: set())
    monkeypatch.setattr(cli, "_EscListener", _FakeListener)
    monkeypatch.setattr(cli, "_telemetry_row_count", lambda endpoint: None)
    monkeypatch.setattr(cli, "autoremove", lambda: None)
    monkeypatch.setattr(cli.quality_mod, "load_pack", lambda: ({"pack_id": "p", "items": []}, "sha"))
    monkeypatch.setattr(cli, "_run_contribution_loop", lambda *a, **k: cli._ContributionStats(benchmarked=[]))

    result = runner.invoke(cli.app, ["contribute"])

    assert result.exit_code == 0, result.stdout
    assert "without asking each time" in result.stderr
    assert config.load_config()["contribute_always_ack"] is True


def test_contribute_skips_always_warning_once_acknowledged(isolated_omm_home, monkeypatch):
    config.update_config(telemetry_send_policy="always", contribute_always_ack=True)
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(
        cli.predictor,
        "load_model_with_change_note",
        lambda url: ({"trees": [{}], "candidates": [{"repo_id": "o", "filename": "m.gguf"}]}, False),
    )
    monkeypatch.setattr(cli, "scan_hardware", lambda: object())
    monkeypatch.setattr(cli.predictor, "rank_candidates", lambda artifact, hw: [])
    monkeypatch.setattr(cli.benchmark_history, "loaded_refs", lambda: set())
    monkeypatch.setattr(cli, "_EscListener", _FakeListener)
    monkeypatch.setattr(cli, "_telemetry_row_count", lambda endpoint: None)
    monkeypatch.setattr(cli, "autoremove", lambda: None)
    monkeypatch.setattr(cli.quality_mod, "load_pack", lambda: ({"pack_id": "p", "items": []}, "sha"))
    monkeypatch.setattr(cli, "_run_contribution_loop", lambda *a, **k: cli._ContributionStats(benchmarked=[]))
    confirms = []
    monkeypatch.setattr(cli, "_ask_confirm", lambda message, **k: confirms.append(message) or True)

    result = runner.invoke(cli.app, ["contribute"])

    assert result.exit_code == 0, result.stdout
    assert "without asking each time" not in result.stdout
    assert confirms == ["Start contributing compute now?"]
