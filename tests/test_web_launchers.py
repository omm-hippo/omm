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
    shell=(created[0]).read_text()
    assert shlex.split(shell.split('exec ',1)[1].strip())[0]==executable
    batch=created[1].read_bytes().decode()
    assert 'DisableDelayedExpansion' in batch and 'python%%name!.exe' in batch
    assert '"'+executable.replace('%','%%')+'"' in batch


def test_existing_launcher_is_not_replaced(tmp_path):
    directory=tmp_path/'shortcuts';directory.mkdir()
    (directory/'Open-OMM.command').write_text('personal file')
    with pytest.raises(ValueError,match='replace existing'):launchers.create(directory)
    assert (directory/'Open-OMM.command').read_text()=='personal file'
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
    stub.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > '+shlex.quote(str(output))+'\n')
    stub.chmod(0o755)
    import os
    env=os.environ.copy();env['OMM_EXECUTABLE']=str(stub)
    root=Path(__file__).resolve().parents[1]
    subprocess.run(['sh',str(root/'packaging/web-launchers/Open-OMM.command')],env=env,check=True,timeout=5)
    assert output.read_text().splitlines()==['web','--open']
