import json
from datetime import datetime, timedelta, timezone

import requests
import pytest
from types import SimpleNamespace
from typer.testing import CliRunner

from omm import cli, config

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolate_contribute_disk_preflight(request, monkeypatch):
    """Command tests must not depend on free space on the developer's C:.

    The two disk-specific tests below keep the real preflight function and
    inject exact disk usage. Every other test exercises a later preflight or
    command behavior independently.
    """
    disk_tests = {
        "test_contribute_refuses_to_start_when_model_volume_has_less_than_ten_gib",
        "test_contribute_yes_flag_before_subcommand_skips_low_disk_prompt",
    }
    if request.node.name not in disk_tests:
        monkeypatch.setattr(cli, "_ensure_contribute_start_space", lambda: None)


class _FakeListener:
    """Stands in for _EscListener: sets stop_event immediately so the real
    loop body (already covered by test_contribute_loop.py) runs at most
    zero/one iterations in these command-level tests."""

    def __init__(self):
        self.stop_event = cli.threading.Event()

    def start(self):
        self.stop_event.set()


def test_contribute_refuses_to_start_when_model_volume_has_less_than_ten_gib(
    isolated_omm_home, monkeypatch
):
    monkeypatch.setattr(cli.linker, "storage_volume_key", lambda path: ("test", "volume"))
    monkeypatch.setattr(
        cli.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(free=9 * 1024**3),
    )
    monkeypatch.setattr(
        cli,
        "_ensure_ollama_running",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must stop before engine start")),
    )

    result = runner.invoke(cli.app, ["contribute", "--yes"])

    assert result.exit_code == 1
    assert "will not start with low disk space" in result.stderr


def test_contribute_yes_flag_before_subcommand_skips_low_disk_prompt(
    isolated_omm_home, monkeypatch
):
    monkeypatch.setattr(cli.linker, "storage_volume_key", lambda path: ("test", "volume"))
    monkeypatch.setattr(
        cli.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(free=9 * 1024**3),
    )
    monkeypatch.setattr(
        cli,
        "_ensure_ollama_running",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must stop before engine start")),
    )

    result = runner.invoke(cli.app, ["--yes", "contribute"])

    assert result.exit_code == 1
    assert "will not start with low disk space" in result.stderr


def test_contribute_never_runs_unrelated_auto_import(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(
        cli,
        "_run_import_flow",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("contribute must not auto-import existing models")
        ),
    )
    monkeypatch.setattr(
        cli,
        "_ensure_contribute_start_space",
        lambda: (_ for _ in ()).throw(cli.typer.Exit(1)),
    )

    result = runner.invoke(cli.app, ["contribute", "--yes"])

    assert result.exit_code == 1


def test_engine_preflight_happens_before_expensive_work_consent(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(
        cli, "_ask_confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no consent prompt"))
    )
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: False)
    monkeypatch.setattr(cli.benchmark, "find_ollama_executable", lambda: None)
    # omm contribute now falls back to LM Studio before giving up entirely
    # (_select_benchmark_engine), so its absence needs to be explicit here
    # too or this test would depend on whether LM Studio happens to be
    # installed on the machine running the test suite.
    monkeypatch.setattr(cli.linker, "_lms_cli_path", lambda: None)
    monkeypatch.setattr(cli.linker, "lmstudio_daemon_reachable", lambda: False)

    result = runner.invoke(cli.app, ["contribute"])

    assert result.exit_code == 1, result.stdout
    assert "Neither Ollama nor LM Studio" in result.stderr
    assert "→ Install one of them, start it once, then retry `omm contribute`." in result.stderr


