"""A status problem that also says who acts on it and how (`omm cluster` guide).

`Problem` is a `str`, so `omm cluster status`, its JSON and every caller that
treats problems as text keep working; `guide.py` reads the extra fields to
turn the same list into steps, so the two views never disagree.
"""

from __future__ import annotations

from typing import Literal

Kind = Literal["run", "manual", "wait", "tell"]


class Problem(str):
    """Problem text plus how it gets resolved.

    kind: "run" (omm can fix it on this Mac: `fix` names the fix),
    "manual" (a person acts on this Mac: a screen prompt, a cable),
    "wait" (it resolves by itself), "tell" (it is done on another Mac,
    named by `where`, or on employee PCs).
    after: a fix to offer once a manual step is done (usually "restart").
    """

    kind: Kind
    where: str | None
    fix: str | None
    after: str | None

    def __new__(cls, text: str, kind: Kind = "manual", *, where: str | None = None, fix: str | None = None,
                after: str | None = None) -> "Problem":
        value = super().__new__(cls, text)
        value.kind, value.where, value.fix, value.after = kind, where, fix, after
        return value

    def __reduce__(self):
        return (_rebuild, (str(self), self.kind, self.where, self.fix, self.after))


def _rebuild(text, kind, where, fix, after):
    return Problem(text, kind, where=where, fix=fix, after=after)


def of(value: str) -> Problem:
    """A plain string (an error text, an older check) as a problem on this Mac."""
    return value if isinstance(value, Problem) else Problem(value)
