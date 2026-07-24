import threading

from omm import benchmark_history, cli, registry


class _FakeQueue:
    def __init__(self, candidates):
        self._candidates = list(candidates)
        self.marked_seen = []

    def next_candidate(self, refetch=None):
        if not self._candidates:
            return None
        return self._candidates.pop(0)

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
    stop_event.set()
    monkeypatch.setattr(cli, "_install_impl", lambda *a, **k: (_ for _ in ()).throw(AssertionError))

    stats = cli._run_contribution_loop(queue, stop_event, refetch=None)

    assert stats.benchmarked == []


def test_stops_when_queue_exhausted(isolated_omm_home):
    queue = _FakeQueue([])
    stop_event = threading.Event()

    stats = cli._run_contribution_loop(queue, stop_event, refetch=None)

    assert stats.benchmarked == []
    assert stats.skipped_unfit == 0
    assert stats.attempted_not_uploaded == 0


def test_successful_benchmark_records_history_and_deletes_model(isolated_omm_home, monkeypatch):
    c = _candidate(filename="model.gguf")
    queue = _FakeQueue([c])
    stop_event = threading.Event()
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


def test_download_error_skips_candidate_and_continues(isolated_omm_home, monkeypatch):
    c1 = _candidate(filename="bad.gguf", name="bad")
    c2 = _candidate(filename="good.gguf", name="good")
    queue = _FakeQueue([c1, c2])
    stop_event = threading.Event()
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
