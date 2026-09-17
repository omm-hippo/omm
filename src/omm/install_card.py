"""Plain data structures for the model-install source and result cards."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit


def _size_text(value: int | None) -> str:
    if value is None:
        return "unknown (the download response will be checked when available)"
    gib = value / (1024**3)
    return f"{value:,} bytes ({gib:.2f} GiB)"


def plan(
    *,
    provider: str | None,
    repository: str | None,
    filename: str,
    size_bytes: int | None,
    destination: Path,
    url: str,
    expected_sha256: str | None,
    cache_source: str | None = None,
) -> dict[str, object]:
    return {
        "provider": provider or "direct HTTPS source",
        "repository": repository or "not provided",
        "file": filename,
        "size_bytes": size_bytes,
        "size": _size_text(size_bytes),
        "format": "GGUF",
        "destination": str(destination),
        "source": cache_source or "network",
        "planned_checks": {
            "https": urlsplit(url).scheme.casefold() == "https",
            "size": "provider size and HTTP response length" if size_bytes else "HTTP response length when provided",
            "sha256": "provider/pinned digest match" if expected_sha256 else "required before provider download",
        },
    }


def result(
    *,
    url: str,
    actual_size: int,
    expected_size: int | None,
    actual_sha256: str,
    expected_sha256: str | None,
    reused_verified_cache: bool,
) -> dict[str, object]:
    return {
        "https": (
            "source is HTTPS; no network request was made this run"
            if reused_verified_cache and urlsplit(url).scheme.casefold() == "https"
            else "passed"
            if urlsplit(url).scheme.casefold() == "https"
            else "failed"
        ),
        "size": (
            "matched provider metadata"
            if expected_size is not None and actual_size == expected_size
            else "read from the verified cached file; no HTTP size check was made this run"
            if reused_verified_cache
            else "checked against the HTTP response when available"
        ),
        "size_bytes": actual_size,
        "sha256": "matched expected digest" if expected_sha256 == actual_sha256 else "computed",
        "sha256_digest": actual_sha256,
        "reused_verified_cache": reused_verified_cache,
        "meaning": "A matching hash proves byte-for-byte integrity against that digest; it does not prove the file is non-malicious.",
    }
