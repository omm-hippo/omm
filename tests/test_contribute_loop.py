import threading

from omm import benchmark_history, cli, registry


class _FakeQueue:
    def __init__(self, candidates):
        self._candidates = list(candidates)
        self.marked_seen = []

    def next_candidate(self, refetch=None):
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
