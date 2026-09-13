#!/usr/bin/env python3
"""Fail on text file I/O that leaves the encoding up to the platform.

`open()`, `Path.read_text()` and `Path.write_text()` fall back to
`locale.getencoding()` when no encoding is given. On a Korean or Japanese
Windows box that is cp949/cp932, not UTF-8, so any file omm itself wrote as
UTF-8 - run logs, the registry, config - fails to decode the moment its
contents carry a non-ASCII byte. A repo checkout under a path like
`D:/Desktop/오픈소스 프로젝트` is enough to trigger it, because the run log
records the working directory.

Binary modes are exempt: encoding does not apply to them.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TARGETS = ("src", "tests", "scripts")

# `.open()` on these is not text file I/O: os.open returns a descriptor,
# tarfile/zipfile/gzip open archives and members as bytes. Their `encoding`
# argument, where one exists at all, means something unrelated.
NON_TEXT_RECEIVERS = {"os", "tarfile", "zipfile", "gzip", "bz2", "lzma", "io"}


def _is_binary_mode(call: ast.Call, *, mode_index: int) -> bool:
    """True when the call asks for bytes, where encoding is illegal.

    The builtin takes `open(file, mode)`; `Path.open(mode)` puts mode first.
    """
    mode: str | None = None
    if len(call.args) > mode_index and isinstance(call.args[mode_index], ast.Constant):
        value = call.args[mode_index].value
        if isinstance(value, str):
            mode = value
    for keyword in call.keywords:
        if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
            if isinstance(keyword.value.value, str):
                mode = keyword.value.value
    return mode is not None and "b" in mode


def _has_dynamic_mode(call: ast.Call, mode_index: int) -> bool:
    """True when the mode is computed, so text-vs-binary is undecidable here."""
    if len(call.args) > mode_index and not isinstance(call.args[mode_index], ast.Constant):
        return True
    return any(
        keyword.arg == "mode" and not isinstance(keyword.value, ast.Constant)
        for keyword in call.keywords
    )


def _violations(path: Path) -> list[tuple[int, str]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError) as error:
        raise SystemExit(f"{path}: could not parse ({error})")

    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if any(keyword.arg == "encoding" for keyword in node.keywords):
            continue
        func = node.func

        if isinstance(func, ast.Name) and func.id == "open":
            # A non-constant mode cannot be judged here; leave it alone
            # rather than block on a call we cannot read.
            if not _is_binary_mode(node, mode_index=1) and not _has_dynamic_mode(node, 1):
                found.append((node.lineno, "open()"))
            continue

        if not isinstance(func, ast.Attribute):
            continue

        if isinstance(func.value, ast.Name) and func.value.id in NON_TEXT_RECEIVERS:
            continue

        if func.attr == "read_text":
            # Path.read_text() takes no positional arguments; anything that
            # does is a different API (importlib.metadata's, for one).
            if not node.args:
                found.append((node.lineno, "read_text()"))
        elif func.attr == "write_text":
            found.append((node.lineno, "write_text()"))
        elif func.attr == "open":
            if not _is_binary_mode(node, mode_index=0) and not _has_dynamic_mode(node, 0):
                found.append((node.lineno, "Path.open()"))
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "targets",
        nargs="*",
        default=list(DEFAULT_TARGETS),
        help="directories to scan (default: src tests scripts)",
    )
    args = parser.parse_args()

    failures: list[str] = []
    for target in args.targets:
        base = ROOT / target
        if not base.exists():
            raise SystemExit(f"no such directory: {base}")
        for path in sorted(base.rglob("*.py")):
            for lineno, what in _violations(path):
                failures.append(f"{path.relative_to(ROOT).as_posix()}:{lineno}: {what} without encoding=")

    if failures:
        print("Text file I/O without an explicit encoding:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        print(
            f"\n{len(failures)} call(s). Pass encoding=\"utf-8\" - omm writes and reads "
            "UTF-8 everywhere, and the platform default is cp949/cp932 on a Korean or "
            "Japanese Windows install.",
            file=sys.stderr,
        )
        return 1

    print("All text file I/O declares an encoding.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
