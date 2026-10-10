"""Local chat history, idempotency, interruption and model-load ownership."""
import json
import threading
import time
from types import SimpleNamespace
import uuid

import pytest

from omm import config, registry
from omm.engines.base import LoadOptions, LoadReceipt, RuntimeHealth, RuntimeModel, RuntimeModelRef, UnloadResult
from omm.web import chat as module
from omm.web.chat import ChatManager
from omm.web.chat_stream import ChatCancelled
from omm.web.jobs import JobConflict, JobManager


def wait_for(manager,key,status,timeout=3):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        value=manager.get(key)
        if value['status']==status:return value
        time.sleep(.005)
    raise AssertionError(manager.get(key))


class Runtime:
    key='ollama'
    def __init__(self):
        self.loaded=False
        self.preloaded=False
        self.other=False
        self.reachable=True
        self.releases=True
        self.calls=[]
    def health(self):return RuntimeHealth(self.reachable)
    def list_models(self):
        return [RuntimeModel('model','model',self.loaded,'model' if self.loaded else None),
                RuntimeModel('other','other',self.other,'other' if self.other else None)]
    def load(self,ref,options):
        self.calls.append('load')
        self.loaded=True
        options=LoadOptions(context_length=2048) if self.preloaded else options
        return LoadReceipt(RuntimeModel('model','model',True,'model'),'model',self.preloaded,not self.preloaded,options)
    def unload(self,receipt):
        self.calls.append('unload')
        if self.releases:self.loaded=False
        return UnloadResult(self.releases)


@pytest.fixture
def prepared(monkeypatch,isolated_omm_home):
    path=config.MODELS_DIR/'model.gguf'
    path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'GGUF-fixture')
    registry.upsert_entry(path.name,linked={'ollama':True},ollama_name='model')
    runtime=Runtime()
    monkeypatch.setattr(module,'adapter_for',lambda engine:runtime)
    monkeypatch.setattr(module.cli,'_compatibility_model_ref',lambda *args:RuntimeModelRef('model'))
    monkeypatch.setattr(module.runtime_profiles,'saved_options_for_file',lambda *args:LoadOptions(4096))
    monkeypatch.setattr(module.runtime_profiles,'ensure_memory',lambda *args:None)
    monkeypatch.setattr(module.hardware,'scan_hardware',lambda:object())
    jobs=JobManager();manager=ChatManager(jobs);jobs.operation_guard=manager.guard_jobs
    yield manager,runtime,jobs,path
    manager.close();jobs.close()


def start(prepared):
    manager,*_=prepared
    row=manager.start({'filename':'model.gguf','engine':'ollama'},str(uuid.uuid4()))
    return wait_for(manager,row['id'],'ready')


@pytest.mark.parametrize('preloaded',[False,True])
def test_explicit_end_preserves_preloaded_models(prepared,preloaded):
    manager,runtime,_,_=prepared
    runtime.loaded=runtime.preloaded=preloaded
    row=start(prepared)
    assert row['loaded_by_omm'] is not preloaded
    manager.end(row['id']);closed=wait_for(manager,row['id'],'closed')
    assert ('unload' in runtime.calls) is not preloaded
    assert runtime.loaded is preloaded
    assert closed['released_owned_load'] is not preloaded


def test_other_loaded_work_blocks_a_new_load(prepared):
    manager,runtime,_,_=prepared
    runtime.other=True
    row=manager.start({'filename':'model.gguf','engine':'ollama'},str(uuid.uuid4()))
    failed=wait_for(manager,row['id'],'failed')
    assert '다른 모델' in failed['notice']
    assert runtime.calls==[] and runtime.other


def test_unreachable_runtime_does_not_load(prepared):
    manager,runtime,_,_=prepared;runtime.reachable=False
    row=manager.start({'filename':'model.gguf','engine':'ollama'},str(uuid.uuid4()))
    wait_for(manager,row['id'],'failed');assert runtime.calls==[]


