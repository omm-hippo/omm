import threading
from types import SimpleNamespace

import pytest

from omm import benchmark_history, cli, registry


class _FakeQueue:
    def __init__(self, candidates):
        self._candidates = list(candidates)
        self.marked_seen = []

    def next_candidate(self, refetch=None, fetch_siblings=None):
        # Mirrors the real ContributionQueue: a candidate marked seen must
        # never be handed out again, even if it's still in the backing list.
        while self._candidates:
            candidate = self._candidates.pop(0)
            if cli.contribute_mod.ref(candidate) not in self.marked_seen:
                return candidate
        return None

    def mark_seen(self, ref):
        self.marked_seen.append(ref)


def _candidate(repo_id="org/repo", filename="model.gguf", name="model", provider="huggingface"):
    return {"repo_id": repo_id, "filename": filename, "name": name, "provider": provider}


def _seed_registry_entry(filename, sha256="deadbeef"):
    registry.upsert_entry(
        filename,
        sha256=sha256,
        version=sha256[:7],
        linked={"lmstudio": False, "ollama": True},
    )


def test_stops_immediately_when_stop_event_already_set(isolated_omm_home, monkeypatch):
    queue = _FakeQueue([_candidate()])
    stop_event = threading.Event()
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    stop_event.set()
    monkeypatch.setattr(cli, "_install_impl", lambda *a, **k: (_ for _ in ()).throw(AssertionError))

    stats = cli._run_contribution_loop(queue, stop_event, refetch=None)

    assert stats.benchmarked == []


def test_stops_when_queue_exhausted(isolated_omm_home, monkeypatch):
    queue = _FakeQueue([])
    stop_event = threading.Event()
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)

    stats = cli._run_contribution_loop(queue, stop_event, refetch=None)

    assert stats.benchmarked == []
    assert stats.skipped_unfit == 0
    assert stats.attempted_not_uploaded == 0


def test_low_memory_candidate_is_skipped_before_download(isolated_omm_home, monkeypatch):
    candidate = _candidate(filename="too-large-for-live-memory.gguf")
    candidate["size_bytes"] = 1024**3
    queue = _FakeQueue([candidate])
    stop_event = threading.Event()
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    monkeypatch.setattr(
        cli,
        "_contribute_candidate_memory_plan",
        lambda candidate, **kwargs: SimpleNamespace(
            decision=cli.memory_guard_mod.GuardDecision.BLOCK,
            required_gb=1.2,
            available_gb=0.0,
        ),
    )
    monkeypatch.setattr(
        cli,
        "_install_impl",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("download/install must not start")
        ),
    )

    stats = cli._run_contribution_loop(queue, stop_event, refetch=None)

    assert stats.skipped_low_memory == 1
    assert stats.benchmarked == []
    assert queue.marked_seen == [
        "huggingface:org/repo:too-large-for-live-memory.gguf"
    ]


def test_start_memory_preflight_aborts_when_every_pending_candidate_is_blocked(
    isolated_omm_home, monkeypatch
):
    artifact = {
        "candidates": [
            _candidate(filename="small.gguf") | {"size_bytes": 512 * 1024**2},
            _candidate(filename="large.gguf") | {"size_bytes": 2 * 1024**3},
        ]
    }
    monkeypatch.setattr(
        cli.memory_guard_mod,
        "OllamaManagedRuntime",
        lambda registry_data: SimpleNamespace(list_residents=lambda: ()),
    )
    monkeypatch.setattr(
        cli,
        "_contribute_candidate_memory_plan",
        lambda candidate, **kwargs: SimpleNamespace(
            decision=cli.memory_guard_mod.GuardDecision.BLOCK,
            required_gb=candidate["size_bytes"] / 1024**3 * 1.2,
            available_gb=0.0,
            reserve_gb=2.0,
        ),
    )

    with pytest.raises(cli.typer.Exit) as error:
        cli._ensure_contribute_candidate_memory(artifact, object(), set())

    assert error.value.exit_code == 1


