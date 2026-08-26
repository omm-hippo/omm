"""Shared "cause + fix" formatting for user-facing CLI error messages
(issue #191). Every standardized error reads as one or two lines: what
went wrong, then - when there is a concrete next step - a copy-pasteable
fix prefixed with an arrow."""

from __future__ import annotations

from rich.console import Console


def print_cli_error(console: Console, cause: str, fix: str | None = None) -> None:
    """Print `cause` as the error line, then `fix` (if given) as a second
    line prefixed with "→ ", styled to draw the eye to the actionable
    step."""
    console.print(f"[error]{cause}[/error]")
    if fix:
        console.print(f"[accent]→ {fix}[/accent]")
