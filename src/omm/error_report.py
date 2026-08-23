"""Strictly opt-in, best-effort error reports. Never raises.

`omm contribute` runs unattended on machines the maintainers cannot see, so
a failure that only ever reaches the local console is effectively invisible.
This module collects a deliberately small, scrubbed description of such a
failure and - only with explicit consent - posts it to a *separate*,
write-only Realtime Database channel (`/error_reports`), never the
publicly-readable `/telemetry` node that benchmark rows use.

The structure mirrors `omm.telemetry` on purpose (pending queue, atomic
locked writes, an attempt log, `flush_pending` on a later invocation) so
there is one queueing pattern to reason about. The differences are
deliberate:

* the send policy is its own config field, `error_report_send_policy`, and
  an unset policy means **never** - this feature is opt-in, while telemetry
  defaults to asking;
* the endpoint is derived from `telemetry_endpoint` rather than configured
  separately, so there is no second URL to keep in sync (and no endpoint
  means the feature is simply off);
* the payload is an allow-list of fields (see `docs/error-reports.md`).
  Tracebacks, absolute paths, usernames, environment variables, the command
  line, and generated model text are never part of it.
"""

from __future__ import annotations

import json
import platform
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse, urlunparse

from filelock import Timeout as FileLockTimeout

from omm import config
from omm.atomic import atomic_write_text, locked
from omm.config import load_config
from omm import package_metadata
from omm.telemetry import secure_endpoint

SCHEMA_VERSION = 1

#: The only values the config field may hold. Anything else - including the
#: `None` written by `DEFAULT_CONFIG` for a user who never made a choice -
#: is treated as "not configured", which behaves as `never`.
POLICIES = ("ask", "always", "never")

#: Identifiers for the three places a report can originate from. Kept in
#: sync with the `trigger` validation in `database.rules.json`.
TRIGGERS = ("install_quality_eval", "daemon_restart_giveup", "crash")

_MAX_LOG_LINES = 500
_MAX_PENDING_REPORTS = 200
_DEFAULT_MAX_RETRIES_PER_FLUSH = 3
_MAX_MESSAGE_LENGTH = 2000
_MAX_TYPE_LENGTH = 200
_MAX_CATALOG_REF_LENGTH = 620

_TELEMETRY_SEGMENTS = {"telemetry.json": "error_reports.json", "telemetry": "error_reports"}

# One-run consent (`omm contribute --report-errors`, or answering "y" to the
# `ask` prompt). Never persisted: it authorizes this process, not a future
# unattended one. Tri-state, because "not asked yet" and "asked and
# declined" must behave differently - a decline suppresses queueing for the
# rest of the run, while an unanswered `always` policy still queues.
_run_consent: bool | None = None


def set_run_consent(allowed: bool | None) -> None:
    """Record a one-shot decision for the current process only."""
    global _run_consent
    _run_consent = allowed


def run_consent() -> bool | None:
    return _run_consent


def read_config() -> dict[str, Any]:
    """Read the config without creating `~/.omm` as a side effect.

    The crash hook runs on every command, including on a machine that has
    never run omm before; materializing a home directory (and a log file)
    for a feature nobody turned on would be the wrong trade.
    """
    if not config.CONFIG_PATH.exists():
        return {}
    try:
        return load_config()
    except Exception:
        return {}


def send_policy(config_data: dict[str, Any] | None = None) -> str:
    """The effective policy. An unset or unrecognized value means `never`:
    error reports are opt-in, so anything other than a deliberate choice
    must not send."""
    value = (read_config() if config_data is None else config_data).get(
        "error_report_send_policy"
    )
    return value if value in POLICIES else "never"


def policy_is_set(config_data: dict[str, Any] | None = None) -> bool:
    """True once the user has actually chosen a policy.

    This distinguishes "never touched it" (which behaves as `never`, but a
    one-run `--report-errors` may still opt in for a single run) from an
    explicit `omm setting error-reports --disable`, which a runtime flag
    must not be able to override.
    """
    value = (read_config() if config_data is None else config_data).get(
        "error_report_send_policy"
    )
    return value in POLICIES


