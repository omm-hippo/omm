import React, {useEffect, useRef, useState} from 'react';
import RichText from './RichText';
import {shouldSend} from './workspace-state.js';
import {sharedMessages} from './shared-ai-state.js';
import './shared-ai.css';

async function sharedApi(path, body, signal) {
  let response;
  try {
    response = await fetch(`/api/shared-ai${path}`, {
      method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin',
      headers: body === undefined ? {} : {'Content-Type':'application/json','X-OMM-Web':'1'},
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: signal || AbortSignal.timeout(75000),
    });
  } catch {
    throw {error:'로컬 OMM과 연결이 끊겼어요.',action:'공유 서버가 계속 처리 중일 수 있어요. OMM 상태를 확인한 뒤 다시 연결하세요. 자동 재전송하지 않아요.'};
  }
  let value;
  try {value = await response.json();} catch {
    throw {error:'공유 AI 화면의 응답을 읽지 못했어요.',action:'공유 AI 기능을 지원하는 OMM을 실행했는지 확인하세요.'};
  }
  if (!response.ok) {
    if (response.status === 404) throw {error:'이 OMM 서버는 공유 AI 연결 화면을 지원하지 않아요.',action:'공유 AI 기능이 포함된 OMM으로 업데이트한 뒤 관리 화면을 다시 실행하세요.'};
    throw {code:value.code,error:value.error || '요청을 처리하지 못했어요.',action:value.action || '연결 상태를 확인하세요.'};
  }
  return value;
}