def test_low_memory_candidate_check_queries_lmstudio_residents_for_lmstudio_engine(
    isolated_omm_home, monkeypatch
):
    """The pre-download memory check must ask the engine actually being
    benchmarked what's resident, not always Ollama - a contribute session
    running against LM Studio would otherwise plan against an empty (or
    just wrong) resident list."""
    candidate = _candidate(filename="too-large-for-live-memory.gguf")
    candidate["size_bytes"] = 1024**3
    queue = _FakeQueue([candidate])
    stop_event = threading.Event()
    monkeypatch.setattr(cli.linker, "lmstudio_daemon_reachable", lambda: True)
    monkeypatch.setattr(
        cli,
        "scan_hardware",
        lambda: cli.HardwareInfo(
            os_name="Linux", os_version="", cpu="CPU",
            ram_total_gb=16, ram_available_gb=0.05,
            unified_memory=False, gpu_name=None,
            vram_total_gb=None, vram_free_gb=None,
        ),
    )

    monkeypatch.setattr(
        cli.memory_guard_mod,
        "OllamaManagedRuntime",
        lambda registry_data: (_ for _ in ()).throw(
            AssertionError("must not query Ollama residents for engine=lmstudio")
        ),
    )
    seen = {}

    class _FakeLMStudioRuntime:
        def __init__(self, registry_data):
            seen["queried"] = True

        def list_residents(self):
            return ()

    monkeypatch.setattr(cli.memory_guard_mod, "LMStudioManagedRuntime", _FakeLMStudioRuntime)

    monkeypatch.setattr(
        cli,
        "_install_impl",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("download/install must not start")
        ),
    )

    stats = cli._run_contribution_loop(queue, stop_event, refetch=None, engine="lmstudio")

    assert seen.get("queried") is True
    assert stats.skipped_low_memory == 1
    assert stats.benchmarked == []


def test_successful_benchmark_records_history_and_deletes_model(isolated_omm_home, monkeypatch):
    c = _candidate(filename="model.gguf")
    queue = _FakeQueue([c])
    stop_event = threading.Event()
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    _seed_registry_entry("model.gguf")

    def fake_install_impl(resolved, **kwargs):
        stop_event.set()  # stop the loop after this one iteration
        return cli.InstallOutcome(
            filename="model.gguf",
            repo_id="org/repo",
            linked={"lmstudio": False, "ollama": True},
            tokens_per_sec=42.0,
            telemetry_sent=True,
            sha256="deadbeef",
        )

    monkeypatch.setattr(cli, "_install_impl", fake_install_impl)
    removed = []
    monkeypatch.setattr(cli, "_remove_one", lambda fn, entry: removed.append(fn))

    stats = cli._run_contribution_loop(queue, stop_event, refetch=None)

    assert stats.benchmarked == [("model", 42.0)]
    assert removed == ["model.gguf"]
    assert benchmark_history.has_been_benchmarked("huggingface:org/repo:model.gguf")
    assert queue.marked_seen == ["huggingface:org/repo:model.gguf"]


def test_skipped_unfit_candidate_counted_and_not_deleted(isolated_omm_home, monkeypatch):
    c = _candidate(filename="too-big.gguf")
    queue = _FakeQueue([c])
    stop_event = threading.Event()
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)

    def fake_install_impl(resolved, **kwargs):
        stop_event.set()
        return cli.InstallOutcome(
            filename="too-big.gguf", repo_id="org/repo", linked={}, skipped_unfit=True
        )

    monkeypatch.setattr(cli, "_install_impl", fake_install_impl)
    removed = []
    monkeypatch.setattr(cli, "_remove_one", lambda fn, entry: removed.append(fn))

    stats = cli._run_contribution_loop(queue, stop_event, refetch=None)

    assert stats.skipped_unfit == 1
    assert stats.benchmarked == []
    assert removed == []
    assert queue.marked_seen == ["huggingface:org/repo:too-big.gguf"]


