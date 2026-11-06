"""The poster worker loop: gate, claim, draw, upload, fail, state, status line."""

from __future__ import annotations

import json
import logging

import pytest

from omm.cluster import poster_worker as worker
from omm.cluster.poster_contract import BadJob, Job, TransientError
from omm.cluster.poster_drawer import DrawerError, DrawerTimeout

TOKEN = "t" * 43
JOB = Job("3f2a1c4e-0000-4000-8000-000000000001", "a robot", 1000, 750, "s3cret")
OWN = "12D3KooWOwn"
OTHER = "12D3KooWOther"


def _state(runner_state: str | None, node: str = OWN) -> dict:
    runners = {} if runner_state is None else {"r1": {runner_state: {}}}
    return {"instances": {"i1": {"MlxRingInstance": {"shardAssignments": {
        "modelId": "m", "nodeToRunner": {node: "r1"}, "runnerToShard": {"r1": {}}}}}}, "runners": runners}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class FakeClient:
    def __init__(self, claims=(), upload_status=200):
        self.claims = list(claims)
        self.calls = []
        self.upload_status = upload_status

    def claim(self):
        self.calls.append(("claim",))
        item = self.claims.pop(0) if self.claims else None
        if isinstance(item, Exception):
            raise item
        return item

    def ping(self):
        self.calls.append(("ping",))
        return True

    def upload(self, job, jpeg):
        self.calls.append(("upload", job.id, jpeg))
        return self.upload_status

    def fail(self, job_id, reason):
        self.calls.append(("fail", job_id, reason))


class FakeDrawer:
    def __init__(self, clock, *, draw_seconds=30.0, error=None):
        self.clock, self.draw_seconds, self.error = clock, draw_seconds, error
        self.requests, self.stopped, self.started = [], False, False

    @property
    def running(self):
        return self.started and not self.stopped

    def start(self, timeout):
        self.started = True
        return 1.0

    def draw(self, prompt, width, height, seed, out, timeout, quality=90):
        self.requests.append({"prompt": prompt, "width": width, "height": height, "timeout": timeout})
        if self.error:
            raise self.error
        self.clock.sleep(self.draw_seconds)
        out.write_bytes(b"\xff\xd8jpeg")
        return self.draw_seconds

    def stop(self):
        self.stopped = True


def _worker(clock, client, *, budget=40.0, critical=False, exo=None, drawers=None, **kw):
    drawers = drawers if drawers is not None else []

    def make_drawer():
        drawer = kw.pop("drawer", None) or FakeDrawer(clock)
        drawers.append(drawer)
        return drawer

    return worker.Worker(client, make_drawer, budget_gb=lambda: budget, pressure_critical=lambda: critical,
                         exo_state=lambda: (exo, OWN), now=clock.now, sleep=clock.sleep), drawers


def test_gate_closed_when_memory_short():
    assert worker.gate(False, 12.0, False) == (False, "waiting for memory")


def test_gate_open_with_enough_memory():
    assert worker.gate(False, 16.0, False) == (True, "ready")


def test_gate_closed_when_exo_loading():
    assert worker.gate(True, 40.0, False) == (False, "cluster model loading")


def test_gate_closed_on_critical_pressure():
    assert worker.gate(False, 40.0, True) == (False, "waiting for memory")


def test_exo_wants_memory():
    assert worker.exo_wants_memory(_state("RunnerLoading"), OWN)
    assert worker.exo_wants_memory(_state(None), OWN)  # instance placed, runner not up yet
    assert not worker.exo_wants_memory(_state("RunnerReady"), OWN)
    assert not worker.exo_wants_memory(_state("RunnerLoading", node=OTHER), OWN)
    assert not worker.exo_wants_memory(_state("RunnerFailed"), OWN)
    assert not worker.exo_wants_memory(None, OWN)
    assert not worker.exo_wants_memory(_state("RunnerLoading"), None)


def test_no_claim_or_ping_while_gate_closed(isolated_omm_home):
    clock, client = Clock(), FakeClient([JOB])
    w, _ = _worker(clock, client, budget=12.0)
    assert w.step() == worker.IDLE_SECONDS
    assert client.calls == []
    assert worker.read_state()["phase"] == "waiting for memory"


def test_no_claim_while_exo_loads_on_this_mac(isolated_omm_home):
    clock, client = Clock(), FakeClient([JOB])
    w, _ = _worker(clock, client, exo=_state("RunnerLoading"))
    w.step()
    assert client.calls == []
    assert worker.read_state()["phase"] == "cluster model loading"


def test_empty_claim_sleeps_three_seconds(isolated_omm_home):
    clock, client = Clock(), FakeClient([None])
    w, _ = _worker(clock, client)
    assert w.step() == 3


def test_loop_survives_repeated_network_errors(isolated_omm_home):
    clock = Clock()
    client = FakeClient([TransientError("x")] * 7 + [None])
    w, _ = _worker(clock, client)
    delays = [w.step() for _ in range(8)]
    assert delays == [3, 6, 12, 24, 48, 60, 60, 3]


def test_bad_job_with_id_is_failed(isolated_omm_home):
    clock, client = Clock(), FakeClient([BadJob(JOB.id), BadJob(None)])
    w, _ = _worker(clock, client)
    w.step()
    w.step()
    assert [c for c in client.calls if c[0] == "fail"] == [("fail", JOB.id, "bad job")]


