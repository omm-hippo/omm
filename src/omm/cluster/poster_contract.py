"""The poster worker's side of the site contract: claim, ping, result upload, fail.

Contract (not ours to change): DSHS repo
docs/superpowers/specs/2026-11-06-poster-mac-art-design.md §5-3 and §7. Jobs are
pulled from a Supabase RPC with the public anon key plus this Mac's token;
only the finished image goes to the site. Nothing here ever logs or raises
with the token or the prompt in the text.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import time
import uuid

from omm.cluster.poster_config import PosterSettings

MAX_AREA = 786_432
SIZE_MULTIPLE = 16
MAX_IMAGE_BYTES = 4 * 1024 * 1024
RETRY_DELAYS = (5, 15)
MAX_PROMPT_CHARS = 4000
_MAX_REASON = 200
_DROP = frozenset({404, 413, 415})


@dataclass(frozen=True)
class Job:
    id: str
    prompt: str
    width: int
    height: int
    job_secret: str


class TransientError(Exception):
    """The claim could not be answered now (network, 5xx, garbage); back off and ask again."""


class BadJob(Exception):
    def __init__(self, job_id: str | None):
        super().__init__("the site sent a job this worker cannot read")
        self.job_id = job_id


def fit_size(width: int, height: int) -> tuple[int, int]:
    """Scale down to MAX_AREA keeping the ratio, then floor each side to a multiple of 16."""
    if width * height > MAX_AREA:
        scale = math.sqrt(MAX_AREA / (width * height))
        width, height = width * scale, height * scale
    return (max(SIZE_MULTIPLE, int(width) // SIZE_MULTIPLE * SIZE_MULTIPLE),
            max(SIZE_MULTIPLE, int(height) // SIZE_MULTIPLE * SIZE_MULTIPLE))


def _positive_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value <= 10_000


def parse_job(body) -> Job | None:
    """The claim reply: `[]`/`null` means no job; otherwise one job, checked field by field."""
    if body is None or body == []:
        return None
    if not isinstance(body, list) or len(body) != 1 or not isinstance(body[0], dict):
        raise BadJob(None)
    row = body[0]
    try:
        job_id = str(uuid.UUID(str(row.get("id"))))
    except ValueError:
        raise BadJob(None) from None
    prompt, secret = row.get("prompt"), row.get("job_secret")
    if (not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT_CHARS
            or not isinstance(secret, str) or not secret
            or not _positive_int(row.get("width")) or not _positive_int(row.get("height"))):
        raise BadJob(job_id)
    return Job(job_id, prompt, row["width"], row["height"], secret)


class SiteClient:
    def __init__(self, settings: PosterSettings, token: str, *, session=None, timeout: float = 30):
        import requests

        self._requests = requests
        self._session = session or requests.Session()
        self._settings = settings
        self._token = token
        self._timeout = timeout

    def _rpc(self, name: str):
        return self._session.post(
            f"{self._settings.supabase_url}/rest/v1/rpc/{name}",
            headers={"apikey": self._settings.anon_key, "Authorization": f"Bearer {self._settings.anon_key}"},
            json={"p_token": self._token}, timeout=self._timeout)

    def _job_url(self, job_id: str, action: str) -> str:
        return f"{self._settings.site_url}/api/poster-art/jobs/{uuid.UUID(job_id)}/{action}"

    def claim(self) -> Job | None:
        try:
            response = self._rpc("claim_poster_art_job")
        except self._requests.RequestException as error:
            raise TransientError(f"claim failed: {type(error).__name__}") from None
        if response.status_code != 200:
            raise TransientError(f"claim answered HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError:
            raise TransientError("claim answered something that is not JSON") from None
        return parse_job(body)

    def ping(self) -> bool:
        try:
            return self._rpc("poster_art_ping").status_code == 200
        except self._requests.RequestException:
            return False

    def upload(self, job: Job, jpeg: bytes) -> int:
        """HTTP status of the result upload, 0 when it never got an answer."""
        try:
            # One bytes body, so requests sets Content-Length (a chunked upload gets 413).
            response = self._session.post(
                self._job_url(job.id, "result"), data=bytes(jpeg), timeout=self._timeout,
                headers={"Authorization": f"Bearer {self._token}", "X-Job-Secret": job.job_secret,
                         "Content-Type": "image/jpeg"})
        except self._requests.RequestException:
            return 0
        return response.status_code

    def fail(self, job_id: str, reason: str) -> None:
        try:
            self._session.post(self._job_url(job_id, "fail"), json={"reason": reason[:_MAX_REASON]},
                               headers={"Authorization": f"Bearer {self._token}"}, timeout=self._timeout)
        except (self._requests.RequestException, ValueError):
            pass


def upload_with_retries(client: SiteClient, job: Job, jpeg: bytes, deadline: float, *,
                        now=time.monotonic, sleep=time.sleep) -> str:
    """200 -> "done"; 404/413/415 -> "dropped"; anything else retried after 5 s, 15 s, then fail."""
    delays = list(RETRY_DELAYS)
    while True:
        status = client.upload(job, jpeg)
        if status == 200:
            return "done"
        if status in _DROP:
            return "dropped"
        if not delays or now() + delays[0] > deadline:
            client.fail(job.id, "upload failed")
            return "failed"
        sleep(delays.pop(0))