def test_unfit_candidates_eventually_exhaust_instead_of_spinning_forever(
    isolated_omm_home, monkeypatch
):
    """Once the only hardware-fit candidate is gone, the queue's "above"
    pool of unfit candidates must also become exhausted - otherwise
    next_candidate() always has one more unfit entry to hand back and the
    loop spins at machine speed forever instead of ever stopping."""
    candidates = [_candidate(filename=f"unfit-{i}.gguf") for i in range(3)]
    queue = _FakeQueue(list(candidates))
    stop_event = threading.Event()
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)

    def fake_install_impl(resolved, **kwargs):
        return cli.InstallOutcome(
            filename=resolved.filename, repo_id="org/repo", linked={}, skipped_unfit=True
        )

    monkeypatch.setattr(cli, "_install_impl", fake_install_impl)

    stats = cli._run_contribution_loop(queue, stop_event, refetch=None)

    assert stats.skipped_unfit == 3
    assert len(queue.marked_seen) == 3


def test_upload_failure_counts_as_not_uploaded_and_does_not_mark_seen(isolated_omm_home, monkeypatch):
    c = _candidate(filename="model.gguf")
    queue = _FakeQueue([c])
    stop_event = threading.Event()
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    _seed_registry_entry("model.gguf")

    def fake_install_impl(resolved, **kwargs):
        stop_event.set()
        return cli.InstallOutcome(
            filename="model.gguf",
            repo_id="org/repo",
            linked={"lmstudio": False, "ollama": True},
            tokens_per_sec=42.0,
            telemetry_sent=False,
            sha256="deadbeef",
        )

    monkeypatch.setattr(cli, "_install_impl", fake_install_impl)
    removed = []
    monkeypatch.setattr(cli, "_remove_one", lambda fn, entry: removed.append(fn))

    stats = cli._run_contribution_loop(queue, stop_event, refetch=None)

    assert stats.attempted_not_uploaded == 1
    assert removed == ["model.gguf"]
    assert queue.marked_seen == []
    assert not benchmark_history.has_been_benchmarked("huggingface:org/repo:model.gguf")


def test_ollama_unreachable_mid_loop_counts_as_not_uploaded(isolated_omm_home, monkeypatch):
    c = _candidate(filename="model.gguf")
    queue = _FakeQueue([c])
    stop_event = threading.Event()
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    _seed_registry_entry("model.gguf")

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
    removed = []
    monkeypatch.setattr(cli, "_remove_one", lambda fn, entry: removed.append(fn))

    stats = cli._run_contribution_loop(queue, stop_event, refetch=None)

    assert stats.attempted_not_uploaded == 1
    assert removed == ["model.gguf"]


def test_gives_up_on_candidate_that_repeatedly_fails_to_benchmark(isolated_omm_home, monkeypatch):
    """A candidate that never produces a real tokens_per_sec (e.g. it
    reliably crashes the daemon or times out every time) must not be
    re-offered by the queue forever - that would burn the whole unattended
    session on one broken model while every other candidate goes untried
    (the exact failure mode behind repeated 0-upload `omm contribute` runs).
    """
    c = _candidate(filename="model.gguf")
    # The real queue would keep re-offering this candidate since it never
    # gets marked seen on failure; the fake queue mirrors that by handing it
    # back out every time next_candidate() is called, until mark_seen fires.
    queue = _FakeQueue([c, c, c])
    stop_event = threading.Event()
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    _seed_registry_entry("model.gguf")

    calls = []

    def fake_install_impl(resolved, **kwargs):
        calls.append(resolved.filename)
        return cli.InstallOutcome(
            filename="model.gguf",
            repo_id="org/repo",
            linked={"lmstudio": False, "ollama": True},
            tokens_per_sec=None,
            telemetry_sent=False,
        )

    monkeypatch.setattr(cli, "_install_impl", fake_install_impl)
    monkeypatch.setattr(cli, "_remove_one", lambda fn, entry: None)

    stats = cli._run_contribution_loop(queue, stop_event, refetch=None)

    # Queue only had 3 entries queued up front (simulating "keeps getting
    # re-offered"); the loop must stop asking for it after the 2nd failure.
    assert calls == ["model.gguf", "model.gguf"]
    assert stats.attempted_not_uploaded == 2
    assert stats.given_up_on == 1
    assert queue.marked_seen == ["huggingface:org/repo:model.gguf"]