def endpoint(config_data: dict[str, Any] | None = None) -> str | None:
    """Derive the error-report endpoint from `telemetry_endpoint`.

    Only the last path segment is rewritten (`telemetry.json` ->
    `error_reports.json`), so the two channels always live on the same
    host and no second URL can drift out of sync. A telemetry endpoint
    that does not end in that segment yields `None` rather than a guessed
    path - posting error data to an address nobody configured is worse
    than not reporting at all. `TELEMETRY_GATEWAY_ENDPOINT` is a special
    case, not a segment rewrite - see the constant's docstring.
    """
    value = (read_config() if config_data is None else config_data).get("telemetry_endpoint")
    if not isinstance(value, str) or not secure_endpoint(value):
        return None
    if value == config.TELEMETRY_GATEWAY_ENDPOINT:
        # The gateway only speaks the PoW-wrapped telemetry protocol, not
        # raw Firebase writes - error_reports must keep going straight to
        # Firebase, so it can't be derived by rewriting this URL's path.
        return config.ERROR_REPORTS_ENDPOINT
    try:
        parsed = urlparse(value)
    except ValueError:
        return None
    head, separator, last = parsed.path.rpartition("/")
    replacement = _TELEMETRY_SEGMENTS.get(last)
    if replacement is None:
        return None
    derived = urlunparse(parsed._replace(path=f"{head}{separator}{replacement}"))
    return derived if secure_endpoint(derived) else None


def enabled(config_data: dict[str, Any] | None = None) -> bool:
    """Whether error reporting can work at all on this machine."""
    return endpoint(config_data) is not None


# `/Users/<name>`, `/home/<name>`, and `C:\Users\<name>` are the three shapes
# that carry a username in practice. The trailing lookahead keeps the match
# anchored on the user directory itself so `/home/models` (a path with no
# user component) is left alone.
_POSIX_HOME_RE = re.compile(r"(?<![\w./])/(?:Users|home)/[^/\\\s:'\"]+")
_WINDOWS_HOME_RE = re.compile(
    r"(?<![\w\\/])[A-Za-z]:\\Users\\[^\\/\s:'\"]+",
    re.IGNORECASE,
)


def scrub_paths(text: str) -> str:
    """Replace per-user home directories with `~` so a message can name a
    file without naming its owner.

    Regex-only by design: this is a targeted username/home-prefix scrubber,
    not general-purpose PII detection (see `docs/error-reports.md`).
    """
    if not isinstance(text, str) or not text:
        return ""
    scrubbed = _WINDOWS_HOME_RE.sub(r"~", text)
    return _POSIX_HOME_RE.sub("~", scrubbed)


def _log_path():
    return config.OMM_HOME / "error_reports.log"


def _pending_path():
    return config.OMM_HOME / "error_reports_pending.json"


def log_attempt(outcome: str, detail: str = "") -> None:
    """Local, never-uploaded record of every decision this module makes, so
    "why did/didn't a report go out" is answerable after the fact."""
    try:
        path = _log_path()
        with locked(path, timeout=30):
            lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
            lines.append(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                "outcome": outcome,
                "detail": scrub_paths(detail),
            }))
            atomic_write_text(path, "\n".join(lines[-_MAX_LOG_LINES:]) + "\n")
    except (OSError, FileLockTimeout):
        pass


