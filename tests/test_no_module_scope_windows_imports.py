"""Regression guard: Windows-only stdlib modules must be imported inside a
function, never at module scope, so importing omm on Linux/macOS can't fail.

``ctypes.wintypes`` is the sharp edge here. It defines simple types whose
type codes only exist in the Windows build of ``_ctypes``, so
``from ctypes import wintypes`` raises ValueError - not ImportError - on
other platforms. A module-scope import of it in omm.hardware would take the
whole CLI down at import time on every non-Windows machine, which no
Windows-only unit test can catch.
"""

import ast
from pathlib import Path

_WINDOWS_ONLY_MODULES = frozenset(
    {"ctypes.wintypes", "winreg", "msvcrt", "_winapi", "winsound"}
)


def _module_scope_imports(tree: ast.Module):
    """Yield (lineno, module) for imports that run at import time.

    Function bodies are skipped: that is the deferred-import pattern the rest
    of omm already uses (``import winreg`` inside _windows_cpu_model). Class
    bodies and module-level if/try blocks are *not* skipped - they execute
    when the module is imported.
    """

    def walk(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if isinstance(child, ast.Import):
                for alias in child.names:
                    yield child.lineno, alias.name
            elif isinstance(child, ast.ImportFrom):
                base = child.module or ""
                yield child.lineno, base
                for alias in child.names:
                    yield child.lineno, f"{base}.{alias.name}" if base else alias.name
            else:
                yield from walk(child)

    return walk(tree)


def test_no_windows_only_module_imported_at_module_scope():
    src_root = Path(__file__).resolve().parent.parent / "src" / "omm"
    offenders = []
    for path in sorted(src_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for lineno, name in _module_scope_imports(tree):
            if name in _WINDOWS_ONLY_MODULES:
                relpath = path.relative_to(src_root.parent.parent)
                offenders.append(f"{relpath.as_posix()}:{lineno}: {name}")
    assert not offenders, (
        "Windows-only modules imported at module scope - move the import "
        "inside the function that needs it:\n" + "\n".join(offenders)
    )