def test_memory_preflight_happens_before_expensive_work_consent(
    isolated_omm_home, monkeypatch
):
    monkeypatch.setattr(
        cli,
        "_ask_confirm",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("no expensive-work consent prompt")
        ),
    )
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(cli.quality_mod, "load_pack", lambda: ({"pack_id": "test"}, False))
    monkeypatch.setattr(
        cli.predictor,
        "load_model_with_change_note",
        lambda *args, **kwargs: (
            {
                "trees": [{}],
                "candidates": [
                    {"repo_id": "org/repo", "filename": "model.gguf", "size_bytes": 1}
                ],
            },
            False,
        ),
    )
    monkeypatch.setattr(cli, "scan_hardware", lambda: object())
    monkeypatch.setattr(cli.predictor, "rank_candidates", lambda *args: [])
    monkeypatch.setattr(cli.benchmark_history, "loaded_refs", lambda: set())
    preflight_calls = []

    def fail_preflight(*args, **kwargs):
        preflight_calls.append(1)
        raise cli.typer.Exit(1)

    monkeypatch.setattr(cli, "_ensure_contribute_candidate_memory", fail_preflight)

    result = runner.invoke(cli.app, ["contribute"])

    assert result.exit_code == 1
    assert preflight_calls == [1]


def test_requires_ollama_daemon(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: True)
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: False)
    monkeypatch.setattr(cli.benchmark, "find_ollama_executable", lambda: None)
    monkeypatch.setattr(cli.linker, "_lms_cli_path", lambda: None)
    monkeypatch.setattr(cli.linker, "lmstudio_daemon_reachable", lambda: False)

    result = runner.invoke(cli.app, ["contribute"])

    assert result.exit_code == 1
    assert "Neither Ollama nor LM Studio" in result.stderr


def test_declines_starting_ollama_daemon_when_prompted(isolated_omm_home, monkeypatch):
    # An existing install, not a fresh one: _root's onboarding gate now
    # covers every subcommand, and on a truly fresh isolated_omm_home the
    # monkeypatched _stdin_is_tty below would also let the real setup
    # wizard fire here first, crashing in its own (unmocked) engine
    # checklist before this test's own scenario ever runs.
    config.update_config(onboarding_completed=True)
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: False)
    monkeypatch.setattr(cli.benchmark, "find_ollama_executable", lambda: cli.Path("ollama.exe"))
    monkeypatch.setattr(cli.linker, "_lms_cli_path", lambda: None)
    monkeypatch.setattr(cli.linker, "lmstudio_daemon_reachable", lambda: False)
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)

    def fake_confirm(message, **k):
        return "installed but stopped" not in message

    monkeypatch.setattr(cli, "_ask_confirm", fake_confirm)
    monkeypatch.setattr(
        cli.benchmark,
        "start_ollama_daemon",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not start")),
    )

    result = runner.invoke(cli.app, ["contribute"])

    assert result.exit_code == 1
    assert "requires the Ollama API" in result.stderr


