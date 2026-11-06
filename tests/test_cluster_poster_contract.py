"""The poster worker's side of the dshs.dev contract, against a local fake server.

Contract: DSHS repo docs/superpowers/specs/2026-11-06-poster-mac-art-design.md §5-3, §7.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import pytest

from omm.cluster import poster_contract
from omm.cluster.poster_config import PosterSettings
from omm.cluster.poster_contract import BadJob, Job, SiteClient, TransientError

TOKEN = "t" * 43
JOB_ID = "3f2a1c4e-0000-4000-8000-000000000001"
JOB = {"id": JOB_ID, "prompt": "a robot", "width": 1024, "height": 768, "job_secret": "s3cret"}


class Fake:
    def __init__(self):
        self.requests = []  # (method, path, headers, body)
        self.replies = {}   # path -> list of (status, body bytes); last one repeats

    def reply(self, path, *answers):
        self.replies[path] = list(answers)


@pytest.fixture
def fake():
    state = Fake()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            state.requests.append(("POST", self.path, dict(self.headers), body))
            answers = state.replies.get(self.path) or [(200, b"{}")]
            status, payload = answers.pop(0) if len(answers) > 1 else answers[0]
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    state.client = SiteClient(PosterSettings(base, "anon", base), TOKEN, timeout=5)
    yield state
    server.shutdown()


CLAIM = "/rest/v1/rpc/claim_poster_art_job"
RESULT = f"/api/poster-art/jobs/{JOB_ID}/result"
FAIL = f"/api/poster-art/jobs/{JOB_ID}/fail"


@pytest.mark.parametrize("w,h,expected", [(1024, 768, (1024, 768)), (1000, 750, (992, 736)),
                                          (2048, 1536, (1024, 768)), (17, 17, (16, 16))])
def test_fit_size(w, h, expected):
    assert poster_contract.fit_size(w, h) == expected


def test_fit_size_area_never_exceeds():
    for w in range(64, 4096, 97):
        for h in range(64, 4096, 131):
            fw, fh = poster_contract.fit_size(w, h)
            assert fw * fh <= 786_432 and fw % 16 == 0 and fh % 16 == 0 and fw >= 16 and fh >= 16
            assert fw <= w and fh <= h


def test_claim_sends_contract_headers(fake):
    fake.reply(CLAIM, (200, json.dumps([JOB]).encode()))
    job = fake.client.claim()
    assert job == Job(JOB_ID, "a robot", 1024, 768, "s3cret")
    method, path, headers, body = fake.requests[0]
    assert path == CLAIM
    assert headers["apikey"] == "anon" and headers["Authorization"] == "Bearer anon"
    assert json.loads(body) == {"p_token": TOKEN}


@pytest.mark.parametrize("payload", [b"[]", b"null"])
def test_claim_empty_and_null(fake, payload):
    fake.reply(CLAIM, (200, payload))
    assert fake.client.claim() is None


def test_claim_5xx_raises_transient(fake):
    fake.reply(CLAIM, (503, b"busy"))
    with pytest.raises(TransientError):
        fake.client.claim()


def test_claim_connection_refused_raises_transient():
    client = SiteClient(PosterSettings("http://127.0.0.1:9", "anon", "http://127.0.0.1:9"), TOKEN, timeout=2)
    with pytest.raises(TransientError) as error:
        client.claim()
    assert TOKEN not in str(error.value)


def test_ping_sends_token_and_never_raises(fake):
    fake.reply("/rest/v1/rpc/poster_art_ping", (500, b"x"))
    assert fake.client.ping() is False
    _, path, headers, body = fake.requests[0]
    assert headers["apikey"] == "anon" and json.loads(body) == {"p_token": TOKEN}
    dead = SiteClient(PosterSettings("http://127.0.0.1:9", "anon", "http://127.0.0.1:9"), TOKEN, timeout=2)
    assert dead.ping() is False


def test_parse_job_rejects_bad_fields():
    with pytest.raises(BadJob) as error:
        poster_contract.parse_job([dict(JOB, id="../../etc")])
    assert error.value.job_id is None
    for bad in ({"width": "1024"}, {"width": 0}, {"job_secret": None}, {"prompt": 5}, {"prompt": "x" * 5000}):
        with pytest.raises(BadJob) as error:
            poster_contract.parse_job([dict(JOB, **bad)])
        assert error.value.job_id == JOB_ID
    with pytest.raises(BadJob):
        poster_contract.parse_job({"not": "a list"})


def test_upload_has_content_length_and_headers(fake):
    jpeg = b"\xff\xd8" + b"x" * 1000
    assert poster_contract.upload_with_retries(fake.client, Job(**JOB), jpeg, deadline=1e12) == "done"
    _, path, headers, body = fake.requests[0]
    assert path == RESULT and body == jpeg
    assert headers["Content-Length"] == str(len(jpeg)) and "Transfer-Encoding" not in headers
    assert headers["Authorization"] == f"Bearer {TOKEN}" and headers["X-Job-Secret"] == "s3cret"
    assert headers["Content-Type"] == "image/jpeg"


@pytest.mark.parametrize("code", [404, 413, 415])
def test_upload_drop_codes_no_retry_no_fail(fake, code):
    fake.reply(RESULT, (code, b"{}"))
    sleeps = []
    outcome = poster_contract.upload_with_retries(fake.client, Job(**JOB), b"\xff\xd8", 1e12, sleep=sleeps.append)
    assert outcome == "dropped" and sleeps == []
    assert [p for _, p, _, _ in fake.requests] == [RESULT]


def test_upload_502_retries_5_then_15_then_fail(fake):
    fake.reply(RESULT, (502, b"{}"))
    sleeps = []
    outcome = poster_contract.upload_with_retries(fake.client, Job(**JOB), b"\xff\xd8", 1e12, sleep=sleeps.append)
    assert outcome == "failed" and sleeps == [5, 15]
    paths = [p for _, p, _, _ in fake.requests]
    assert paths == [RESULT, RESULT, RESULT, FAIL]
    assert json.loads(fake.requests[-1][3]) == {"reason": "upload failed"}


def test_upload_502_then_200_is_done(fake):
    fake.reply(RESULT, (502, b"{}"), (200, b"{}"))
    sleeps = []
    assert poster_contract.upload_with_retries(fake.client, Job(**JOB), b"\xff\xd8", 1e12,
                                               sleep=sleeps.append) == "done"
    assert sleeps == [5]


def test_upload_retry_past_deadline_goes_to_fail(fake):
    fake.reply(RESULT, (502, b"{}"))
    sleeps = []
    clock = iter([100.0, 100.0, 100.0])
    outcome = poster_contract.upload_with_retries(fake.client, Job(**JOB), b"\xff\xd8", deadline=103.0,
                                                  now=lambda: next(clock, 100.0), sleep=sleeps.append)
    assert outcome == "failed" and sleeps == []
    assert [p for _, p, _, _ in fake.requests] == [RESULT, FAIL]


def test_fail_reason_truncated_to_200(fake):
    fake.client.fail(JOB_ID, "x" * 500)
    assert json.loads(fake.requests[0][3]) == {"reason": "x" * 200}
    assert fake.requests[0][2]["Authorization"] == f"Bearer {TOKEN}"
