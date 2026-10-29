import React, {useEffect, useRef, useState} from 'react';
import RichText from './RichText';
import {shouldSend} from './workspace-state.js';
import {sharedMessages} from './shared-ai-state.js';
import './shared-ai.css';
import {L} from './i18n.js';

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
    throw {error:L('로컬 OMM과 연결이 끊겼어요.','Lost the connection to local OMM.'),action:L('공유 서버가 계속 처리 중일 수 있어요. OMM 상태를 확인한 뒤 다시 연결하세요. 자동 재전송하지 않아요.','The shared server may still be working on it. Check OMM, then reconnect. Nothing is resent automatically.')};
  }
  let value;
  try {value = await response.json();} catch {
    throw {error:L('공유 AI 화면의 응답을 읽지 못했어요.','Couldn’t read the Shared AI response.'),action:L('공유 AI 기능을 지원하는 OMM을 실행했는지 확인하세요.','Make sure you’re running a version of OMM that supports Shared AI.')};
  }
  if (!response.ok) {
    if (response.status === 404) throw {error:L('이 OMM 서버는 공유 AI 연결 화면을 지원하지 않아요.','This OMM server doesn’t support Shared AI connections.'),action:L('공유 AI 기능이 포함된 OMM으로 업데이트한 뒤 관리 화면을 다시 실행하세요.','Update to a version of OMM with Shared AI, then restart the management page.')};
    throw {code:value.code,error:value.error || L('요청을 처리하지 못했어요.','Couldn’t process the request.'),action:value.action || L('연결 상태를 확인하세요.','Check the connection.')};
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
    {!connected&&<div className="shared-ai-intro"><span className="shared-ai-kicker">{L('다른 컴퓨터의 모델 사용하기','Use a model on another computer')}</span><h2>{L('공유받은 AI에 연결하세요','Connect to a shared AI')}</h2><p>{L('관리자가 알려 준 서버 주소와 이용 토큰으로 시작해요. 질문은 연결한 서버로 전송돼요.','Start with the server address and access token from your admin. Your questions are sent to the connected server.')}</p></div>}
    <div className="shared-ai-steps" aria-label={L('연결 단계','Connection steps')}><span className={!connected?'current':''}>{L('1 연결 확인','1 Check connection')}</span><span className={connected&&!verified?'current':''}>{L('2 모델 선택·질문','2 Choose model · ask')}</span><span className={verified?'current':''}>{L('3 실제 응답 확인','3 Confirm a real answer')}</span></div>
    <form className="shared-ai-connection" style={connected&&!editing?{display:'none'}:undefined} onSubmit={connect} aria-label={L('공유 AI 연결','Shared AI connection')}>
      <label className="field">{L('서버 주소','Server address')}<input type="text" inputMode="url" autoComplete="off" maxLength={512} placeholder={L('예: http://100.101.102.103:52440','e.g. http://100.101.102.103:52440')} value={url} onChange={event=>{setUrl(event.target.value);setVerified(false);}} disabled={Boolean(busy)||initializing} required/></label>
      <label className="field">{L('이용 토큰','Access token')}<input type="password" autoComplete="off" spellCheck="false" maxLength={4096} placeholder={connection?.token_saved?L('저장된 토큰 사용 · 변경하려면 새 토큰 입력','Using saved token · enter a new one to change it'):L('관리자에게 받은 OMM 이용 토큰','OMM access token from your admin')} value={token} onChange={event=>{setToken(event.target.value);setVerified(false);}} disabled={Boolean(busy)||initializing}/></label>
      <p className="shared-ai-help">{L('Tailscale 장치 공유와 OMM 이용 토큰은 별개예요. 토큰은 이 컴퓨터의 OMM 설정에만 저장하고 화면으로 다시 보내지 않아요.','Tailscale device sharing and the OMM access token are separate. The token is stored only in this computer’s OMM settings and is never sent back to this page.')}</p>
      <div className="shared-ai-actions"><button className="primary" disabled={!url.trim()||Boolean(busy)||initializing} type="submit">{busy==='connect'?L('서버 응답 확인 중…','Checking server response…'):connection?.configured?L('연결 다시 확인','Recheck connection'):L('연결 확인하고 저장','Check and save connection')}</button>{(connection?.configured||error?.code==='storage')&&<button type="button" disabled={Boolean(busy)} onClick={()=>setConfirmClear(true)}>{L('저장된 연결 지우기','Remove saved connection')}</button>}</div>
      {confirmClear&&<div className="shared-ai-clear" role="group" aria-label={L('연결 삭제 확인','Confirm connection removal')}><p>{L('이 컴퓨터의 주소와 토큰만 지워요. 서버의 모델과 발급된 토큰은 유지돼요.','Only the address and token on this computer are removed. The server’s models and issued tokens stay.')}</p><button type="button" className="danger" disabled={Boolean(busy)} onClick={clear}>{L('연결 지우기','Remove connection')}</button><button type="button" disabled={Boolean(busy)} onClick={()=>setConfirmClear(false)}>{L('취소','Cancel')}</button></div>}
    </form>
    {error&&<div className="shared-ai-error" role="alert"><strong>{error.error}</strong><p>{error.action}</p>{uncertain&&<p>{L('질문은 입력란에 남아 있어요. 서버 상태를 확인한 뒤 ‘연결 다시 확인’을 눌러 새 대화를 시작하세요.','Your question is still in the input box. Check the server, then click ‘Recheck connection’ to start a new conversation.')}</p>}</div>}
    {connected&&<section className="shared-ai-conversation" aria-label={L('공유 AI 대화','Shared AI conversation')}>
      <div className="shared-ai-connection-status"><span className={`shared-ai-dot ${uncertain?'uncertain':''}`}/><div role="status"><strong>{uncertain?L('연결 상태 다시 확인 필요','Connection needs rechecking'):verified?L('이 대화에서 실제 응답을 받았어요','Got a real answer in this conversation'):L('저장된 연결 · 모델 목록 확인됨','Saved connection · model list checked')}</strong><p>{connection.url} · {L('마지막 목록 확인','List last checked')} {new Date(connection.checked_at).toLocaleString(L('ko-KR','en-US'))}</p></div><div className="shared-ai-status-actions">{!editing&&<button type="button" disabled={Boolean(busy)} onClick={()=>connect()}>{busy==='connect'?L('확인 중…','Checking…'):L('연결 다시 확인','Recheck connection')}</button>}<button type="button" aria-expanded={editing} disabled={Boolean(busy)} onClick={()=>setEditing(old=>!old)}>{editing?L('연결 정보 닫기','Close connection details'):L('연결 정보 변경','Change connection details')}</button></div></div>
      <label className="field shared-ai-model">{L('관리자에게 확인한 모델','Model confirmed with your admin')}<select aria-label={L('공유 AI 모델','Shared AI model')} value={model} disabled={Boolean(busy)} onChange={event=>{setModel(event.target.value);setTurns([]);setVerified(false);}}><option value="">{L('모델을 선택하세요','Choose a model')}</option>{connection.models.map(key=><option key={key} value={key}>{key}</option>)}</select></label>
      <p className="shared-ai-help">{L('목록은 서버가 제공하는 전체 모델이에요. 실행 중인지는 목록만으로 확인되지 않아요. 첫 질문이 선택한 모델로 전송되므로 사용할 모델을 관리자에게 확인하세요.','This list shows every model the server offers. It doesn’t show whether a model is running. Your first question goes to the model you choose, so check with your admin which one to use.')}</p>
      {!turns.length&&<div className="shared-ai-welcome"><h3>{L('모델을 고르고 첫 질문을 보내세요','Choose a model and send your first question')}</h3><p>{L('예: “7 곱하기 8은 얼마야?”','e.g. “What is 7 times 8?”')}<br/>{L('파일을 자동으로 읽거나 코드를 실행하지 않는 텍스트 대화예요.','This is a text-only chat. It doesn’t read files or run code on its own.')}</p></div>}
      {turns.length>0&&<div className="shared-ai-transcript" role="log" aria-label={L('공유 AI 대화 내용','Shared AI transcript')}>{turns.map((turn,index)=><div key={index}><article className="shared-ai-user"><strong>{L('나','You')}</strong><p>{turn.user}</p></article><article className="shared-ai-assistant"><strong>{model.split('/').at(-1)}</strong><RichText text={turn.assistant}/>{turn.truncated&&<small>{L('답변 길이 제한에 도달했어요. 이어서 설명해 달라고 요청할 수 있어요.','The answer hit the length limit. You can ask it to continue.')}</small>}</article></div>)}</div>}
      <form className="chat-composer shared-ai-composer" onSubmit={send}><textarea aria-label={L('공유 AI에게 질문','Ask the shared AI')} value={draft} onChange={event=>setDraft(event.target.value)} maxLength={4000} placeholder={L('질문을 입력하세요','Type a question')} disabled={Boolean(busy)||!model||uncertain} onCompositionStart={()=>{composing.current=true;}} onCompositionEnd={()=>{composing.current=false;}} onKeyDown={event=>{if(shouldSend(event)&&!composing.current){event.preventDefault();send();}}}/><div className="chat-send"><small>{L('Enter 전송 · Shift+Enter 줄바꿈','Enter to send · Shift+Enter for new line')}</small><button type="submit" className="primary" disabled={!draft.trim()||!model||Boolean(busy)||uncertain}>{busy==='message'?L('답변 기다리는 중…','Waiting for answer…'):L('질문 보내기','Send question')}</button></div></form>
      <p className="shared-ai-help">{L('질문과 최근 대화 일부를 서버로 보내요. 대화는 현재 화면에만 남고 새로고침하면 지워져요. 주소와 토큰은 유지돼요.','Your question and part of the recent conversation are sent to the server. The conversation stays only on this page and is cleared on reload. The address and token are kept.')}</p>
    </section>}
  </div>;
}