def test_giving_up_on_candidate_reports_failure_telemetry_with_real_reason(
    isolated_omm_home, monkeypatch
):
    """Once a candidate is given up on, the *real* reason it failed
    (generation_timeout, out_of_memory, etc. - never a vague "daemon"
    guess) should be reported so future sessions don't have to rediscover
    "this doesn't work on this hardware" from scratch every time."""
    c = _candidate(filename="model.gguf")
    queue = _FakeQueue([c, c])
    stop_event = threading.Event()
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    _seed_registry_entry("model.gguf")

    def fake_install_impl(resolved, **kwargs):
        return cli.InstallOutcome(
            filename="model.gguf",
            repo_id="org/repo",
            linked={"lmstudio": False, "ollama": True},
            ollama_tag="model:latest",
            tokens_per_sec=None,
            telemetry_sent=False,
            failure_reason="generation_timeout",
            model_metadata={"parameter_size": "9B", "quantization_level": "Q4_K_M"},
        )

    monkeypatch.setattr(cli, "_install_impl", fake_install_impl)
    monkeypatch.setattr(cli, "_remove_one", lambda fn, entry: None)
    reported = []
    monkeypatch.setattr(
        cli, "_report_contribute_failure_telemetry", lambda outcome: reported.append(outcome)
    )

    stats = cli._run_contribution_loop(queue, stop_event, refetch=None)

    assert stats.given_up_on == 1
    assert len(reported) == 1
    assert reported[0].failure_reason == "generation_timeout"
    assert reported[0].model_metadata == {"parameter_size": "9B", "quantization_level": "Q4_K_M"}


def test_report_contribute_failure_telemetry_sends_transient_error_event(
    isolated_omm_home, monkeypatch
):
    monkeypatch.setattr(
        cli, "scan_hardware",
        lambda: SimpleNamespace(
            ram_total_gb=8.0, vram_total_gb=None, unified_memory=True, gpu_tflops=None,
            cpu=None, cpu_arch=None, cpu_physical_cores=None, cpu_logical_cores=None,
        ),
    )
    sent = []
    monkeypatch.setattr(
        cli.telemetry, "send_event", lambda event, force=False: sent.append(event) or True
    )

    outcome = cli.InstallOutcome(
        filename="model.gguf",
        repo_id="org/repo",
        linked={},
        ollama_tag="model:latest",
        failure_reason="generation_timeout",
        model_metadata={"parameter_size": "9B", "quantization_level": "Q4_K_M"},
    )

    cli._report_contribute_failure_telemetry(outcome)

    assert len(sent) == 1
    assert sent[0]["outcome"] == "transient_error"
    assert sent[0]["failure_reason"] == "generation_timeout"


def test_daemon_dies_mid_benchmark_retries_same_candidate_once(isolated_omm_home, monkeypatch):
    """Daemon crashes *during* a candidate's own download/benchmark (not
    between candidates - that's the other test). The already-downloaded
    model must get one retry after the daemon comes back, instead of being
    thrown away and re-downloaded from scratch as a "new" candidate."""
    c = _candidate(filename="model.gguf")
    queue = _FakeQueue([c])
    stop_event = threading.Event()
    _seed_registry_entry("model.gguf")

    reachable_calls = [True, False]
    monkeypatch.setattr(
        cli.benchmark, "ollama_daemon_reachable", lambda: reachable_calls.pop(0)
    )
    restarted = []
    fake_proc = object()
    monkeypatch.setattr(
        cli.benchmark, "start_ollama_daemon", lambda: (restarted.append(1), fake_proc)[1]
    )

    calls = []

    def fake_install_impl(resolved, **kwargs):
        calls.append(resolved.filename)
        if len(calls) == 1:
            return cli.InstallOutcome(
                filename="model.gguf",
                repo_id="org/repo",
                linked={"lmstudio": False, "ollama": True},
                tokens_per_sec=None,
                telemetry_sent=False,
            )
        stop_event.set()
        return cli.InstallOutcome(
            filename="model.gguf",
            repo_id="org/repo",
            linked={"lmstudio": False, "ollama": True},
            tokens_per_sec=42.0,
            telemetry_sent=True,
            sha256="deadbeef",
        )

    monkeypatch.setattr(cli, "_install_impl", fake_install_impl)
    removed = []
    monkeypatch.setattr(cli, "_remove_one", lambda fn, entry: removed.append(fn))

    daemon_ref = {"proc": None}
    stats = cli._run_contribution_loop(queue, stop_event, refetch=None, daemon_ref=daemon_ref)

    assert calls == ["model.gguf", "model.gguf"]
    assert restarted == [1]
    assert daemon_ref["proc"] is fake_proc
    assert stats.daemon_restarts == 1
    assert stats.benchmarked == [("model", 42.0)]
    assert removed == ["model.gguf"]  # only removed once, after the final outcome


