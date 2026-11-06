"""Settings for the poster art worker: three public values and one secret token.

The site (dshs.dev) hands each Mac SUPABASE_URL, SUPABASE_ANON_KEY (public),
SITE_URL and its own TOKEN. The three public values live in
OMM_HOME/cluster/poster/settings.json; the token lives only in the login
keychain. It goes to `security` on stdin, never on argv (argv shows in `ps`),
and any error text from `security` has it scrubbed out before it is shown.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit

from omm import config
from omm.cluster.errors import ClusterError

KEYCHAIN_SERVICE = "omm-cluster-poster"
KEYCHAIN_ACCOUNT = "worker-token"
SECURITY = "/usr/bin/security"

# FLUX.2-klein-4B is Apache-2.0 and ungated; FLUX.1-schnell needs an HF login
# on every Mac. Measured on Node1 (GPU busy): 30 s per 1024x768 picture, 13.8 GB peak.
MODEL_REPO = "black-forest-labs/FLUX.2-klein-4B"
MODEL_REVISION = "e7b7dc27f91deacad38e78976d1f2b499d76a294"
# The repo also carries a 7.2 GB single-file copy and sample images mflux never reads.
MODEL_ALLOW_PATTERNS = ("model_index.json", "scheduler/*", "text_encoder/*", "tokenizer/*", "transformer/*", "vae/*")
MFLUX_MODEL = "flux2-klein-4b"
QUANTIZE = 8
STEPS = 4
JPEG_QUALITY = 90
MFLUX_REQUIREMENT = "mflux==0.24.0"
MODEL_NEED_GB = 16.0

_TOKEN = re.compile(r"^[A-Za-z0-9_-]{20,200}$")
_KEYS = ("SUPABASE_URL", "SUPABASE_ANON_KEY", "SITE_URL", "TOKEN")
_NOT_FOUND = 44


@dataclass(frozen=True)
class PosterSettings:
    supabase_url: str
    anon_key: str
    site_url: str


def poster_dir() -> Path:
    return config.OMM_HOME / "cluster" / "poster"


def _settings_path() -> Path:
    return poster_dir() / "settings.json"


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def parse_env(text: str) -> tuple[PosterSettings, str]:
    """Read SUPABASE_URL, SUPABASE_ANON_KEY, SITE_URL and TOKEN from KEY=VALUE lines."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = _unquote(value)
    missing = [key for key in _KEYS if not values.get(key)]
    if missing:
        raise ClusterError(f"missing {', '.join(missing)} (expected KEY=VALUE lines for {', '.join(_KEYS)})")
    settings = PosterSettings(values["SUPABASE_URL"], values["SUPABASE_ANON_KEY"], values["SITE_URL"])
    return validate(settings, values["TOKEN"]), values["TOKEN"]


def _check_url(name: str, url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme != "https" or not parts.netloc or parts.query or parts.fragment:
        raise ClusterError(f"{name} must be a plain https:// address")
    return url.strip().rstrip("/")


def _check_token(token: str) -> None:
    if not _TOKEN.match(token):
        raise ClusterError("TOKEN does not look like a poster worker token (20-200 letters, digits, - or _)")


def validate(settings: PosterSettings, token: str) -> PosterSettings:
    _check_token(token)
    if not settings.anon_key.strip():
        raise ClusterError("SUPABASE_ANON_KEY is empty")
    return PosterSettings(_check_url("SUPABASE_URL", settings.supabase_url), settings.anon_key.strip(),
                          _check_url("SITE_URL", settings.site_url))


def save_settings(settings: PosterSettings) -> None:
    from omm.atomic import atomic_write_text

    path = _settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(asdict(settings), indent=2) + "\n")
    path.chmod(0o600)


def load_settings() -> PosterSettings | None:
    try:
        data = json.loads(_settings_path().read_text(encoding="utf-8"))
        return PosterSettings(data["supabase_url"], data["anon_key"], data["site_url"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def delete_settings() -> None:
    _settings_path().unlink(missing_ok=True)


def store_token(token: str, *, run=subprocess.run) -> None:
    _check_token(token)
    line = f"add-generic-password -U -s {KEYCHAIN_SERVICE} -a {KEYCHAIN_ACCOUNT} -w {token}\n"
    result = run([SECURITY, "-i"], input=line, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = " ".join(((result.stderr or "") + " " + (result.stdout or "")).split()).replace(token, "***")
        raise ClusterError(f"could not save the token in the login keychain ({detail[:300]}). Over ssh the keychain "
                           "is often locked: run `security unlock-keychain` first, or run this command once in the "
                           "Mac's own Terminal.")


def read_token(*, run=subprocess.run) -> str | None:
    result = run([SECURITY, "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT, "-w"],
                 capture_output=True, text=True, check=False)
    token = (result.stdout or "").strip()
    return token if result.returncode == 0 and token else None


def delete_token(*, run=subprocess.run) -> None:
    result = run([SECURITY, "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT],
                 capture_output=True, text=True, check=False)
    if result.returncode not in (0, _NOT_FOUND):
        raise ClusterError("could not remove the poster worker token from the login keychain")
