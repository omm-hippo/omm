"""Environment-first credentials backed only by native OS secret stores."""

from __future__ import annotations

import os
import platform
from urllib.parse import urlsplit

SERVICE = "omm-model"
PROVIDERS = ("huggingface", "lmstudio")
ENVIRONMENT_VARIABLES = {
    "huggingface": "HF_TOKEN",
    "lmstudio": "LM_API_TOKEN",
}
_SESSION_TOKENS: dict[str, str] = {}


class CredentialError(RuntimeError):
    pass


def normalize_provider(provider: str) -> str:
    aliases = {"hf": "huggingface", "hugging-face": "huggingface", "lm-studio": "lmstudio"}
    normalized = aliases.get(provider.strip().casefold(), provider.strip().casefold())
    if normalized not in PROVIDERS:
        raise CredentialError(f"provider must be one of: {', '.join(PROVIDERS)}")
    return normalized


def _valid_token(token: object) -> str | None:
    if not isinstance(token, str):
        return None
    value = token.strip()
    if not value or len(value) > 4096 or any(character in value for character in "\r\n\0"):
        return None
    return value


def _native_backend():
    """Return a positively identified native backend, never a file fallback."""
    try:
        import keyring

        backend = keyring.get_keyring()
    except Exception:
        return None
    module = type(backend).__module__
    accepted = {
        "Darwin": "keyring.backends.macOS",
        "Windows": "keyring.backends.Windows",
        "Linux": "keyring.backends.SecretService",
    }.get(platform.system())
    if accepted is None or not module.startswith(accepted):
        return None
    try:
        if float(getattr(backend, "priority", 0)) <= 0:
            return None
    except (TypeError, ValueError):
        return None
    return backend


def secure_store_name() -> str | None:
    if _native_backend() is None:
        return None
    return {
        "Darwin": "macOS Keychain",
        "Windows": "Windows Credential Manager",
        "Linux": "Linux Secret Service",
    }.get(platform.system())


def _stored_token(provider: str) -> str | None:
    backend = _native_backend()
    if backend is None:
        return None
    try:
        return _valid_token(backend.get_password(SERVICE, provider))
    except Exception:
        return None


def token_source(provider: str) -> str:
    provider = normalize_provider(provider)
    if _valid_token(os.environ.get(ENVIRONMENT_VARIABLES[provider])):
        return "environment"
    if _stored_token(provider):
        return "secure-store"
    if _valid_token(_SESSION_TOKENS.get(provider)):
        return "session"
    return "none"


def token_for(provider: str) -> str | None:
    provider = normalize_provider(provider)
    environment = _valid_token(os.environ.get(ENVIRONMENT_VARIABLES[provider]))
    if environment:
        return environment
    stored = _stored_token(provider)
    if stored:
        return stored
    return _valid_token(_SESSION_TOKENS.get(provider))


def store_token(provider: str, token: str) -> dict[str, object]:
    provider = normalize_provider(provider)
    value = _valid_token(token)
    if value is None:
        raise CredentialError("token is empty or contains unsupported control characters")
    backend = _native_backend()
    if backend is None:
        _SESSION_TOKENS[provider] = value
        return {"provider": provider, "stored": False, "source": "session"}
    try:
        backend.set_password(SERVICE, provider, value)
    except Exception:
        # A backend can be installed but unavailable in this login session
        # (for example Linux without an unlocked Secret Service). Fail closed
        # to a process-only token, never to a plaintext file backend.
        _SESSION_TOKENS[provider] = value
        return {"provider": provider, "stored": False, "source": "session"}
    _SESSION_TOKENS.pop(provider, None)
    return {"provider": provider, "stored": True, "source": "secure-store"}


def logout(provider: str) -> dict[str, object]:
    provider = normalize_provider(provider)
    _SESSION_TOKENS.pop(provider, None)
    removed = False
    backend = _native_backend()
    if backend is not None:
        try:
            backend.delete_password(SERVICE, provider)
            removed = True
        except Exception as error:
            # Distinguish "nothing was stored" from a locked/broken store.
            # Reporting the latter as a successful logout would leave a
            # credential behind while telling the user it was gone.
            try:
                still_present = _valid_token(
                    backend.get_password(SERVICE, provider)
                ) is not None
            except Exception as read_error:
                raise CredentialError(
                    "the native secret store could not confirm credential deletion"
                ) from read_error
            if still_present:
                raise CredentialError(
                    "the native secret store did not delete this credential"
                ) from error
            removed = False
    return {
        "provider": provider,
        "removed": removed,
        "environment_active": bool(_valid_token(os.environ.get(ENVIRONMENT_VARIABLES[provider]))),
    }


def status(provider: str) -> dict[str, object]:
    provider = normalize_provider(provider)
    source = token_source(provider)
    return {
        "provider": provider,
        "authenticated": source != "none",
        "source": source,
        "environment_variable": ENVIRONMENT_VARIABLES[provider],
        "secure_store": secure_store_name(),
    }


def huggingface_headers() -> dict[str, str]:
    token = token_for("huggingface")
    return {"Authorization": f"Bearer {token}"} if token else {}


def headers_for_url(url: str) -> dict[str, str]:
    try:
        host = (urlsplit(url).hostname or "").casefold()
    except ValueError:
        return {}
    if host == "huggingface.co" or host.endswith(".huggingface.co") or host == "hf.co":
        return huggingface_headers()
    return {}