def test_starts_and_stops_ollama_daemon_when_confirmed(isolated_omm_home, monkeypatch):
    config.update_config(telemetry_endpoint="https://example.com/telemetry.json")
    reachable = {"value": False}
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: reachable["value"])
    monkeypatch.setattr(cli.benchmark, "find_ollama_executable", lambda: cli.Path("ollama.exe"))
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: True)
    monkeypatch.setattr(
        cli.predictor,
        "load_model_with_change_note",
        lambda url, *a, **k: ({"trees": [{}], "candidates": [{"repo_id": "o", "filename": "m.gguf"}]}, False),
    )
    monkeypatch.setattr(cli, "scan_hardware", lambda: object())
    monkeypatch.setattr(cli.predictor, "rank_candidates", lambda artifact, hw: [])
    monkeypatch.setattr(cli.benchmark_history, "loaded_refs", lambda: set())
    monkeypatch.setattr(cli, "_EscListener", _FakeListener)
    monkeypatch.setattr(cli, "_telemetry_row_count", lambda endpoint: 100)
    monkeypatch.setattr(cli, "_run_contribution_loop", lambda *a, **k: cli._ContributionStats(benchmarked=[]))
    monkeypatch.setattr(cli, "autoremove", lambda: None)
    monkeypatch.setattr(cli, "cleanup", lambda: None)

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
    monkeypatch.setattr(cli.benchmark, "find_ollama_executable", lambda: cli.Path("ollama.exe"))
    monkeypatch.setattr(
        cli, "_ask_confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not prompt"))
    )
    monkeypatch.setattr(
        cli.predictor,
        "load_model_with_change_note",
        lambda url, *a, **k: ({"trees": [{}], "candidates": [{"repo_id": "o", "filename": "m.gguf"}]}, False),
    )
    monkeypatch.setattr(cli, "scan_hardware", lambda: object())
    monkeypatch.setattr(cli.predictor, "rank_candidates", lambda artifact, hw: [])
    monkeypatch.setattr(cli.benchmark_history, "loaded_refs", lambda: set())
    monkeypatch.setattr(cli, "_EscListener", _FakeListener)
    monkeypatch.setattr(cli, "_telemetry_row_count", lambda endpoint: 100)
    monkeypatch.setattr(cli, "_run_contribution_loop", lambda *a, **k: cli._ContributionStats(benchmarked=[]))
    monkeypatch.setattr(cli, "autoremove", lambda: None)
    monkeypatch.setattr(cli, "cleanup", lambda: None)

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
    monkeypatch.setattr(cli.predictor, "load_model_with_change_note", lambda url, *a, **k: (None, False))

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
        lambda url, *a, **k: ({"trees": [{}], "candidates": [{"repo_id": "o", "filename": "m.gguf"}]}, False),
    )
    monkeypatch.setattr(cli, "scan_hardware", lambda: object())
    monkeypatch.setattr(cli.predictor, "rank_candidates", lambda artifact, hw: [])
    monkeypatch.setattr(cli.benchmark_history, "loaded_refs", lambda: set())
    monkeypatch.setattr(cli, "_EscListener", _FakeListener)
    monkeypatch.setattr(cli, "_telemetry_row_count", lambda endpoint: 100)

    loop_calls = []

    def fake_loop(queue, stop_event, refetch, quality_pack=None, daemon_ref=None, fetch_siblings=None, engine="ollama"):
        loop_calls.append(1)
        return cli._ContributionStats(benchmarked=[("m", 12.5)], skipped_unfit=1, attempted_not_uploaded=0)

    monkeypatch.setattr(cli, "_run_contribution_loop", fake_loop)
    autoremove_calls = []
    cleanup_calls = []
    monkeypatch.setattr(cli, "autoremove", lambda: autoremove_calls.append(1))
    monkeypatch.setattr(cli, "cleanup", lambda: cleanup_calls.append(1))

    result = runner.invoke(cli.app, ["contribute"])

    assert result.exit_code == 0, result.stdout
    assert loop_calls == [1]
    assert autoremove_calls == [1]
    assert cleanup_calls == [1]
    assert "session summary" in result.stdout.lower()
    assert "m" in result.stdout and "12.5" in result.stdout
    assert "100 -> 100" in result.stdout


