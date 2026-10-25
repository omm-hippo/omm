"""Shared error type for the `omm cluster` modules."""

from __future__ import annotations


class ClusterError(RuntimeError):
    """A cluster step failed; the message is shown to the user as-is."""