def _read_pending_unlocked(path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(loaded, list):
        return []
    return [report for report in loaded if isinstance(report, dict)][-_MAX_PENDING_REPORTS:]


def _load_pending() -> list[dict[str, Any]]:
    path = _pending_path()
    try:
        with locked(path, timeout=30):
            return _read_pending_unlocked(path)
    except (OSError, FileLockTimeout):
        return []


def _save_pending(reports: list[dict[str, Any]]) -> None:
    try:
        path = _pending_path()
        with locked(path, timeout=30):
            atomic_write_text(path, json.dumps(reports[-_MAX_PENDING_REPORTS:]))
    except (OSError, FileLockTimeout):
        pass


def _append_pending(report: dict[str, Any]) -> None:
    """Append without losing reports written by another omm process."""
    try:
        path = _pending_path()
        with locked(path, timeout=30):
            reports = _read_pending_unlocked(path)
            reports.append(report)
            atomic_write_text(path, json.dumps(reports[-_MAX_PENDING_REPORTS:]))
    except (OSError, FileLockTimeout):
        pass


def discard_pending() -> int:
    """Drop everything queued and report how many were dropped.

    Called when the user opts out: a queue built up under an earlier
    consent must not sit on disk waiting for a policy that will never come.
    """
    reports = _load_pending()
    if reports:
        _save_pending([])
    return len(reports)


def pending_count() -> int:
    return len(_load_pending())


def catalog_ref(repo_id: str | None, filename: str | None) -> str | None:
    """`repo_id:filename` - the catalog coordinates of a model, never the
    local absolute path it was downloaded to."""
    name = filename if isinstance(filename, str) and filename.strip() else None
    if name is None:
        return None
    repo = repo_id if isinstance(repo_id, str) and repo_id.strip() else None
    ref = f"{repo.strip()}:{name.strip()}" if repo else name.strip()
    return ref[:_MAX_CATALOG_REF_LENGTH]


def _client_version() -> str | None:
    """Read the installed version without importing `omm.cli` (which imports
    this module)."""
    try:
        return package_metadata.version()
    except Exception:
        return None


def _hardware_scores() -> dict[str, float]:
    """The same anonymous chip scores v8 telemetry already uploads, computed
    by the same parser (`omm.featurize.parse_chip_score`). Raw CPU/GPU name
    strings are never part of the result.

    Only a scan the failing command already performed is used
    (`hardware.last_scan()`): a full scan shells out to platform tools and
    takes seconds, and making a user wait for that after a crash - before
    they even see their traceback - is a bad trade for a secondary field.
    Every one of these keys is optional in the payload for that reason.
    """
    try:
        from omm.featurize import parse_chip_score
        from omm.hardware import last_scan

        info = last_scan()
    except Exception:
        return {}
    if info is None:
        return {}
    scores: dict[str, float] = {}
    cpu_name = getattr(info, "cpu", None)
    if isinstance(cpu_name, str) and cpu_name.strip():
        cpu_score, cpu_tier = parse_chip_score(cpu_name)
        scores["cpu_score"], scores["cpu_tier"] = cpu_score, cpu_tier
    gpu_name = getattr(info, "gpu_name", None)
    if isinstance(gpu_name, str) and gpu_name.strip():
        gpu_score, gpu_tier = parse_chip_score(gpu_name)
        scores["gpu_score"], scores["gpu_tier"] = gpu_score, gpu_tier
    arch = getattr(info, "cpu_arch", None)
    if isinstance(arch, str) and arch.strip():
        scores["cpu_arch"] = arch.strip()[:64]
    return scores


def build_report(
    error: BaseException | None = None,
    *,
    trigger: str,
    error_type: str | None = None,
    message: str | None = None,
    subcommand: str | None = None,
    catalog_ref: str | None = None,
    engine: str | None = None,
) -> dict[str, Any]:
    """Assemble the exact dict that would be uploaded.

    Every string that could contain a home directory goes through
    `scrub_paths` here, once, so no caller can forget to do it.
    """
    resolved_type = error_type or (type(error).__name__ if error is not None else "UnknownError")
    resolved_message = message if message is not None else (str(error) if error else "")
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "error_type": scrub_paths(resolved_type)[:_MAX_TYPE_LENGTH] or "UnknownError",
        "error_message": scrub_paths(resolved_message)[:_MAX_MESSAGE_LENGTH],
        "trigger": trigger,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "os_name": (platform.system() or "unknown")[:128],
        "os_version": (platform.release() or "")[:128],
    }
    version = _client_version()
    if version:
        report["client_version"] = version[:100]
    if subcommand:
        report["subcommand"] = subcommand[:64]
    if catalog_ref:
        report["catalog_ref"] = scrub_paths(catalog_ref)[:_MAX_CATALOG_REF_LENGTH]
    if engine:
        report["engine"] = engine[:32]
    report.update(_hardware_scores())
    return report


def preview_text(report: dict[str, Any]) -> str:
    """Pretty-printed payload for the consent prompt - what the user is
    asked about is literally what would be sent."""
    return json.dumps(report, indent=2, sort_keys=True)


def preview_report() -> tuple[dict[str, Any], bool]:
    """The payload to show at the consent prompt, and whether it is only an
    example. A queued report is preferred - then the preview is not a
    representative sample but the literal next upload."""
    pending = _load_pending()
    if pending:
        return pending[0], False
    return example_report(), True


def example_report() -> dict[str, Any]:
    """A representative payload used by the consent preview when nothing is
    queued yet, so the user still sees real hardware/version values rather
    than a hand-written sample."""
    try:
        from omm import hardware

        if hardware.last_scan() is None:
            # Only here, at an interactive prompt, is a fresh scan worth its
            # cost: a preview that omits the hardware fields would understate
            # what the run is about to send.
            hardware.scan_hardware()
    except Exception:
        pass
    return build_report(
        trigger="install_quality_eval",
        error_type="QualityEvaluationError",
        message="Ollama /api/generate request failed",
        catalog_ref="unsloth/Qwen3-4B-GGUF:Qwen3-4B-Q4_K_M.gguf",
        engine="ollama",
    )