def test_daemon_wont_restart_after_dying_mid_benchmark_gives_up_on_candidate(
    isolated_omm_home, monkeypatch
):
    """If the daemon can't be restarted after dying mid-benchmark, fall back
    to the existing not-uploaded bookkeeping instead of retrying forever."""
    c = _candidate(filename="model.gguf")
    queue = _FakeQueue([c])
    stop_event = threading.Event()
    _seed_registry_entry("model.gguf")

    reachable_calls = [True, False]
    monkeypatch.setattr(
        cli.benchmark, "ollama_daemon_reachable", lambda: reachable_calls.pop(0)
    )
    monkeypatch.setattr(cli.benchmark, "start_ollama_daemon", lambda: None)

    calls = []

    def fake_install_impl(resolved, **kwargs):
        calls.append(resolved.filename)
        stop_event.set()
        return cli.InstallOutcome(
            filename="model.gguf",
            repo_id="org/repo",
            linked={"lmstudio": False, "ollama": True},
            tokens_per_sec=None,
            telemetry_sent=False,
        )

    monkeypatch.setattr(cli, "_install_impl", fake_install_impl)
    removed = []
    monkeypatch.setattr(cli, "_remove_one", lambda fn, entry: removed.append(fn))

    stats = cli._run_contribution_loop(queue, stop_event, refetch=None)

    assert calls == ["model.gguf"]  # no retry attempted - daemon never came back
    assert stats.daemon_restarts == 0
    assert stats.attempted_not_uploaded == 1
    assert removed == ["model.gguf"]


def test_dead_daemon_is_restarted_before_next_candidate(isolated_omm_home, monkeypatch):
    """Daemon crashed mid-session (e.g. OOM): the loop must notice before
    burning bandwidth on a download, restart it, and carry on - instead of
    downloading a multi-GB file only to fail the benchmark afterwards."""
    c = _candidate(filename="model.gguf")
    queue = _FakeQueue([c])
    stop_event = threading.Event()
    _seed_registry_entry("model.gguf")

    reachable_calls = [False, True]
    monkeypatch.setattr(
        cli.benchmark, "ollama_daemon_reachable", lambda: reachable_calls.pop(0)
    )
    restarted = []
    fake_proc = object()
    monkeypatch.setattr(
        cli.benchmark, "start_ollama_daemon", lambda: (restarted.append(1), fake_proc)[1]
    )

    def fake_install_impl(resolved, **kwargs):
        stop_event.set()
        return cli.InstallOutcome(
            filename="model.gguf",
            repo_id="org/repo",
            linked={"lmstudio": False, "ollama": True},
            tokens_per_sec=42.0,
            telemetry_sent=True,
            sha256="deadbeef",
        )

    monkeypatch.setattr(cli, "_install_impl", fake_install_impl)
    monkeypatch.setattr(cli, "_remove_one", lambda fn, entry: None)

    daemon_ref = {"proc": None}
    stats = cli._run_contribution_loop(queue, stop_event, refetch=None, daemon_ref=daemon_ref)

    assert restarted == [1]
    assert daemon_ref["proc"] is fake_proc
    assert stats.daemon_restarts == 1
    assert stats.benchmarked == [("model", 42.0)]