def test_streamed_turns_are_saved_and_sent_as_context(monkeypatch,prepared):
    manager,runtime,_,_=prepared;row=start(prepared);captured=[]
    def stream(adapter,receipt,messages,stop,on_chunk,on_connection):
        captured.append(messages);on_chunk('안녕');on_chunk('하세요');return 'stop'
    monkeypatch.setattr(module,'stream_reply',stream)
    manager.send(row['id'],'첫 질문',str(uuid.uuid4()));wait_for(manager,row['id'],'ready')
    manager.send(row['id'],'이어서 질문',str(uuid.uuid4()));value=wait_for(manager,row['id'],'ready')
    assert captured[1][-3:]==[{'role':'user','content':'첫 질문'},{'role':'assistant','content':'안녕하세요'},{'role':'user','content':'이어서 질문'}]
    saved=json.loads((manager.root/(row['id']+'.json')).read_text(encoding='utf-8'))
    assert saved['turns']==value['turns'] and value['turns'][0]['assistant']=='안녕하세요'
    assert 'unload' not in runtime.calls


def test_requests_are_idempotent_and_do_not_duplicate_generation(monkeypatch,prepared):
    manager,*_=prepared;row=start(prepared);count=[]
    def stream(*args):count.append(True);args[4]('answer');return 'stop'
    monkeypatch.setattr(module,'stream_reply',stream)
    rid=str(uuid.uuid4());manager.send(row['id'],'hi',rid);wait_for(manager,row['id'],'ready')
    assert len(manager.send(row['id'],'hi',rid)['turns'])==1 and len(count)==1
    with pytest.raises(JobConflict):manager.send(row['id'],'different',rid)
    assert manager.start({'filename':'model.gguf','engine':'ollama'},row['open_request_id'])['id']==row['id']


def test_cancel_keeps_partial_answer_and_excludes_it_from_context(monkeypatch,prepared):
    manager,*_=prepared;row=start(prepared);begun=threading.Event()
    def stream(adapter,receipt,messages,stop,on_chunk,on_connection):
        on_chunk('부분 응답');begun.set();stop.wait(2);raise ChatCancelled()
    monkeypatch.setattr(module,'stream_reply',stream)
    manager.send(row['id'],'중단할 질문',str(uuid.uuid4()));assert begun.wait(1)
    manager.cancel(row['id']);value=wait_for(manager,row['id'],'ready')
    assert value['turns'][0]['assistant']=='부분 응답' and value['turns'][0]['status']=='cancelled'
    captured=[]
    def next_reply(*args):captured.extend(args[2]);args[4]('new answer');return 'stop'
    monkeypatch.setattr(module,'stream_reply',next_reply)
    manager.send(row['id'],'다음 질문',str(uuid.uuid4()));wait_for(manager,row['id'],'ready')
    assert len(captured)==2 and captured[-1]['content']=='다음 질문'


def test_truncated_stream_is_not_saved_as_completed(monkeypatch,prepared):
    manager,*_=prepared;row=start(prepared)
    def stream(*args):args[4]('partial');raise ValueError('missing final frame')
    monkeypatch.setattr(module,'stream_reply',stream)
    manager.send(row['id'],'hi',str(uuid.uuid4()));value=wait_for(manager,row['id'],'ready')
    assert value['turns'][0]['status']=='failed'


@pytest.mark.parametrize('text',['',None,{'text':'hi'},'x'*8193])
def test_invalid_messages_do_not_start_work(prepared,text):
    manager,*_=prepared;row=start(prepared)
    with pytest.raises(ValueError):manager.send(row['id'],text,str(uuid.uuid4()))
    assert manager.get(row['id'])['turns']==[]


def test_management_jobs_are_blocked_until_chat_ends(prepared):
    manager,_,jobs,_=prepared;row=start(prepared)
    with pytest.raises(JobConflict,match='채팅용'):
        jobs.start({'operation':'uninstall','filename':'model.gguf'},str(uuid.uuid4()))
    manager.end(row['id']);wait_for(manager,row['id'],'closed');manager.guard_jobs()


def test_loaded_instance_changed_by_other_work_is_not_unloaded(monkeypatch,prepared):
    manager,runtime,_,_=prepared;row=start(prepared)
    monkeypatch.setattr(runtime,'list_models',lambda:[RuntimeModel('model','model',True,'new-instance')])
    manager.send(row['id'],'hi',str(uuid.uuid4()));value=wait_for(manager,row['id'],'closed')
    assert 'unload' not in runtime.calls and value['turns'][0]['status']=='failed'