def queue_report(
    error: BaseException | None = None,
    *,
    trigger: str,
    error_type: str | None = None,
    message: str | None = None,
    subcommand: str | None = None,
    catalog_ref: str | None = None,
    engine: str | None = None,
) -> dict[str, Any] | None:
    """Append a report to the pending queue, or return None without building
    one at all.

    Never performs I/O over the network and never prompts: callers sit on
    hot paths (an unattended contribute loop, the crash handler) where a
    blocking send or a question would be actively harmful.
    """
    try:
        consent = run_consent()
        config_data = read_config()
        # An unset policy still behaves as `never`, except when this run
        # carries a one-shot `--report-errors`. An explicitly stored `never`
        # is never overridable that way.
        opted_in = consent is not False and (
            send_policy(config_data) != "never"
            or (consent and not policy_is_set(config_data))
        )
        if not opted_in:
            # Deliberately silent, including in the local log: someone who
            # never turned this on should not accumulate a log file about a
            # feature that is doing nothing.
            if policy_is_set(config_data):
                log_attempt("skipped_opt_out", trigger)
            return None
        if not enabled(config_data):
            log_attempt("skipped_no_endpoint", trigger)
            return None
        report = build_report(
            error,
            trigger=trigger,
            error_type=error_type,
            message=message,
            subcommand=subcommand,
            catalog_ref=catalog_ref,
            engine=engine,
        )
        _append_pending(report)
        log_attempt("queued", trigger)
        return report
    except Exception:
        # Reporting an error must never itself become one.
        return None


def _post_report(report: dict[str, Any], config_data: dict[str, Any] | None = None) -> bool:
    """POST one report. Returns True on 2xx, False on anything else."""
    import requests

    target = endpoint(config_data)
    if target is None:
        log_attempt("skipped_no_endpoint")
        return False

    params = {}
    if "firebaseio.com" in target:
        from omm import firebase_auth

        id_token = firebase_auth.get_id_token()
        # The error_reports node accepts unauthenticated writes, so a missing
        # token is not fatal here (unlike telemetry). Attach one when we have
        # it and carry on when we don't.
        if id_token is not None:
            params["auth"] = id_token

    wire = {key: value for key, value in report.items() if value is not None}
    try:
        resp = requests.post(target, params=params, json=wire, timeout=5)
    except requests.RequestException as e:
        log_attempt("send_failed_network", str(e)[:300])
        return False
    if not (200 <= resp.status_code < 300):
        detail = " ".join(str(getattr(resp, "text", "") or "").split())[:300]
        log_attempt(f"send_failed_http_{resp.status_code}", detail)
        return False
    log_attempt("sent_ok")
    return True


def send_report(report: dict[str, Any], force: bool = False) -> bool:
    """Send one report immediately when policy (or one-run consent) allows."""
    config_data = read_config()
    if not force and send_policy(config_data) != "always" and not run_consent():
        log_attempt("skipped_opt_out")
        return False
    return _post_report(report, config_data)


def flush_pending(max_retries: int = _DEFAULT_MAX_RETRIES_PER_FLUSH, force: bool = False) -> int:
    """Best-effort send of everything queued earlier. Returns how many went out.

    Consent is re-checked here rather than trusted from queue time, so a
    later `omm setting error-reports --disable` also stops a backlog that
    was queued while reporting was still on. `always` flushes unattended;
    `ask` waits for `force` (the next `omm contribute`, once the user has
    answered the prompt) so a crash is never followed by an immediate
    upload the user never approved.
    """
    config_data = read_config()
    if not force and send_policy(config_data) != "always":
        return 0
    if force and send_policy(config_data) == "never" and policy_is_set(config_data):
        return 0
    path = _pending_path()
    try:
        # Serialize the whole bounded batch so a concurrent writer cannot be
        # erased by this read/modify/write cycle.
        with locked(path, timeout=30):
            reports = _read_pending_unlocked(path)
            if not reports:
                return 0
            to_retry, still_pending = reports[:max_retries], reports[max_retries:]
            sent = 0
            for report in to_retry:
                if _post_report(report, config_data):
                    sent += 1
                else:
                    still_pending.append(report)
            atomic_write_text(path, json.dumps(still_pending[-_MAX_PENDING_REPORTS:]))
            return sent
    except (OSError, FileLockTimeout):
        return 0