export default function SharedAI() {
  const [connection,setConnection] = useState(null);
  const [url,setUrl] = useState('');
  const [token,setToken] = useState('');
  const [model,setModel] = useState('');
  const [turns,setTurns] = useState([]);
  const [draft,setDraft] = useState('');
  const [busy,setBusy] = useState('');
  const [initializing,setInitializing] = useState(true);
  const [error,setError] = useState(null);
  const [verified,setVerified] = useState(false);
  const [confirmClear,setConfirmClear] = useState(false);
  const [uncertain,setUncertain] = useState(false);
  const [editing,setEditing] = useState(true);
  const pending = useRef(false), composing = useRef(false);
  const connected = connection?.configured && connection.url === url.trim() && !token;

  useEffect(()=>{
    const controller = new AbortController();
    sharedApi('',undefined,controller.signal).then(value=>{
      if (!controller.signal.aborted) {setConnection(value);setUrl(value.url || '');setModel(value.model || '');setEditing(!value.configured);}
    }).catch(value=>{if (!controller.signal.aborted) setError(value);})
      .finally(()=>{if (!controller.signal.aborted) setInitializing(false);});
    return ()=>controller.abort();
  },[]);

  async function connect(event) {
    event?.preventDefault();
    if (pending.current) return;
    pending.current = true;setBusy('connect');setError(null);
    try {
      const value = await sharedApi('/connect',{url,token});
      setConnection(value);setUrl(value.url);setToken('');setModel(value.model || '');
      setTurns([]);setVerified(false);setUncertain(false);setEditing(false);
    } catch(value) {setError(value);setUncertain(true);setEditing(true);}
    finally {pending.current=false;setBusy('');}
  }

  async function clear() {
    if (pending.current) return;
    pending.current=true;setBusy('clear');setError(null);
    try {
      const value=await sharedApi('/clear',{confirmed:true});
      setConnection(value);setToken('');setModel('');setTurns([]);setDraft('');
      setVerified(false);setUncertain(false);setConfirmClear(false);setEditing(true);
    } catch(value) {setError(value);}
    finally {pending.current=false;setBusy('');}
  }

  async function send(event) {
    event?.preventDefault();
    const question=draft.trim();
    if (!question || !connected || !model || pending.current || uncertain) return;
    pending.current=true;setBusy('message');setError(null);
    const messages=sharedMessages(turns,question);
    try {
      const result=await sharedApi('/messages',{connection_id:connection.connection_id,model,messages,request_id:crypto.randomUUID()});
      setTurns(old=>[...old,{user:question,assistant:result.text,truncated:result.truncated}]);
      setDraft('');setVerified(true);
    } catch(value) {setError(value);setUncertain(true);}
    finally {pending.current=false;setBusy('');}
  }

  return <div className={`shared-ai-panel ${connected&&!editing?'is-connected':''}`}>
    {!connected&&<div className="shared-ai-intro"><span className="shared-ai-kicker">다른 컴퓨터의 모델 사용하기</span><h2>공유받은 AI에 연결하세요</h2><p>관리자가 알려 준 서버 주소와 이용 토큰으로 시작해요. 질문은 연결한 서버로 전송돼요.</p></div>}
    <div className="shared-ai-steps" aria-label="연결 단계"><span className={!connected?'current':''}>1 연결 확인</span><span className={connected&&!verified?'current':''}>2 모델 선택·질문</span><span className={verified?'current':''}>3 실제 응답 확인</span></div>
    <form className="shared-ai-connection" style={connected&&!editing?{display:'none'}:undefined} onSubmit={connect} aria-label="공유 AI 연결">
      <label className="field">서버 주소<input type="text" inputMode="url" autoComplete="off" maxLength={512} placeholder="예: http://100.101.102.103:52440" value={url} onChange={event=>{setUrl(event.target.value);setVerified(false);}} disabled={Boolean(busy)||initializing} required/></label>
      <label className="field">이용 토큰<input type="password" autoComplete="off" spellCheck="false" maxLength={4096} placeholder={connection?.token_saved?'저장된 토큰 사용 · 변경하려면 새 토큰 입력':'관리자에게 받은 OMM 이용 토큰'} value={token} onChange={event=>{setToken(event.target.value);setVerified(false);}} disabled={Boolean(busy)||initializing}/></label>
      <p className="shared-ai-help">Tailscale 장치 공유와 OMM 이용 토큰은 별개예요. 토큰은 이 컴퓨터의 OMM 설정에만 저장하고 화면으로 다시 보내지 않아요.</p>
      <div className="shared-ai-actions"><button className="primary" disabled={!url.trim()||Boolean(busy)||initializing} type="submit">{busy==='connect'?'서버 응답 확인 중…':connection?.configured?'연결 다시 확인':'연결 확인하고 저장'}</button>{(connection?.configured||error?.code==='storage')&&<button type="button" disabled={Boolean(busy)} onClick={()=>setConfirmClear(true)}>저장된 연결 지우기</button>}</div>
      {confirmClear&&<div className="shared-ai-clear" role="group" aria-label="연결 삭제 확인"><p>이 컴퓨터의 주소와 토큰만 지워요. 서버의 모델과 발급된 토큰은 유지돼요.</p><button type="button" className="danger" disabled={Boolean(busy)} onClick={clear}>연결 지우기</button><button type="button" disabled={Boolean(busy)} onClick={()=>setConfirmClear(false)}>취소</button></div>}
    </form>
    {error&&<div className="shared-ai-error" role="alert"><strong>{error.error}</strong><p>{error.action}</p>{uncertain&&<p>질문은 입력란에 남아 있어요. 서버 상태를 확인한 뒤 ‘연결 다시 확인’을 눌러 새 대화를 시작하세요.</p>}</div>}
    {connected&&<section className="shared-ai-conversation" aria-label="공유 AI 대화">
      <div className="shared-ai-connection-status"><span className={`shared-ai-dot ${uncertain?'uncertain':''}`}/><div role="status"><strong>{uncertain?'연결 상태 다시 확인 필요':verified?'이 대화에서 실제 응답을 받았어요':'저장된 연결 · 모델 목록 확인됨'}</strong><p>{connection.url} · 마지막 목록 확인 {new Date(connection.checked_at).toLocaleString('ko-KR')}</p></div><div className="shared-ai-status-actions">{!editing&&<button type="button" disabled={Boolean(busy)} onClick={()=>connect()}>{busy==='connect'?'확인 중…':'연결 다시 확인'}</button>}<button type="button" aria-expanded={editing} disabled={Boolean(busy)} onClick={()=>setEditing(old=>!old)}>{editing?'연결 정보 닫기':'연결 정보 변경'}</button></div></div>
      <label className="field shared-ai-model">관리자에게 확인한 모델<select aria-label="공유 AI 모델" value={model} disabled={Boolean(busy)} onChange={event=>{setModel(event.target.value);setTurns([]);setVerified(false);}}><option value="">모델을 선택하세요</option>{connection.models.map(key=><option key={key} value={key}>{key}</option>)}</select></label>
      <p className="shared-ai-help">목록은 서버가 제공하는 전체 모델이에요. 실행 중인지는 목록만으로 확인되지 않아요. 첫 질문이 선택한 모델로 전송되므로 사용할 모델을 관리자에게 확인하세요.</p>
      {!turns.length&&<div className="shared-ai-welcome"><h3>모델을 고르고 첫 질문을 보내세요</h3><p>예: “7 곱하기 8은 얼마야?”<br/>파일을 자동으로 읽거나 코드를 실행하지 않는 텍스트 대화예요.</p></div>}
      {turns.length>0&&<div className="shared-ai-transcript" role="log" aria-label="공유 AI 대화 내용">{turns.map((turn,index)=><div key={index}><article className="shared-ai-user"><strong>나</strong><p>{turn.user}</p></article><article className="shared-ai-assistant"><strong>{model.split('/').at(-1)}</strong><RichText text={turn.assistant}/>{turn.truncated&&<small>답변 길이 제한에 도달했어요. 이어서 설명해 달라고 요청할 수 있어요.</small>}</article></div>)}</div>}
      <form className="chat-composer shared-ai-composer" onSubmit={send}><textarea aria-label="공유 AI에게 질문" value={draft} onChange={event=>setDraft(event.target.value)} maxLength={4000} placeholder="질문을 입력하세요" disabled={Boolean(busy)||!model||uncertain} onCompositionStart={()=>{composing.current=true;}} onCompositionEnd={()=>{composing.current=false;}} onKeyDown={event=>{if(shouldSend(event)&&!composing.current){event.preventDefault();send();}}}/><div className="chat-send"><small>Enter 전송 · Shift+Enter 줄바꿈</small><button type="submit" className="primary" disabled={!draft.trim()||!model||Boolean(busy)||uncertain}>{busy==='message'?'답변 기다리는 중…':'질문 보내기'}</button></div></form>
      <p className="shared-ai-help">질문과 최근 대화 일부를 서버로 보내요. 대화는 현재 화면에만 남고 새로고침하면 지워져요. 주소와 토큰은 유지돼요.</p>
    </section>}
  </div>;
}
