import React, {useEffect,useRef,useState} from 'react';
import {api} from './api';

const activeStates=['starting','ready','generating','cancelling','closing','release_failed'];
const pollingStates=['starting','generating','cancelling','closing'];
const names={ollama:'Ollama',lmstudio:'LM Studio'};
const labels={starting:'모델 준비 중',ready:'대화 연결됨',generating:'응답 생성 중',cancelling:'응답 중단 중',closing:'연결 종료 중',closed:'대화 종료됨',failed:'연결 실패',interrupted:'다시 연결 필요',release_failed:'모델 상태 확인 필요'};

export default function ChatPanel({models,initialModel,onModelStatusChange}) {
  const [history,setHistory]=useState([]);
  const [chat,setChat]=useState(null);
  const [modelId,setModelId]=useState(initialModel?.id || '');
  const [engine,setEngine]=useState(initialModel?.engines.find(x=>names[x]) || 'ollama');
  const [draft,setDraft]=useState('');
  const [error,setError]=useState('');
  const [busy,setBusy]=useState(false);
  const [initializing,setInitializing]=useState(true);
  const [deleteConfirm,setDeleteConfirm]=useState(false);
  const openId=useRef(null),messageId=useRef(null),sequence=useRef(0);
  const transcript=useRef(null),follow=useRef(true),composer=useRef(null);
  const eligible=models.filter(x=>x.exists && x.engines.some(key=>names[key]));
  const selected=eligible.find(x=>x.id===modelId);
  const linked=selected?.engines.filter(key=>names[key]) || [];
  const connected=chat && activeStates.includes(chat.status);
  const generating=chat && ['generating','cancelling'].includes(chat.status);
  const ready=chat?.status==='ready';
  const lastTurn=chat?.turns.at(-1);

  async function readHistory(signal) {
    const rows=await api('/api/chats',undefined,signal);setHistory(rows);return rows;
  }
  useEffect(()=>{
    const controller=new AbortController();
    async function restore() {
      try {
        const rows=await readHistory(controller.signal);
        const active=rows.find(x=>activeStates.includes(x.status));
        if(active && !controller.signal.aborted)setChat(await api(`/api/chats/${active.id}`,undefined,controller.signal));
      }catch(e){if(!controller.signal.aborted)setError(e.message);}
      finally{if(!controller.signal.aborted)setInitializing(false);}
    }
    restore();return ()=>controller.abort();
  },[]);
  useEffect(()=>{
    if(initialModel && !connected){setModelId(initialModel.id);setEngine(initialModel.engines.find(x=>names[x]) || 'ollama');}
  },[initialModel?.id,Boolean(connected)]);
  useEffect(()=>{
    if(!modelId && eligible.length)setModelId(eligible[0].id);
  },[modelId,eligible[0]?.id]);
  useEffect(()=>{
    if(linked.length && !linked.includes(engine))setEngine(linked[0]);
  },[modelId,engine,linked.join(',')]);
  useEffect(()=>{
    if(!chat || !pollingStates.includes(chat.status))return;
    const controller=new AbortController();let timer;
    async function poll() {
      try {
        const value=await api(`/api/chats/${chat.id}`,undefined,controller.signal);
        if(controller.signal.aborted)return;
        setChat(value);
        if(!pollingStates.includes(value.status)){
          await readHistory(controller.signal);onModelStatusChange();return;
        }
      }catch(e){if(!controller.signal.aborted)setError(e.message);}
      if(!controller.signal.aborted)timer=setTimeout(poll,300);
    }
    timer=setTimeout(poll,150);
    return ()=>{controller.abort();clearTimeout(timer);};
  },[chat?.id,chat?.status]);
  useEffect(()=>{
    const element=transcript.current;
    if(element && follow.current)element.scrollTop=element.scrollHeight;
  },[lastTurn?.id,lastTurn?.assistant,chat?.status]);

  async function start() {
    if(!selected)return;
    setBusy(true);setError('');openId.current ||= crypto.randomUUID();
    const existing=chat && !connected;
    try {
      const value=await api('/api/chats',{id:selected.id,engine,confirmed:true,request_id:openId.current,...(existing?{chat_id:chat.id}:{})});
      setChat(value);openId.current=null;follow.current=true;await readHistory();
    }catch(e){setError(e.message);}finally{setBusy(false);}
  }
  async function send(event) {
    event.preventDefault();if(!draft.trim() || !ready || busy)return;
    setBusy(true);setError('');messageId.current ||= crypto.randomUUID();follow.current=true;
    try {
      const value=await api(`/api/chats/${chat.id}/messages`,{text:draft,request_id:messageId.current});
      setChat(value);setDraft('');messageId.current=null;
    }catch(e){setError(e.message);}finally{setBusy(false);}
  }
  async function action(name) {
    if(!chat)return;
    setBusy(true);setError('');
    try {const value=await api(`/api/chats/${chat.id}/${name}`,{confirmed:true});
      if(name==='delete'){setChat(null);setDraft('');setDeleteConfirm(false);}
      else setChat(value);
      await readHistory();onModelStatusChange();
    }catch(e){setError(e.message);}finally{setBusy(false);}
  }
  async function openHistory(id) {
    const current=++sequence.current;setDeleteConfirm(false);
    if(!id){setChat(null);setDraft('');openId.current=messageId.current=null;return;}
    setError('');
    try {const value=await api(`/api/chats/${id}`);if(current!==sequence.current)return;
      setChat(value);setModelId(value.model_id);setEngine(value.engine);setDraft('');openId.current=messageId.current=null;follow.current=true;
    }catch(e){if(current===sequence.current)setError(e.message);}
  }
  function exportChat() {
    const url=URL.createObjectURL(new Blob([JSON.stringify(chat,null,2)+'\n'],{type:'application/json'}));
    const link=document.createElement('a');link.href=url;link.download=`omm-chat-${chat.id}.json`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  }
  return <section className="chat-panel">
    <div className="chat-controls"><label className="field">대화 기록<select aria-label="대화 기록" value={chat?.id || ''} disabled={Boolean(connected) || busy || initializing} onChange={e=>openHistory(e.target.value)}><option value="">새 대화</option>{history.map(row=><option key={row.id} value={row.id}>{row.filename} · {new Date(row.updated_at*1000).toLocaleString('ko-KR')}</option>)}</select></label>{chat && !connected && <button onClick={()=>openHistory('')}>새 대화</button>}{chat && <button onClick={exportChat} disabled={busy}>대화 JSON 저장</button>}</div>
    {error && <p className="error" role="alert">{error}</p>}
    {!connected && <div className="chat-connect"><div className="chat-selectors"><label className="field">모델<select aria-label="대화 모델" disabled={Boolean(chat) || busy} value={modelId} onChange={e=>{setModelId(e.target.value);openId.current=null;}}><option value="">모델 선택</option>{eligible.map(row=><option value={row.id} key={row.id}>{row.filename}</option>)}</select></label><label className="field">실행 앱<select aria-label="대화 실행 앱" value={engine} disabled={Boolean(chat) || busy} onChange={e=>{setEngine(e.target.value);openId.current=null;}}>{linked.map(key=><option key={key} value={key}>{names[key]}</option>)}</select></label></div><p>선택한 로컬 모델과 대화해요. 질문과 답변은 이 컴퓨터에 저장되며 외부 AI 서비스로 보내지 않아요.</p><button className="primary" disabled={!selected || !linked.includes(engine) || busy || initializing} onClick={start}>{busy?'연결 요청 중…':chat?'이 대화 다시 연결':'대화 시작'}</button>{!eligible.length && <p>Ollama 또는 LM Studio에 연결된 모델이 필요해요. 내 모델에서 먼저 연결하세요.</p>}</div>}
    {chat && <><div className="chat-heading"><div><h2>{chat.filename}</h2><p>{names[chat.engine]} · <span role="status">{labels[chat.status]}</span></p></div>{connected && <button onClick={()=>action('end')} disabled={busy || chat.status==='closing'}>{chat.loaded_by_omm===false?'대화 연결 종료':'모델 종료'}</button>}</div>{chat.notice && <p className="notice">{chat.notice}</p>}<div className="chat-transcript" ref={transcript} role="log" aria-label="대화 내용" aria-live="polite" onScroll={e=>{const el=e.currentTarget;follow.current=el.scrollHeight-el.scrollTop-el.clientHeight<80;}}>{!chat.turns.length && <p className="chat-empty">궁금한 내용을 입력해 보세요.</p>}{chat.turns.map(turn=><React.Fragment key={turn.id}><article className="chat-message user-message"><strong>나</strong><div>{turn.user}</div></article><article className="chat-message assistant-message"><strong>모델</strong><div>{turn.assistant || (turn.status==='generating'?'응답을 기다리고 있어요.':'응답이 없어요.')}</div>{turn.status==='cancelled' && <small>중단된 응답 · 다음 질문의 문맥에 포함하지 않아요.</small>}{turn.status==='interrupted' && <small>서버가 종료되기 전의 일부 응답이에요.</small>}{turn.status==='failed' && <small role="alert">{turn.error}</small>}{turn.finish_reason==='length' && <small>응답 길이 제한에 도달했어요.</small>}</article></React.Fragment>)}</div></>}
    <form className="chat-composer" onSubmit={send}><label className="sr-only" htmlFor="chat-message">메시지</label><textarea id="chat-message" ref={composer} value={draft} disabled={!ready || busy} maxLength={8192} placeholder={ready?'메시지를 입력하세요.':'대화를 연결하면 메시지를 입력할 수 있어요.'} onChange={e=>{setDraft(e.target.value);messageId.current=null;}} onKeyDown={e=>{if(e.key==='Enter' && !e.shiftKey && !e.nativeEvent.isComposing && e.keyCode!==229){e.preventDefault();e.currentTarget.form.requestSubmit();}}}/><div className="chat-send"><small>Enter 전송 · Shift+Enter 줄바꿈</small>{generating?<button type="button" onClick={()=>action('cancel')} disabled={busy || chat.status==='cancelling'}>응답 중단</button>:<button className="primary" disabled={!ready || busy || !draft.trim()}>전송</button>}</div></form>
    {chat && !connected && <div className="chat-delete">{deleteConfirm?<><span>이 컴퓨터의 대화 기록을 삭제할까요?</span><button className="danger" onClick={()=>action('delete')} disabled={busy}>삭제 확인</button><button onClick={()=>setDeleteConfirm(false)}>취소</button></>:<button className="text-button danger" onClick={()=>setDeleteConfirm(true)}>대화 기록 삭제</button>}</div>}
  </section>;
}