def test_exhausted_session_prints_thank_you_banner_with_coverage(isolated_omm_home, monkeypatch):
    config.update_config(telemetry_endpoint="https://example.com/telemetry.json")
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: True)
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(
        cli.predictor,
        "load_model_with_change_note",
        lambda url, *a, **k: (
            {
                "trees": [{}],
                "candidates": [
                    {"repo_id": "o", "filename": "a.gguf"},
                    {"repo_id": "o", "filename": "b.gguf"},
                    {"repo_id": "o", "filename": "c.gguf"},
                ],
            },
            False,
        ),
    )
    monkeypatch.setattr(cli, "scan_hardware", lambda: object())
    monkeypatch.setattr(cli.predictor, "rank_candidates", lambda artifact, hw: [])
    # 2 of 3 candidates already covered by the time the (faked) loop returns:
    # loaded_refs seeds the queue's history_refs before the loop runs, and
    # the faked loop below doesn't touch it further, so this is exactly what
    # queue.history_refs will contain when the summary is printed.
    monkeypatch.setattr(
        cli.benchmark_history, "loaded_refs", lambda: {"huggingface:o:a.gguf", "huggingface:o:b.gguf"}
    )
    monkeypatch.setattr(cli, "_EscListener", _FakeListener)
    monkeypatch.setattr(cli, "_telemetry_row_count", lambda endpoint: 100)

    def fake_loop(queue, stop_event, refetch, quality_pack=None, daemon_ref=None, fetch_siblings=None, engine="ollama"):
        return cli._ContributionStats(benchmarked=[], skipped_unfit=1, exhausted=True)

    monkeypatch.setattr(cli, "_run_contribution_loop", fake_loop)
    monkeypatch.setattr(cli, "autoremove", lambda: None)
    monkeypatch.setattr(cli, "cleanup", lambda: None)

    result = runner.invoke(cli.app, ["contribute"])

    assert result.exit_code == 0, result.stdout
    assert "Thank you for contributing" in result.stdout
    assert "2/3 candidates covered" in result.stdout
    state = cli.contribute_state.load()
    assert state["total_candidates"] == 3
    assert state["covered_candidates"] == 2


def test_no_heads_up_warning_on_first_ever_session(isolated_omm_home, monkeypatch):
    config.update_config(telemetry_endpoint="https://example.com/telemetry.json")
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: True)
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(
        cli.predictor,
        "load_model_with_change_note",
        lambda url, *a, **k: ({"trees": [{}], "candidates": [{"repo_id": "o", "filename": "a.gguf"}]}, False),
    )
    monkeypatch.setattr(cli, "scan_hardware", lambda: object())
    monkeypatch.setattr(cli.predictor, "rank_candidates", lambda artifact, hw: [])
    monkeypatch.setattr(cli.benchmark_history, "loaded_refs", lambda: set())
    monkeypatch.setattr(cli, "_EscListener", _FakeListener)
    monkeypatch.setattr(cli, "_telemetry_row_count", lambda endpoint: 100)
    monkeypatch.setattr(
        cli, "_run_contribution_loop",
        lambda *a, **k: cli._ContributionStats(benchmarked=[]),
    )
    monkeypatch.setattr(cli, "autoremove", lambda: None)
    monkeypatch.setattr(cli, "cleanup", lambda: None)

    result = runner.invoke(cli.app, ["contribute"])

    assert result.exit_code == 0, result.stdout
    assert "Heads up" not in result.stderr


def test_heads_up_warning_when_prior_session_already_covered_same_catalog(
    isolated_omm_home, monkeypatch
):
    cli.contribute_state.record_exhausted(total_candidates=1, covered_candidates=1)
    config.update_config(telemetry_endpoint="https://example.com/telemetry.json")
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: True)
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(
        cli.predictor,
        "load_model_with_change_note",
        lambda url, *a, **k: ({"trees": [{}], "candidates": [{"repo_id": "o", "filename": "a.gguf"}]}, False),
    )
    monkeypatch.setattr(cli, "scan_hardware", lambda: object())
    monkeypatch.setattr(cli.predictor, "rank_candidates", lambda artifact, hw: [])
    monkeypatch.setattr(cli.benchmark_history, "loaded_refs", lambda: {"huggingface:o:a.gguf"})
    monkeypatch.setattr(cli, "_EscListener", _FakeListener)
    monkeypatch.setattr(cli, "_telemetry_row_count", lambda endpoint: 100)
    monkeypatch.setattr(
        cli, "_run_contribution_loop",
        lambda *a, **k: cli._ContributionStats(benchmarked=[], exhausted=True),
    )
    monkeypatch.setattr(cli, "autoremove", lambda: None)
    monkeypatch.setattr(cli, "cleanup", lambda: None)

    result = runner.invoke(cli.app, ["contribute"])

    assert result.exit_code == 0, result.stdout
    assert "Heads up" in result.stderr
    assert "1/1" in result.stderr


