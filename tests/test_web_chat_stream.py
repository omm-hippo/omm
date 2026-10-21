"""Runtime wire formats and loopback-only streamed responses (no live service)."""
import json
import threading
from types import SimpleNamespace

import pytest

from omm.engines.base import LoadOptions, LoadReceipt, RuntimeAdapterError, RuntimeModel
from omm.web.chat_stream import ChatCancelled, stream_reply


@pytest.fixture
def transport(monkeypatch):
    calls=[]
    responses=[]
    class Response:
        status_code=200
        closed=False
        def iter_lines(self,**kwargs):yield from self.lines
        def close(self):self.closed=True
    class Session:
        trust_env=True
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def post(self,url,**kwargs):
            assert self.trust_env is False
            calls.append((url,kwargs));return responses.pop(0)
    import requests
    monkeypatch.setattr(requests,'Session',Session)
    def response(lines,status=200):
        item=Response();item.lines=lines;item.status_code=status;responses.append(item);return item
    return calls,response


def runtime(engine='ollama',owned=True):
    adapter=SimpleNamespace(key=engine,_client=SimpleNamespace(base_url='http://127.0.0.1:12345',_headers={}))
    receipt=LoadReceipt(RuntimeModel('model','model',True,'instance'),'instance',not owned,owned,LoadOptions(2048))
    return adapter,receipt


def test_ollama_sends_history_and_preserves_observed_context(transport):
    calls,response=transport;adapter,receipt=runtime(owned=False);output=[]
    response([json.dumps({'message':{'content':'hello '},'done':False}).encode(),json.dumps({'message':{'content':'world'},'done':True,'done_reason':'stop'}).encode()])
    messages=[{'role':'user','content':'hi'}]
    assert stream_reply(adapter,receipt,messages,threading.Event(),output.append,lambda r:None)=='stop'
    payload=calls[0][1]['json']
    assert output==['hello ','world'] and payload['messages']==messages
    assert payload['options']['num_ctx']==2048 and 'keep_alive' not in payload
    assert calls[0][1]['allow_redirects'] is False


def test_lmstudio_reads_openai_compatible_text_deltas(transport):
    calls,response=transport;adapter,receipt=runtime('lmstudio');output=[]
    response([b'data: {"choices":[{"delta":{"content":"hello"},"finish_reason":null}]}',b'',b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',b'data: [DONE]'])
    assert stream_reply(adapter,receipt,[{'role':'user','content':'hi'}],threading.Event(),output.append,lambda r:None)=='stop'
    assert output==['hello'] and calls[0][0].endswith('/v1/chat/completions')
    assert calls[0][1]['json']['model']=='instance'


def test_ollama_non_thinking_compatibility_retry_is_bounded(transport):
    calls,response=transport;adapter,receipt=runtime();response([],400)
    response([b'{"message":{"content":"OK"},"done":true}'])
    stream_reply(adapter,receipt,[],threading.Event(),lambda t:None,lambda r:None)
    assert len(calls)==2 and 'think' not in calls[1][1]['json']


@pytest.mark.parametrize('lines',[[b'{"message":{"content":"partial"},"done":false}'],[b'broken'],[b'{"error":"private runtime text"}']])
def test_incomplete_or_malformed_stream_cannot_pass(transport,lines):
    _,response=transport;adapter,receipt=runtime();response(lines)
    with pytest.raises(RuntimeAdapterError):stream_reply(adapter,receipt,[],threading.Event(),lambda t:None,lambda r:None)


def test_redirects_are_not_followed(transport):
    calls,response=transport;adapter,receipt=runtime();response([],307)
    with pytest.raises(RuntimeAdapterError):stream_reply(adapter,receipt,[],threading.Event(),lambda t:None,lambda r:None)
    assert len(calls)==1 and calls[0][1]['allow_redirects'] is False


def test_cancel_closes_response(transport):
    _,response=transport;adapter,receipt=runtime();item=response([b'{"message":{"content":"partial"},"done":false}'])
    stop=threading.Event()
    def chunk(text):stop.set()
    with pytest.raises(ChatCancelled):stream_reply(adapter,receipt,[],stop,chunk,lambda r:None)
    assert item.closed


def test_remote_runtime_is_rejected_before_requests(transport):
    calls,_=transport;adapter,receipt=runtime();adapter._client.base_url='https://evil.example'
    with pytest.raises(ValueError):stream_reply(adapter,receipt,[],threading.Event(),lambda t:None,lambda r:None)
    assert calls==[]
