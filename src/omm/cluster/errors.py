"""Shared error type for the `omm cluster` modules."""

from __future__ import annotations


class ClusterError(RuntimeError):
    """A cluster step failed; the message is shown to the user as-is."""


class PlacementError(ClusterError):
    """The Mac an admin asked to load a model on cannot take it."""


class UsageProblem(ClusterError):
    """A request that can never succeed as typed (exit 2)."""