def test_dead_lmstudio_daemon_is_restarted_before_next_candidate(isolated_omm_home, monkeypatch):
    """Mirrors test_dead_daemon_is_restarted_before_next_candidate for the
    LM Studio engine: the loop's daemon-health check must dispatch to
    linker.lmstudio_daemon_reachable/start_lmstudio_daemon, not the Ollama
    functions, when engine="lmstudio"."""
    c = _candidate(filename="model.gguf")
    queue = _FakeQueue([c])
    stop_event = threading.Event()
    _seed_registry_entry("model.gguf")

    reachable_calls = [False, True]
    monkeypatch.setattr(
        cli.linker, "lmstudio_daemon_reachable", lambda: reachable_calls.pop(0)
    )
    monkeypatch.setattr(
        cli.benchmark,
        "ollama_daemon_reachable",
        lambda: (_ for _ in ()).throw(AssertionError("must not check Ollama for engine=lmstudio")),
    )
    restarted = []
    monkeypatch.setattr(
        cli.linker, "start_lmstudio_daemon", lambda: (restarted.append(1), True)[1]
    )

    def fake_install_impl(resolved, **kwargs):
        assert kwargs.get("benchmark_engine") == "lmstudio"
        assert kwargs.get("link_only_engine") == "lmstudio"
        stop_event.set()
        return cli.InstallOutcome(
            filename="model.gguf",
            repo_id="org/repo",
            linked={"lmstudio": True, "ollama": False},
            tokens_per_sec=42.0,
            telemetry_sent=True,
            sha256="deadbeef",
        )

    monkeypatch.setattr(cli, "_install_impl", fake_install_impl)
    monkeypatch.setattr(cli, "_remove_one", lambda fn, entry: None)

    daemon_ref = {"proc": None}
    stats = cli._run_contribution_loop(
        queue, stop_event, refetch=None, daemon_ref=daemon_ref, engine="lmstudio"
    )

    assert restarted == [1]
    assert daemon_ref["proc"] is True
    assert stats.daemon_restarts == 1
    assert stats.benchmarked == [("model", 42.0)]


def test_daemon_that_wont_come_back_aborts_loop_instead_of_spinning(isolated_omm_home, monkeypatch):
    """If the daemon can't be restarted at all, the loop must give up after
    a few tries rather than looping unattended for hours re-downloading
    models it can never benchmark."""
    queue = _FakeQueue([_candidate(filename="model.gguf")] * 10)
    stop_event = threading.Event()
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: False)
    monkeypatch.setattr(cli.benchmark, "start_ollama_daemon", lambda: None)
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        cli, "_install_impl", lambda *a, **k: (_ for _ in ()).throw(AssertionError)
    )

    stats = cli._run_contribution_loop(queue, stop_event, refetch=None)

    assert stats.benchmarked == []
    assert stats.daemon_restarts == 0
    assert len(queue._candidates) == 10  # never even attempted a download


def test_download_error_skips_candidate_and_continues(isolated_omm_home, monkeypatch):
    c1 = _candidate(filename="bad.gguf", name="bad")
    c2 = _candidate(filename="good.gguf", name="good")
    queue = _FakeQueue([c1, c2])
    stop_event = threading.Event()
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    _seed_registry_entry("good.gguf")

    calls = []

    def fake_install_impl(resolved, **kwargs):
        calls.append(resolved.filename)
        if resolved.filename == "bad.gguf":
            raise cli.DownloadError("network broke")
        stop_event.set()
        return cli.InstallOutcome(
            filename="good.gguf",
            repo_id="org/repo",
            linked={"lmstudio": False, "ollama": True},
            tokens_per_sec=10.0,
            telemetry_sent=True,
            sha256="abc",
        )

    monkeypatch.setattr(cli, "_install_impl", fake_install_impl)
    monkeypatch.setattr(cli, "_remove_one", lambda fn, entry: None)

    stats = cli._run_contribution_loop(queue, stop_event, refetch=None)

    assert calls == ["bad.gguf", "good.gguf"]
    assert stats.benchmarked == [("good", 10.0)]


