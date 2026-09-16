"""Environment-first credentials backed only by native OS secret stores."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
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


def _run_secret_command(args: list[str], *, token: str | None = None):
    """Run a native secret-store CLI without putting a token in argv."""
    try:
        return subprocess.run(
            args,
            input=(f"{token}\n" if token is not None else None),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


class _MacOSKeychainBackend:
    name = "macOS Keychain"

    def get_password(self, service: str, provider: str) -> str | None:
        result = _run_secret_command(
            ["/usr/bin/security", "find-generic-password", "-a", provider, "-s", service, "-w"]
        )
        return result.stdout.rstrip("\r\n") if result is not None and result.returncode == 0 else None

    def set_password(self, service: str, provider: str, token: str) -> None:
        # `security help add-generic-password`: a final `-w` with no argv
        # value prompts on stdin. This avoids exposing the token in `ps`.
        result = _run_secret_command(
            [
                "/usr/bin/security", "add-generic-password", "-a", provider,
                "-s", service, "-U", "-w",
            ],
            token=token,
        )
        if result is None or result.returncode != 0:
            raise CredentialError("macOS Keychain could not save this credential")

    def delete_password(self, service: str, provider: str) -> None:
        result = _run_secret_command(
            ["/usr/bin/security", "delete-generic-password", "-a", provider, "-s", service]
        )
        if result is None:
            raise CredentialError("macOS Keychain could not delete this credential")
        if result.returncode != 0 and self.get_password(service, provider) is not None:
            raise CredentialError("macOS Keychain did not delete this credential")


class _LinuxSecretServiceBackend:
    name = "Linux Secret Service"

    def __init__(self, executable: str) -> None:
        self.executable = executable

    def get_password(self, service: str, provider: str) -> str | None:
        result = _run_secret_command(
            [self.executable, "lookup", "service", service, "provider", provider]
        )
        return result.stdout.rstrip("\r\n") if result is not None and result.returncode == 0 else None

    def set_password(self, service: str, provider: str, token: str) -> None:
        result = _run_secret_command(
            [
                self.executable, "store", "--label", f"OMM {provider}",
                "service", service, "provider", provider,
            ],
            token=token,
        )
        if result is None or result.returncode != 0:
            raise CredentialError("Linux Secret Service could not save this credential")

    def delete_password(self, service: str, provider: str) -> None:
        result = _run_secret_command(
            [self.executable, "clear", "service", service, "provider", provider]
        )
        if result is None:
            raise CredentialError("Linux Secret Service could not delete this credential")
        if result.returncode != 0 and self.get_password(service, provider) is not None:
            raise CredentialError("Linux Secret Service did not delete this credential")


class _WindowsCredentialBackend:
    name = "Windows Credential Manager"
    _CRED_TYPE_GENERIC = 1
    _CRED_PERSIST_LOCAL_MACHINE = 2
    _ERROR_NOT_FOUND = 1168

    @staticmethod
    def _target(service: str, provider: str) -> str:
        return f"{service}:{provider}"

    @staticmethod
    def _api():
        import ctypes
        from ctypes import wintypes

        class Credential(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD),
                ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR),
                ("LastWritten", wintypes.FILETIME),
                ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD),
                ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR),
                ("UserName", wintypes.LPWSTR),
            ]

        advapi = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        advapi.CredWriteW.argtypes = [ctypes.POINTER(Credential), wintypes.DWORD]
        advapi.CredWriteW.restype = wintypes.BOOL
        advapi.CredReadW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(Credential)),
        ]
        advapi.CredReadW.restype = wintypes.BOOL
        advapi.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        advapi.CredDeleteW.restype = wintypes.BOOL
        advapi.CredFree.argtypes = [ctypes.c_void_p]
        advapi.CredFree.restype = None
        return ctypes, Credential, advapi

    def get_password(self, service: str, provider: str) -> str | None:
        ctypes, Credential, advapi = self._api()
        pointer = ctypes.POINTER(Credential)()
        if not advapi.CredReadW(
            self._target(service, provider), self._CRED_TYPE_GENERIC, 0,
            ctypes.byref(pointer),
        ):
            error = ctypes.get_last_error()
            if error == self._ERROR_NOT_FOUND:
                return None
            raise CredentialError("Windows Credential Manager could not read this credential")
        try:
            credential = pointer.contents
            raw = ctypes.string_at(
                credential.CredentialBlob, credential.CredentialBlobSize
            )
            return raw.decode("utf-16-le")
        finally:
            advapi.CredFree(pointer)

    def set_password(self, service: str, provider: str, token: str) -> None:
        ctypes, Credential, advapi = self._api()
        raw = token.encode("utf-16-le")
        blob = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
        credential = Credential()
        credential.Type = self._CRED_TYPE_GENERIC
        credential.TargetName = self._target(service, provider)
        credential.CredentialBlobSize = len(raw)
        credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
        credential.Persist = self._CRED_PERSIST_LOCAL_MACHINE
        credential.UserName = provider
        if not advapi.CredWriteW(ctypes.byref(credential), 0):
            raise CredentialError("Windows Credential Manager could not save this credential")

    def delete_password(self, service: str, provider: str) -> None:
        ctypes, _Credential, advapi = self._api()
        if advapi.CredDeleteW(
            self._target(service, provider), self._CRED_TYPE_GENERIC, 0
        ):
            return
        error = ctypes.get_last_error()
        if error != self._ERROR_NOT_FOUND:
            raise CredentialError("Windows Credential Manager could not delete this credential")


def _native_backend():
    system = platform.system()
    if system == "Darwin" and os.path.isfile("/usr/bin/security"):
        return _MacOSKeychainBackend()
    if system == "Windows":
        try:
            return _WindowsCredentialBackend()
        except Exception:
            return None
    if system == "Linux" and (executable := shutil.which("secret-tool")):
        return _LinuxSecretServiceBackend(executable)
    return None


def secure_store_name() -> str | None:
    backend = _native_backend()
    return getattr(backend, "name", None) if backend is not None else None


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
            before = _valid_token(backend.get_password(SERVICE, provider))
        except Exception as error:
            raise CredentialError(
                "the native secret store could not read this credential for deletion"
            ) from error
        if before is not None:
            try:
                backend.delete_password(SERVICE, provider)
            except Exception as error:
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
            if _valid_token(backend.get_password(SERVICE, provider)) is not None:
                raise CredentialError("the native secret store did not delete this credential")
            removed = True
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
