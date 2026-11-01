"""Language of user-facing document search messages.

The document store, sharing and remote modules are shared by the web UI,
which is Korean first, and by `omm rag` and its MCP server, which speak
English like the rest of the CLI. Each message is written in both languages
with :func:`t` and picked when it is made, from a context variable that the
CLI and MCP entry points set to English. Threads start from the default, so
web request threads keep Korean without doing anything.
"""
from __future__ import annotations

import contextvars

KOREAN, ENGLISH = "ko", "en"
_LANGUAGE: contextvars.ContextVar[str] = contextvars.ContextVar("omm_language", default=KOREAN)


def current() -> str:
    return _LANGUAGE.get()


def use(language: str) -> contextvars.Token:
    """Make messages in this context `language` ("ko" or "en"); returns a token for :func:`reset`."""
    return _LANGUAGE.set(ENGLISH if language == ENGLISH else KOREAN)


def reset(token: contextvars.Token) -> None:
    _LANGUAGE.reset(token)


def t(korean: str, english: str) -> str:
    return english if _LANGUAGE.get() == ENGLISH else korean


def from_header(value: str | None) -> str:
    """The language an Accept-Language header asks for; Korean unless it starts with English."""
    first = (value or "").split(",")[0].strip().lower()
    return ENGLISH if first.startswith("en") else KOREAN