def test_no_heads_up_warning_when_catalog_grew_since_last_exhaustion(
    isolated_omm_home, monkeypatch
):
    cli.contribute_state.record_exhausted(total_candidates=1, covered_candidates=1)
    config.update_config(telemetry_endpoint="https://example.com/telemetry.json")
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: True)
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(
        cli.predictor,
        "load_model_with_change_note",
        lambda url, *a, **k: (
            {
                "trees": [{}],
                "candidates": [
                    {"repo_id": "o", "filename": "a.gguf"},
                    {"repo_id": "o", "filename": "b.gguf"},
                ],
            },
            False,
        ),
    )
    monkeypatch.setattr(cli, "scan_hardware", lambda: object())
    monkeypatch.setattr(cli.predictor, "rank_candidates", lambda artifact, hw: [])
    monkeypatch.setattr(cli.benchmark_history, "loaded_refs", lambda: {"huggingface:o:a.gguf"})
    monkeypatch.setattr(cli, "_EscListener", _FakeListener)
    monkeypatch.setattr(cli, "_telemetry_row_count", lambda endpoint: 100)
    monkeypatch.setattr(
        cli, "_run_contribution_loop",
        lambda *a, **k: cli._ContributionStats(benchmarked=[]),
    )
    monkeypatch.setattr(cli, "autoremove", lambda: None)
    monkeypatch.setattr(cli, "cleanup", lambda: None)

    result = runner.invoke(cli.app, ["contribute"])

    assert result.exit_code == 0, result.stdout
    assert "Heads up" not in result.stderr


def test_contribute_yes_flag_skips_prompt_without_a_tty(isolated_omm_home, monkeypatch):
    config.update_config(telemetry_endpoint="https://example.com/telemetry.json")
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(
        cli.predictor,
        "load_model_with_change_note",
        lambda url, *a, **k: ({"trees": [{}], "candidates": [{"repo_id": "o", "filename": "m.gguf"}]}, False),
    )
    monkeypatch.setattr(cli, "scan_hardware", lambda: object())
    monkeypatch.setattr(cli.predictor, "rank_candidates", lambda artifact, hw: [])
    monkeypatch.setattr(cli.benchmark_history, "loaded_refs", lambda: set())
    monkeypatch.setattr(cli, "_EscListener", _FakeListener)
    monkeypatch.setattr(cli, "_telemetry_row_count", lambda endpoint: 100)
    monkeypatch.setattr(cli, "_run_contribution_loop", lambda *a, **k: cli._ContributionStats(benchmarked=[]))
    monkeypatch.setattr(cli, "autoremove", lambda: None)
    monkeypatch.setattr(cli, "cleanup", lambda: None)

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
        lambda url, *a, **k: ({"trees": [{}], "candidates": [{"repo_id": "o", "filename": "m.gguf"}]}, False),
    )
    monkeypatch.setattr(cli, "scan_hardware", lambda: object())
    monkeypatch.setattr(cli.predictor, "rank_candidates", lambda artifact, hw: [])
    monkeypatch.setattr(cli.benchmark_history, "loaded_refs", lambda: set())
    monkeypatch.setattr(cli, "_EscListener", _FakeListener)
    monkeypatch.setattr(cli, "_telemetry_row_count", lambda endpoint: 0)
    monkeypatch.setattr(cli, "autoremove", lambda: None)
    monkeypatch.setattr(cli, "cleanup", lambda: None)
    fake_pack = {"pack_id": "pack-1", "pack_version": "1.1.0", "items": []}
    monkeypatch.setattr(cli.quality_mod, "load_pack", lambda: (fake_pack, "sha"))

    captured = {}

    def fake_loop(queue, stop_event, refetch, quality_pack=None, daemon_ref=None, fetch_siblings=None, engine="ollama"):
        captured["quality_pack"] = quality_pack
        return cli._ContributionStats(benchmarked=[])

    monkeypatch.setattr(cli, "_run_contribution_loop", fake_loop)

    result = runner.invoke(cli.app, ["contribute"])

    assert result.exit_code == 0, result.stdout
    assert captured["quality_pack"] == fake_pack


