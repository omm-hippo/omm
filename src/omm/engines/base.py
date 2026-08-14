"""Shared contracts and safe HTTP plumbing for local model runtimes."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Literal, Protocol, Sequence
from urllib.parse import urlsplit

FailureReason = Literal[
    "server_unavailable",
    "model_not_visible",
    "load_failed",
    "out_of_memory",
    "generation_timeout",
    "empty_response",
    "unload_failed",
    "unsupported_runtime",
    "unknown",
]

FAILURE_REASONS = frozenset(
    {
        "server_unavailable",
        "model_not_visible",
        "load_failed",
        "out_of_memory",
        "generation_timeout",
        "empty_response",
        "unload_failed",
        "unsupported_runtime",
        "unknown",
    }
)


class RuntimeAdapterError(RuntimeError):
    """A local-runtime failure safe to classify without logging its body."""

    def __init__(
        self,
        reason: FailureReason,
        message: str,
        *,
        transport_kind: str | None = None,
    ) -> None:
        if reason not in FAILURE_REASONS:
            reason = "unknown"
        super().__init__(message)
        self.reason: FailureReason = reason
        self.transport_kind = transport_kind


@dataclass(frozen=True)
class RuntimeHealth:
    reachable: bool
    version: str | None = None
    failure_reason: FailureReason | None = None


@dataclass(frozen=True)
class RuntimeModel:
    key: str
    display_name: str
    loaded: bool
    instance_id: str | None = None
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class RuntimeModelRef:
    key: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class LoadOptions:
    context_length: int = 1024

    def __post_init__(self) -> None:
        if (
            isinstance(self.context_length, bool)
            or not isinstance(self.context_length, int)
            or not 256 <= self.context_length <= 131_072
        ):
            raise ValueError("context_length must be an integer from 256 to 131072")


@dataclass(frozen=True)
class LoadReceipt:
    model: RuntimeModel
    instance_id: str
    was_already_loaded: bool
    loaded_by_omm: bool


@dataclass(frozen=True)
class ProbeRequest:
    prompt: str = "Reply with the single word OK."
    max_output_tokens: int = 8
    timeout_seconds: int = 60

    def __post_init__(self) -> None:
        if not isinstance(self.prompt, str) or not self.prompt.strip() or len(self.prompt) > 1000:
            raise ValueError("probe prompt must contain 1 to 1000 characters")
        if (
            isinstance(self.max_output_tokens, bool)
            or not isinstance(self.max_output_tokens, int)
            or not 1 <= self.max_output_tokens <= 64
        ):
            raise ValueError("max_output_tokens must be an integer from 1 to 64")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, int)
            or not 1 <= self.timeout_seconds <= 300
        ):
            raise ValueError("timeout_seconds must be an integer from 1 to 300")


@dataclass(frozen=True)
class ProbeResult:
    text: str


@dataclass(frozen=True)
class UnloadResult:
    unloaded: bool
    failure_reason: FailureReason | None = None


class RuntimeAdapter(Protocol):
    key: str

    def health(self) -> RuntimeHealth: ...

    def list_models(self) -> list[RuntimeModel]: ...

    def load(self, model: RuntimeModelRef, options: LoadOptions) -> LoadReceipt: ...

    def generate(self, receipt: LoadReceipt, request: ProbeRequest) -> ProbeResult: ...

    def unload(self, receipt: LoadReceipt) -> UnloadResult: ...


def _normalized_model_key(value: str) -> str:
    return value.strip().replace("\\", "/").casefold()


def find_runtime_model(
    models: Sequence[RuntimeModel], reference: RuntimeModelRef
) -> RuntimeModel | None:
    """Match exact runtime identifiers and bounded aliases, never substrings."""
    expected = {
        _normalized_model_key(value)
        for value in (reference.key, *reference.aliases)
        if isinstance(value, str) and value.strip()
    }
    matches = []
    for model in models:
        available = {
            _normalized_model_key(value)
            for value in (model.key, model.display_name, *model.aliases)
            if isinstance(value, str) and value.strip()
        }
        if expected & available:
            matches.append(model)
    return matches[0] if len(matches) == 1 else None


def require_loopback_base_url(base_url: str) -> str:
    """Accept only an explicit HTTP(S) loopback origin with no URL path."""
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("runtime endpoint must be an HTTP(S) loopback origin")
    hostname = parsed.hostname.casefold()
    is_loopback = hostname == "localhost"
    if not is_loopback:
        try:
            is_loopback = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            is_loopback = False
    if (
        not is_loopback
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("runtime endpoint must be a loopback origin without credentials or a path")
    return base_url.rstrip("/")


@dataclass(frozen=True)
class JsonResponse:
    data: dict
    headers: dict[str, str]


class LoopbackJsonClient:
    """Small JSON client that never includes response bodies in exceptions."""

    def __init__(self, base_url: str, *, token: str | None = None) -> None:
        self.base_url = require_loopback_base_url(base_url)
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}

    @staticmethod
    def _response_message(response) -> str:
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            for key in ("error", "message", "detail"):
                value = payload.get(key)
                if isinstance(value, str):
                    return value[:2000].casefold()
        return ""

    @staticmethod
    def _classify(message: str, default: FailureReason) -> FailureReason:
        if any(marker in message for marker in (
            "out of memory",
            "not enough memory",
            "insufficient memory",
            "requires more system memory",
        )):
            return "out_of_memory"
        if any(marker in message for marker in ("unsupported", "not supported", "does not support")):
            return "unsupported_runtime"
        if any(marker in message for marker in ("model not found", "unknown model", "not installed")):
            return "model_not_visible"
        if any(marker in message for marker in ("failed to load", "unable to load", "could not load")):
            return "load_failed"
        return default

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict | None = None,
        timeout: int | float = 10,
        default_failure: FailureReason = "unknown",
        timeout_failure: FailureReason | None = None,
    ) -> JsonResponse:
        import requests

        try:
            with requests.Session() as session:
                # These endpoints are intentionally local-only. Ignoring proxy
                # environment variables also prevents an LM Studio token from
                # being forwarded to a configured HTTP proxy.
                session.trust_env = False
                response = session.request(
                    method,
                    f"{self.base_url}{path}",
                    json=payload,
                    headers=dict(self._headers),
                    timeout=timeout,
                )
        except (requests.exceptions.ConnectTimeout, requests.exceptions.ConnectionError) as error:
            transport_kind = (
                "connect_timeout"
                if isinstance(error, requests.exceptions.ConnectTimeout)
                else "connection_error"
            )
            raise RuntimeAdapterError(
                "server_unavailable",
                "the local runtime server is unavailable",
                transport_kind=transport_kind,
            ) from error
        except requests.exceptions.Timeout as error:
            raise RuntimeAdapterError(
                timeout_failure or default_failure,
                "the local runtime request timed out",
                transport_kind="read_timeout",
            ) from error
        except requests.RequestException as error:
            raise RuntimeAdapterError(default_failure, "the local runtime request failed") from error

        if not response.ok:
            message = self._response_message(response)
            reason = self._classify(message, default_failure)
            if response.status_code in {401, 403}:
                reason = "unknown"
            if response.status_code == 404 and default_failure == "unsupported_runtime":
                reason = "unsupported_runtime"
            safe_message = (
                "the local runtime rejected authentication"
                if response.status_code in {401, 403}
                else f"the local runtime returned HTTP {response.status_code}"
            )
            raise RuntimeAdapterError(reason, safe_message)
        try:
            data = response.json()
        except ValueError as error:
            raise RuntimeAdapterError(default_failure, "the local runtime returned invalid JSON") from error
        if not isinstance(data, dict):
            raise RuntimeAdapterError(default_failure, "the local runtime returned invalid data")
        headers = getattr(response, "headers", {})
        return JsonResponse(
            data,
            {str(key).casefold(): str(value) for key, value in headers.items()},
        )