def test_expired_load_does_not_implicitly_reload_or_replace_other_work(prepared):
    manager,runtime,_,_=prepared;row=start(prepared);runtime.loaded=False;runtime.other=True
    manager.send(row['id'],'hi',str(uuid.uuid4()));value=wait_for(manager,row['id'],'closed')
    assert runtime.calls.count('load')==1 and runtime.other
    assert value['turns'][0]['status']=='failed'


def test_failed_release_blocks_new_work_and_can_be_retried(prepared):
    manager,runtime,_,_=prepared;row=start(prepared);runtime.releases=False
    manager.end(row['id']);wait_for(manager,row['id'],'release_failed')
    with pytest.raises(JobConflict):manager.guard_jobs()
    runtime.releases=True;manager.end(row['id']);wait_for(manager,row['id'],'closed')


def test_restart_requires_explicit_reconnection_and_preserves_transcript(monkeypatch,prepared):
    manager,_,jobs,_=prepared;row=start(prepared)
    def stream(*args):args[4]('stored answer');return 'stop'
    monkeypatch.setattr(module,'stream_reply',stream)
    manager.send(row['id'],'stored question',str(uuid.uuid4()));wait_for(manager,row['id'],'ready')
    resumed=ChatManager(jobs)
    assert resumed.active is None
    value=resumed.get(row['id']);assert value['status']=='interrupted' and value['turns'][0]['assistant']=='stored answer'
    resumed.close()


def test_delete_only_removes_closed_local_chat_record(prepared):
    manager,*_=prepared;row=start(prepared)
    with pytest.raises(JobConflict):manager.delete(row['id'])
    manager.end(row['id']);wait_for(manager,row['id'],'closed')
    assert manager.delete(row['id'])=={'deleted':True}
    assert not (manager.root/(row['id']+'.json')).exists()


def test_changed_model_bytes_cannot_resume_old_history(prepared):
    manager,_,_,path=prepared;row=start(prepared);manager.end(row['id']);wait_for(manager,row['id'],'closed')
    path.write_bytes(b'changed file')
    manager.start({'filename':'model.gguf','engine':'ollama'},str(uuid.uuid4()),row['id'])
    assert '변경' in wait_for(manager,row['id'],'failed')['notice']


def test_failed_initial_save_does_not_claim_runtime_or_block_jobs(monkeypatch,prepared):
    manager,runtime,_,_=prepared
    with monkeypatch.context() as patch:
        patch.setattr(manager,'_save',lambda record:(_ for _ in ()).throw(OSError('disk full')))
        with pytest.raises(OSError):manager.start({'filename':'model.gguf','engine':'ollama'},str(uuid.uuid4()))
    assert manager.active is None and not manager.records and runtime.calls==[]
    manager.guard_jobs()


def test_failed_message_save_does_not_generate_unrecorded_text(monkeypatch,prepared):
    manager,*_=prepared;row=start(prepared)
    with monkeypatch.context() as patch:
        patch.setattr(manager,'_save',lambda record:(_ for _ in ()).throw(OSError('disk full')))
        with pytest.raises(OSError):manager.send(row['id'],'hi',str(uuid.uuid4()))
    value=manager.get(row['id']);assert value['status']=='ready' and value['turns']==[]


def test_failed_final_save_is_visible_instead_of_claiming_stored_success(monkeypatch,prepared):
    manager,*_=prepared;row=start(prepared);original=manager._save
    def save(record):
        if record['turns'] and record['turns'][-1]['status']=='completed':raise OSError('disk full')
        original(record)
    def stream(*args):args[4]('answer');return 'stop'
    with monkeypatch.context() as patch:
        patch.setattr(manager,'_save',save);patch.setattr(module,'stream_reply',stream)
        manager.send(row['id'],'hi',str(uuid.uuid4()));value=wait_for(manager,row['id'],'ready')
    assert value['turns'][-1]['status']=='failed' and '저장하지 못' in value['notice']