def test_contribute_passes_fetch_sibling_candidates_to_loop(isolated_omm_home, monkeypatch):
    config.update_config(telemetry_endpoint="https://example.com/telemetry.json")
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: True)
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(
        cli.predictor,
        "load_model_with_change_note",
        lambda url, *a, **k: ({"trees": [{}], "candidates": [{"repo_id": "o", "filename": "m.gguf"}]}, False),
    )
    monkeypatch.setattr(cli, "scan_hardware", lambda: object())
    monkeypatch.setattr(cli.predictor, "rank_candidates", lambda artifact, hw: [])
    monkeypatch.setattr(cli.benchmark_history, "loaded_refs", lambda: set())
    monkeypatch.setattr(cli, "_EscListener", _FakeListener)
    monkeypatch.setattr(cli, "_telemetry_row_count", lambda endpoint: 0)
    monkeypatch.setattr(cli, "autoremove", lambda: None)
    monkeypatch.setattr(cli, "cleanup", lambda: None)
    fake_pack = {"pack_id": "pack-1", "pack_version": "1.1.0", "items": []}
    monkeypatch.setattr(cli.quality_mod, "load_pack", lambda: (fake_pack, "sha"))

    captured = {}

    def fake_loop(queue, stop_event, refetch, quality_pack=None, daemon_ref=None, fetch_siblings=None, engine="ollama"):
        captured["fetch_siblings"] = fetch_siblings
        return cli._ContributionStats(benchmarked=[])

    monkeypatch.setattr(cli, "_run_contribution_loop", fake_loop)

    result = runner.invoke(cli.app, ["contribute"])

    assert result.exit_code == 0, result.stdout
    assert captured["fetch_siblings"] is cli._fetch_sibling_candidates


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
        lambda url, *a, **k: ({"trees": [{}], "candidates": [{"repo_id": "o", "filename": "m.gguf"}]}, False),
    )
    monkeypatch.setattr(cli, "scan_hardware", lambda: object())
    monkeypatch.setattr(cli.predictor, "rank_candidates", lambda artifact, hw: [])
    monkeypatch.setattr(cli.benchmark_history, "loaded_refs", lambda: set())
    monkeypatch.setattr(cli, "_EscListener", _FakeListener)
    monkeypatch.setattr(cli, "_telemetry_row_count", lambda endpoint: None)
    monkeypatch.setattr(cli, "autoremove", lambda: None)
    monkeypatch.setattr(cli, "cleanup", lambda: None)
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
        lambda url, *a, **k: ({"trees": [{}], "candidates": [{"repo_id": "o", "filename": "m.gguf"}]}, False),
    )
    monkeypatch.setattr(cli, "scan_hardware", lambda: object())
    monkeypatch.setattr(cli.predictor, "rank_candidates", lambda artifact, hw: [])
    monkeypatch.setattr(cli.benchmark_history, "loaded_refs", lambda: set())
    monkeypatch.setattr(cli, "_EscListener", _FakeListener)
    monkeypatch.setattr(cli, "_telemetry_row_count", lambda endpoint: None)
    monkeypatch.setattr(cli, "autoremove", lambda: None)
    monkeypatch.setattr(cli, "cleanup", lambda: None)
    monkeypatch.setattr(cli.quality_mod, "load_pack", lambda: ({"pack_id": "p", "items": []}, "sha"))
    monkeypatch.setattr(cli, "_run_contribution_loop", lambda *a, **k: cli._ContributionStats(benchmarked=[]))
    confirms = []
    monkeypatch.setattr(cli, "_ask_confirm", lambda message, **k: confirms.append(message) or True)

    result = runner.invoke(cli.app, ["contribute"])

    assert result.exit_code == 0, result.stdout
    assert "without asking each time" not in result.stdout
    assert confirms == ["Start contributing compute now?"]


