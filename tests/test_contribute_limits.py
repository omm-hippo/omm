import math
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests
from typer.testing import CliRunner

from omm import cli, config, downloader
from omm.contribute_session import ContributionLimits


@pytest.fixture
def model_server(monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        payload = b"0123456789ab"
        known_length = True
        requests = 0
        truncate_first = False

        def do_GET(self):
            type(self).requests += 1
            self.send_response(200)
            if self.known_length:
                self.send_header("Content-Length", str(len(self.payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(self.payload[:5] if self.truncate_first and self.requests == 1 else self.payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/model"
    monkeypatch.setattr(downloader, "_https_get", lambda source, **kwargs: requests.get(url, **kwargs))
    monkeypatch.setattr(downloader, "_CHUNK_SIZE", 5)
    try:
        yield Handler
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_byte_limit_uses_the_real_stream_and_exact_boundary(model_server, tmp_path):
    target = tmp_path / "model.gguf"
    budget = downloader.DownloadBudget(12)
    with downloader.download_budget_scope(budget):
        downloader.download_file("https://fixture.invalid/model", target, quiet=True)
    assert target.read_bytes() == model_server.payload
    assert budget.used == 12
    assert model_server.requests == 1


def test_oversized_model_is_skipped_before_reading_the_body(model_server, tmp_path):
    target = tmp_path / "model.gguf"
    budget = downloader.DownloadBudget(7)
    with downloader.download_budget_scope(budget), pytest.raises(downloader.DownloadBudgetSkipped):
        downloader.download_file("https://fixture.invalid/model", target, quiet=True)
    assert budget.used == 0
    assert not target.exists()


def test_unknown_size_stream_stops_at_the_byte_limit(model_server, tmp_path):
    model_server.known_length = False
    target = tmp_path / "model.gguf"
    stopped = []
    budget = downloader.DownloadBudget(7, lambda: stopped.append(True))
    with downloader.download_budget_scope(budget), pytest.raises(downloader.DownloadCancelled):
        downloader.download_file("https://fixture.invalid/model", target, quiet=True)
    assert budget.used == 7
    assert target.with_suffix(".gguf.part").read_bytes() == model_server.payload[:7]
    assert not target.exists() and stopped == [True]


def test_retry_bytes_count_against_the_same_allowance(model_server, tmp_path, monkeypatch):
    model_server.truncate_first = True
    monkeypatch.setattr(downloader, "_RETRY_DELAYS", [0] * 9)
    target = tmp_path / "model.gguf"
    budget = downloader.DownloadBudget(17)
    with downloader.download_budget_scope(budget):
        downloader.download_file("https://fixture.invalid/model", target, quiet=True)
    assert target.read_bytes() == model_server.payload
    assert model_server.requests == 2
    assert budget.used == 17  # failed first 5 bytes + a clean 12-byte retry


@pytest.mark.parametrize("value", [0, -1, math.nan, math.inf])
def test_invalid_limits_are_rejected(value):
    with pytest.raises(ValueError):
        ContributionLimits(seconds=value)


@pytest.mark.parametrize("flag,value", [("--max-minutes", "nan"), ("--max-minutes", "0"),
                                        ("--max-download-gb", "inf"), ("--max-models", "-1")])
def test_invalid_cli_limits_do_not_start_an_engine(flag, value, monkeypatch):
    monkeypatch.setattr(cli, "_select_benchmark_engine", lambda: pytest.fail("invalid limits must not start work"))
    result = CliRunner().invoke(cli.app, ["contribute", flag, value, "--yes"])
    assert result.exit_code == 2, result.output


class Queue:
    history_refs = set()

    def __init__(self):
        self.items = iter([{"repo_id": "test/model", "filename": name} for name in ["one.gguf", "two.gguf"]])

    def next_candidate(self, **kwargs):
        return next(self.items, None)

    def mark_seen(self, reference):
        pass


@pytest.fixture
def safe_loop(monkeypatch):
    monkeypatch.setattr(cli, "_engine_daemon_reachable", lambda engine: True)
    monkeypatch.setattr(cli, "_contribute_candidate_memory_plan", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_contribute_native_model_exists", lambda *args: False)


def test_model_count_limit_does_not_start_the_next_model(safe_loop, monkeypatch):
    installed = []
    def install(resolved, **kwargs):
        installed.append(resolved.filename)
        return cli.InstallOutcome(resolved.filename, resolved.repo_id, {}, tokens_per_sec=20, telemetry_sent=True)
    monkeypatch.setattr(cli, "_install_impl", install)
    stats = cli._run_contribution_loop(Queue(), threading.Event(), None, limits=ContributionLimits(models=1))
    assert installed == ["one.gguf"]
    assert stats.attempted_models == 1 and stats.stop_reason == "model_limit"


def test_time_limit_interrupts_work_and_cleans_only_owned_partial(safe_loop, isolated_omm_home, monkeypatch):
    keep = config.MODELS_DIR / "personal.gguf.part"
    keep.write_bytes(b"personal partial")
    created = config.MODELS_DIR / "one.gguf.part"
    def install(resolved, **kwargs):
        created.write_bytes(b"this run's partial")
        assert kwargs["stop_event"].wait(2), "time limit did not request cancellation"
        raise cli.InstallInterrupted(resolved.filename)
    monkeypatch.setattr(cli, "_install_impl", install)
    stats = cli._run_contribution_loop(Queue(), threading.Event(), None, limits=ContributionLimits(seconds=0.05))
    assert stats.stop_reason == "time_limit"
    assert not created.exists()
    assert keep.read_bytes() == b"personal partial"
