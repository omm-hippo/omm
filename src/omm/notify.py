"""Best-effort desktop notifications for background automation (see
watch.py). A missed notification is far less bad than the auto-import loop
dying because a notification backend glitched, so every failure here is
swallowed rather than raised."""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def notify(title: str, body: str) -> None:
    try:
        from plyer import notification
    except ImportError:
        log.debug("plyer not installed; skipping desktop notification")
        return
    try:
        notification.notify(title=title, message=body, app_name="omm", timeout=8)
    except Exception:
        log.debug("desktop notification failed", exc_info=True)