def test_run_job_done_uses_fit_size_and_stops_drawer(isolated_omm_home):
    clock, client = Clock(), FakeClient([JOB])
    w, drawers = _worker(clock, client)
    assert w.step() == 0
    assert drawers[0].requests[0]["width"] == 992 and drawers[0].requests[0]["height"] == 736
    assert drawers[0].requests[0]["prompt"] == "a robot"
    assert drawers[0].stopped
    assert ("upload", JOB.id, b"\xff\xd8jpeg") in client.calls
    state = worker.read_state()
    assert state["last_job_id"] == JOB.id and state["last_outcome"] == "done"
    assert not list((worker.poster_config.poster_dir() / "tmp").glob("*"))


def test_draw_timeout_is_lease_minus_margin(isolated_omm_home):
    clock, client = Clock(), FakeClient([JOB])
    w, drawers = _worker(clock, client)
    w.step()
    assert drawers[0].requests[0]["timeout"] == pytest.approx(worker.LEASE_SECONDS - worker.DRAW_MARGIN_SECONDS)


def test_run_job_timeout_sends_fail(isolated_omm_home):
    clock, client = Clock(), FakeClient([JOB])
    w, _ = _worker(clock, client, drawer=FakeDrawer(clock, error=DrawerTimeout("slow")))
    w.step()
    assert ("fail", JOB.id, "timeout") in client.calls
    assert not any(c[0] == "upload" for c in client.calls)


def test_run_job_drawer_crash_sends_fail(isolated_omm_home):
    clock, client = Clock(), FakeClient([JOB, JOB])
    drawers = []
    w, _ = _worker(clock, client, drawers=drawers, drawer=FakeDrawer(clock, error=DrawerError("boom")))
    w.step()
    assert ("fail", JOB.id, "drawer error") in client.calls
    w.step()
    assert len(drawers) == 2 and drawers[1].requests  # a fresh drawer for the next job
    assert worker.read_state()["last_outcome"] == "done"


def test_ping_loop_pings_every_15s():
    clock, client = Clock(), FakeClient()
    w, _ = _worker(clock, client)
    answers, waits = iter([False, False, False, True]), []
    w._ping_loop(lambda seconds: waits.append(seconds) or next(answers))
    assert waits == [15] * 4 and client.calls == [("ping",)] * 3


def test_upload_drop_is_recorded(isolated_omm_home):
    clock, client = Clock(), FakeClient([JOB], upload_status=404)
    w, _ = _worker(clock, client)
    w.step()
    assert worker.read_state()["last_outcome"] == "dropped"
    assert not any(c[0] == "fail" for c in client.calls)


def test_token_never_logged(isolated_omm_home, caplog):
    caplog.set_level(logging.DEBUG, logger="omm")
    clock = Clock()
    client = FakeClient([TransientError("claim failed: ConnectionError"), BadJob(JOB.id), JOB, None])
    w, _ = _worker(clock, client)
    for _ in range(4):
        w.step()
    text = caplog.text + json.dumps(worker.read_state())
    assert TOKEN not in text and "a robot" not in text and "s3cret" not in text
    assert JOB.id in caplog.text


def test_status_line_phrasings():
    now = 10_000.0
    assert worker.status_line(None, installed=False, running=False, now=now) == "Poster art: off"
    assert worker.status_line({"phase": "downloading the model"}, True, True, now) == \
        "Poster art: on — downloading the model"
    assert worker.status_line({"phase": "waiting for memory", "budget_gb": 12.4}, True, True, now) == \
        "Poster art: on — waiting for memory (12 GiB free, needs 16)"
    assert worker.status_line({"phase": "drawing a picture"}, True, True, now) == \
        "Poster art: on — drawing a picture"
    ready = {"phase": "ready", "last_job_id": JOB.id, "last_outcome": "done", "last_seconds": 32.2,
             "last_at": now - 240}
    assert worker.status_line(ready, True, True, now) == \
        "Poster art: on — ready, last job 3f2a… done in 32 s (4 min ago)"
    assert worker.status_line({"phase": "ready"}, True, False, now) == \
        "Poster art: on — not running (see ~/.omm/logs/cluster-poster.log)"


def test_oversize_image_is_failed_not_uploaded(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(worker.poster_contract, "MAX_IMAGE_BYTES", 3)
    clock, client = Clock(), FakeClient([JOB])
    w, _ = _worker(clock, client)
    w.step()
    assert ("fail", JOB.id, "image too large") in client.calls
    assert not any(c[0] == "upload" for c in client.calls)


class _Stop(BaseException):  # run_worker retries ordinary exceptions forever
    pass


def test_no_claim_until_model_downloaded(isolated_omm_home, monkeypatch):
    from omm.cluster import poster_runtime

    monkeypatch.setattr(worker.poster_config, "load_settings",
                        lambda: worker.poster_config.PosterSettings("https://s", "anon", "https://site"))
    monkeypatch.setattr(worker.poster_config, "read_token", lambda: TOKEN)
    monkeypatch.setattr(poster_runtime, "model_snapshot", lambda: None)
    phases = []

    def download():
        phases.append(worker.read_state()["phase"])
        raise _Stop

    monkeypatch.setattr(poster_runtime, "download_model", download)
    monkeypatch.setattr(worker.poster_contract, "SiteClient", lambda *a, **k: pytest.fail("claimed before model"))
    with pytest.raises(_Stop):
        worker.run_worker()
    assert phases == ["downloading the model"]