def test_contribution_stopped_cleans_up_and_breaks(isolated_omm_home, monkeypatch):
    c = _candidate(filename="model.gguf")
    queue = _FakeQueue([c, _candidate(filename="never-reached.gguf")])
    stop_event = threading.Event()
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)
    _seed_registry_entry("model.gguf")

    def fake_install_impl(resolved, **kwargs):
        raise cli.ContributionStopped("model.gguf")

    monkeypatch.setattr(cli, "_install_impl", fake_install_impl)
    cleaned = []
    monkeypatch.setattr(cli, "_cleanup_incomplete_install", lambda fn: cleaned.append(fn))
    removed = []
    monkeypatch.setattr(cli, "_remove_one", lambda fn, entry: removed.append(fn))

    stats = cli._run_contribution_loop(queue, stop_event, refetch=None)

    assert cleaned == ["model.gguf"]
    assert removed == ["model.gguf"]
    assert stats.benchmarked == []


def test_run_contribution_loop_builds_url_via_provider_dispatch(isolated_omm_home, monkeypatch):
    seen_urls = []
    stop_event = threading.Event()
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)

    def fake_install_impl(resolved, **kwargs):
        seen_urls.append(resolved.url)
        stop_event.set()  # stop the loop after this one iteration
        return cli.InstallOutcome(
            resolved.filename, resolved.repo_id, {}, None, 5.0, True, sha256="x"
        )

    monkeypatch.setattr(cli, "_install_impl", fake_install_impl)
    monkeypatch.setattr(cli.registry, "load_registry", lambda: {})
    monkeypatch.setattr(cli, "_lookup_entry", lambda filename, reg: (None, None))
    monkeypatch.setattr(
        cli.benchmark_history, "record_benchmarked", lambda *a, **k: None
    )

    candidate = _candidate(repo_id="org/repo", filename="model.gguf", provider="modelscope")
    queue = _FakeQueue([candidate])

    cli._run_contribution_loop(queue, stop_event, refetch=lambda: (None, False))

    assert seen_urls == [
        "https://modelscope.cn/api/v1/models/org/repo/repo?Revision=master&FilePath=model.gguf"
    ]


def test_skipped_low_disk_candidate_counted_and_not_deleted(isolated_omm_home, monkeypatch):
    c = _candidate(filename="too-big.gguf")
    queue = _FakeQueue([c])
    stop_event = threading.Event()
    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: True)

    def fake_install_impl(resolved, **kwargs):
        stop_event.set()
        return cli.InstallOutcome(
            filename="too-big.gguf", repo_id="org/repo", linked={}, skipped_low_disk=True
        )

    monkeypatch.setattr(cli, "_install_impl", fake_install_impl)
    removed = []
    monkeypatch.setattr(cli, "_remove_one", lambda fn, entry: removed.append(fn))

    stats = cli._run_contribution_loop(queue, stop_event, refetch=None)

    assert stats.skipped_low_disk == 1
    assert stats.benchmarked == []
    assert removed == []
    assert queue.marked_seen == ["huggingface:org/repo:too-big.gguf"]


def test_print_contribution_summary_includes_low_disk_skip_count(capsys):
    stats = cli._ContributionStats(benchmarked=[], skipped_unfit=1, skipped_low_disk=2)

    cli._print_contribution_summary(stats, 12.0, None, None)

    captured = capsys.readouterr()
    assert "not enough disk space): 2" in captured.out


def test_print_contribution_summary_includes_pre_download_memory_skip_count(capsys):
    stats = cli._ContributionStats(benchmarked=[], skipped_low_memory=3)

    cli._print_contribution_summary(stats, 12.0, None, None)

    captured = capsys.readouterr()
    assert "not enough live memory): 3" in captured.out