def _seed_cooled_down_candidate(ref, filename, reason="memory_pressure_cancelled"):
    for _ in range(cli.benchmark_history.MACHINE_FAILURE_COOLDOWN_STREAK):
        cli.benchmark_history.record_benchmark_failure(
            ref, repo_id="o", filename=filename, reason=reason, engine="ollama"
        )


def test_candidate_in_failure_cooldown_is_announced_and_held_out_of_the_queue(
    isolated_omm_home, monkeypatch
):
    config.update_config(telemetry_endpoint="https://example.com/telemetry.json")
    _seed_cooled_down_candidate("huggingface:o:a.gguf", "a.gguf")
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: True)
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(
        cli.predictor,
        "load_model_with_change_note",
        lambda url, *a, **k: (
            {
                "trees": [{}],
                "candidates": [
                    {"repo_id": "o", "filename": "a.gguf"},
                    {"repo_id": "o", "filename": "b.gguf"},
                ],
            },
            False,
        ),
    )
    monkeypatch.setattr(cli, "scan_hardware", lambda: object())
    monkeypatch.setattr(cli.predictor, "rank_candidates", lambda artifact, hw: [])
    monkeypatch.setattr(cli, "_EscListener", _FakeListener)
    monkeypatch.setattr(cli, "_telemetry_row_count", lambda endpoint: 100)
    queues = []

    def fake_loop(queue, *a, **k):
        queues.append(queue)
        return cli._ContributionStats(benchmarked=[])

    monkeypatch.setattr(cli, "_run_contribution_loop", fake_loop)
    monkeypatch.setattr(cli, "autoremove", lambda: None)
    monkeypatch.setattr(cli, "cleanup", lambda: None)

    result = runner.invoke(cli.app, ["contribute"])

    assert result.exit_code == 0, result.stdout
    assert "Skipping a.gguf" in result.stdout
    assert "memory_pressure_cancelled" in result.stdout
    assert queues[0].excluded_refs == {"huggingface:o:a.gguf"}
    # Never tried this session, so never counted as covered by it.
    assert queues[0].history_refs == set()


def test_candidate_whose_cooldown_has_lapsed_is_offered_again(isolated_omm_home, monkeypatch):
    config.update_config(telemetry_endpoint="https://example.com/telemetry.json")
    _seed_cooled_down_candidate("huggingface:o:a.gguf", "a.gguf")
    history = isolated_omm_home / "benchmark_history.json"
    data = json.loads(history.read_text())
    stale = datetime.now(timezone.utc) - timedelta(
        hours=cli.benchmark_history.MACHINE_FAILURE_COOLDOWN_HOURS + 1
    )
    data["failures"]["huggingface:o:a.gguf"]["last_machine_failure_at"] = stale.isoformat()
    history.write_text(json.dumps(data))
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: True)
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(
        cli.predictor,
        "load_model_with_change_note",
        lambda url, *a, **k: (
            {"trees": [{}], "candidates": [{"repo_id": "o", "filename": "a.gguf"}]},
            False,
        ),
    )
    monkeypatch.setattr(cli, "scan_hardware", lambda: object())
    monkeypatch.setattr(cli.predictor, "rank_candidates", lambda artifact, hw: [])
    monkeypatch.setattr(cli, "_EscListener", _FakeListener)
    monkeypatch.setattr(cli, "_telemetry_row_count", lambda endpoint: 100)
    queues = []

    def fake_loop(queue, *a, **k):
        queues.append(queue)
        return cli._ContributionStats(benchmarked=[])

    monkeypatch.setattr(cli, "_run_contribution_loop", fake_loop)
    monkeypatch.setattr(cli, "autoremove", lambda: None)
    monkeypatch.setattr(cli, "cleanup", lambda: None)

    result = runner.invoke(cli.app, ["contribute"])

    assert result.exit_code == 0, result.stdout
    assert "Skipping a.gguf" not in result.stdout
    assert queues[0].excluded_refs == set()
