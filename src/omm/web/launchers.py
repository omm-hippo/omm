"""Create personal desktop launchers for the same installed OMM executable."""
from __future__ import annotations

from pathlib import Path
import shlex
import sys


def command():
    if getattr(sys, 'frozen', False):
        return [sys.executable, 'web', '--open']
    package_root = str(Path(__file__).resolve().parents[2])
    program = f'import sys; sys.path.insert(0, {package_root!r}); from omm.cli import main; main()'
    return [sys.executable, '-c', program, 'web', '--open']


def create(destination: Path) -> list[Path]:
    destination = destination.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    argv = command()
    # Every batch argument is quoted even when it has no whitespace. Percent
    # expansion is escaped and delayed expansion is disabled for personal paths.
    batch = ' '.join('"'+arg.replace('%','%%').replace('"','""')+'"' for arg in argv)
    definitions = {
        'Open-OMM.command': '#!/bin/sh\nset -eu\nexec '+shlex.join(argv)+'\n',
        'Open-OMM.cmd': '@echo off\r\nsetlocal DisableDelayedExpansion\r\n'+batch+'\r\nif errorlevel 1 pause\r\n',
    }
    for name, text in definitions.items():
        path = destination/name
        if path.is_symlink() or (path.exists() and path.read_bytes().decode('utf-8') != text):
            raise ValueError(f'Refusing to replace existing launcher: {name}')
    result=[]
    for name,text in definitions.items():
        path=destination/name
        path.write_text(text,encoding='utf-8',newline='')
        if name.endswith('.command'):path.chmod(0o755)
        result.append(path)
    return result
