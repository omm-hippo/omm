from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner
from omm import cli
from omm.web import launchers


def test_create_is_idempotent_and_handles_quoted_executable(monkeypatch,tmp_path):
    executable=str(tmp_path/'Python & spaces'/'python%name!.exe')
    monkeypatch.setattr(launchers.sys,'executable',executable)
    created=launchers.create(tmp_path/'shortcuts')
    assert launchers.create(tmp_path/'shortcuts')==created
    shell=(created[0]).read_text(encoding="utf-8")
    assert shlex.split(shell.split('exec ',1)[1].strip())[0]==executable
    batch=created[1].read_bytes().decode()
    assert 'DisableDelayedExpansion' in batch and 'python%%name!.exe' in batch
    assert '"'+executable.replace('%','%%')+'"' in batch


def test_existing_launcher_is_not_replaced(tmp_path):
    directory=tmp_path/'shortcuts';directory.mkdir()
    (directory/'Open-OMM.command').write_text('personal file',encoding='utf-8')
    with pytest.raises(ValueError,match='replace existing'):launchers.create(directory)
    assert (directory/'Open-OMM.command').read_text(encoding="utf-8")=='personal file'
    assert not (directory/'Open-OMM.cmd').exists()


def test_cli_writes_launchers_without_starting_server(monkeypatch,tmp_path,isolated_omm_home):
    from omm.web import server
    monkeypatch.setattr(server,'serve',lambda *args,**kwargs: pytest.fail('server started during launcher creation'))
    result=CliRunner().invoke(cli.app,['web','--create-launchers',str(tmp_path/'shortcuts')])
    assert result.exit_code==0,result.output
    assert (tmp_path/'shortcuts'/'Open-OMM.command').is_file()


@pytest.mark.skipif(os.name=='nt',reason='POSIX launcher contract')
def test_mac_source_launcher_dispatches_only_to_the_selected_stub(tmp_path):
    stub=tmp_path/'omm stub';output=tmp_path/'arguments'
    stub.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > '+shlex.quote(str(output))+'\n',encoding='utf-8')
    stub.chmod(0o755)
    import os
    env=os.environ.copy();env['OMM_EXECUTABLE']=str(stub)
    root=Path(__file__).resolve().parents[1]
    subprocess.run(['sh',str(root/'packaging/web-launchers/Open-OMM.command')],env=env,check=True,timeout=5)
    assert output.read_text(encoding="utf-8").splitlines()==['web','--open']


def _source_cmd_launcher() -> bytes:
    return (Path(__file__).resolve().parents[1] / 'packaging/web-launchers/Open-OMM.cmd').read_bytes()


def test_windows_source_launcher_switches_to_utf8_before_korean_guidance():
    """#423: the missing-install guidance is Korean. cmd parses a whole
    parenthesized block before running it, so chcp 65001 must run on its own
    top-level line before any Korean line, never inside an if (...) block."""
    text = _source_cmd_launcher().decode('utf-8').replace('\r\n', '\n')
    lines = text.splitlines()
    chcp = lines.index('chcp 65001 >nul')
    korean = [i for i, line in enumerate(lines) if any('\uac00' <= ch <= '\ud7a3' for ch in line)]
    assert korean and min(korean) > chcp
    assert all(line.startswith('echo ') for line in (lines[i] for i in korean))
    assert lines.index('if not errorlevel 1 exit /b 0') < chcp
    assert 'docs/beginner-install.md' in text
    assert lines[-1] == 'exit /b 1'


@pytest.mark.skipif(os.name != 'nt', reason='cmd.exe launcher contract')
def test_windows_source_launcher_prints_korean_guidance_only_on_failure(tmp_path):
    source = _source_cmd_launcher().decode('utf-8').replace('\r\n', '\n').replace('\n', '\r\n')
    # Stand in for omm so the real installed omm (if any) is never started.
    for result_code, name in ((9009, 'missing.cmd'), (0, 'ok.cmd')):
        script = source.replace('"%~dp0omm.exe" web --open', f'cmd /c exit {result_code}')
        script = script.replace('  omm web --open\r\n', f'  cmd /c exit {result_code}\r\n')
        path = tmp_path / name
        path.write_bytes(script.encode('utf-8'))
        completed = subprocess.run(
            ['cmd', '/c', str(path)], input=b'\r\n', capture_output=True, timeout=30
        )
        output = completed.stdout.decode('utf-8', errors='replace')
        if result_code:
            assert completed.returncode == 1
            assert 'OMM 관리 화면을 시작하지 못했습니다' in output
        else:
            assert completed.returncode == 0
            assert output.strip() == ''
