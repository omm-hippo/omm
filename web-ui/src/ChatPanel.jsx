import {RecoveryActions} from './Usability';
import React, {useEffect,useRef,useState} from 'react';
import {api} from './api';
import RichText, {CopyButton} from './RichText';
import {shouldSend} from './workspace-state.js';
import {L} from './i18n.js';

const activeStates=['starting','ready','generating','cancelling','closing','release_failed'];
const pollingStates=['starting','generating','cancelling','closing'];
const names={ollama:'Ollama',lmstudio:'LM Studio'};
const labels={starting:L('모델 준비 중','Preparing model'),ready:L('대화 연결됨','Connected'),generating:L('응답 생성 중','Generating reply'),cancelling:L('응답 중단 중','Stopping reply'),closing:L('연결 종료 중','Disconnecting'),closed:L('대화 종료됨','Chat ended'),failed:L('연결 실패','Connection failed'),interrupted:L('다시 연결 필요','Reconnect needed'),release_failed:L('모델 상태 확인 필요','Check model status')};

export default function ChatPanel({models,initialModel,preparedTask,onModelStatusChange,onModels,onRecover}) {
  const [history,setHistory]=useState([]);
  const [chat,setChat]=useState(null);
  const [modelId,setModelId]=useState(initialModel?.id || '');
  const [engine,setEngine]=useState(initialModel?.engines.find(x=>names[x]) || 'ollama');
  const [draft,setDraft]=useState('');
  const [error,setError]=useState('');
  const [busy,setBusy]=useState(false);
  const [initializing,setInitializing]=useState(true);
  const [deleteConfirm,setDeleteConfirm]=useState(false);
  const preparedId=useRef(null),pendingPrepared=useRef(null);
  const openId=useRef(null),messageId=useRef(null),sequence=useRef(0);
  const transcript=useRef(null),follow=useRef(true),composer=useRef(null),composing=useRef(false);
  const [sendMode,setSendMode]=useState('enter');
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

  async function start(prepared=null) {
    let preparation=prepared?.model_id ? prepared : pendingPrepared.current;
    if(!prepared?.model_id && preparation && (preparation.model_id!==modelId || preparation.engine!==engine)){preparation=null;openId.current=null;}
    pendingPrepared.current=preparation;
    const target=preparation ? {id:preparation.model_id} : selected;
    if(!target || busy || initializing)return;
    setBusy(true);setError('');openId.current ||= crypto.randomUUID();
    const existing=!preparation && chat && !connected;
    try {
      const value=await api('/api/chats',{id:target.id,engine:preparation?.engine || engine,confirmed:true,request_id:openId.current,...(existing?{chat_id:chat.id}:{})});
      setChat(value);openId.current=null;pendingPrepared.current=null;follow.current=true;await readHistory();
    }catch(e){setError(e.message);}finally{setBusy(false);}
  }
  useEffect(()=>{
    if(!preparedTask || initializing || busy || preparedId.current===preparedTask.id)return;
    if(connected){
      preparedId.current=preparedTask.id;
      if(chat.filename!==preparedTask.filename || chat.engine!==preparedTask.engine)setError(L('현재 대화를 종료한 뒤 준비된 모델을 열어 주세요.','End the current chat, then open the prepared model.'));
      return;
    }
    preparedId.current=preparedTask.id;openId.current=preparedTask.id;
    setModelId(preparedTask.model_id);setEngine(preparedTask.engine);start(preparedTask);
  },[preparedTask?.id,initializing,busy,Boolean(connected)]);
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
    <div className="chat-controls"><label className="field">{L('대화 기록','Chat history')}<select aria-label={L('대화 기록','Chat history')} value={chat?.id || ''} disabled={Boolean(connected) || busy || initializing} onChange={e=>openHistory(e.target.value)}><option value="">{L('새 대화','New chat')}</option>{history.map(row=><option key={row.id} value={row.id}>{row.filename} · {new Date(row.updated_at*1000).toLocaleString(L('ko-KR','en-US'))}</option>)}</select></label>{chat && !connected && <button onClick={()=>openHistory('')}>{L('새 대화','New chat')}</button>}{chat && <button onClick={exportChat} disabled={busy}>{L('대화 JSON 저장','Save chat as JSON')}</button>}</div>
    {error && <p className="error" role="alert">{error}</p>}
    {!connected && <div className="chat-connect"><div className="chat-selectors"><label className="field">{L('모델','Model')}<select aria-label={L('대화 모델','Chat model')} disabled={Boolean(chat) || busy} value={modelId} onChange={e=>{setModelId(e.target.value);openId.current=null;}}><option value="">{L('모델 선택','Choose a model')}</option>{eligible.map(row=><option value={row.id} key={row.id}>{row.filename}</option>)}</select></label><label className="field">{L('실행 앱','Runner')}<select aria-label={L('대화 실행 앱','Chat runner')} value={engine} disabled={Boolean(chat) || busy} onChange={e=>{setEngine(e.target.value);openId.current=null;}}>{linked.map(key=><option key={key} value={key}>{names[key]}</option>)}</select></label></div><p>{L('선택한 로컬 모델과 대화해요. 질문과 답변은 이 컴퓨터에 저장되며 외부 AI 서비스로 보내지 않아요.','Chat with the local model you choose. Questions and answers are saved on this computer and never sent to an outside AI service.')}</p><button className="primary" disabled={!selected || !linked.includes(engine) || busy || initializing} onClick={start}>{busy?L('연결 요청 중…','Connecting…'):chat?L('이 대화 다시 연결','Reconnect this chat'):L('대화 시작','Start chat')}</button>{!eligible.length && <p>{L('Ollama 또는 LM Studio에 연결된 모델이 필요해요.','You need a model linked to Ollama or LM Studio.')} <button className="text-button inline-button" onClick={onModels}>{L('내 모델에서 연결하기','Link one in My models')}</button></p>}</div>}
    {!chat && <div className="chat-welcome"><h2>{L('무엇부터 이야기할까요?','What would you like to talk about?')}</h2><p>{L('설치한 모델로 대화하고, 답변을 이 컴퓨터에 남기세요.','Chat with an installed model and keep the answers on this computer.')}</p><div className="chat-starters">{[[L('개념 이해하기','Understand a concept'),L('이해하고 싶은 개념을 예시와 함께 설명해 줘. 개념은 ','Explain this concept with examples. The concept is ')],[L('글 다듬기','Polish writing'),L('다음 글의 의미를 유지하면서 자연스럽게 다듬어 줘.\n\n','Polish the following text so it reads naturally, keeping its meaning.\n\n')],[L('코드 살펴보기','Explain code'),L('다음 코드가 어떻게 동작하는지 설명해 줘.\n\n','Explain how the following code works.\n\n')]].map(([label,text])=><button key={label} onClick={()=>{setDraft(text);messageId.current=null;composer.current?.focus();}}>{label}</button>)}</div></div>}{chat && <><div className="chat-heading"><div><h2>{chat.filename}</h2><p>{names[chat.engine]} · <span role="status">{labels[chat.status]}</span></p></div>{connected && <button onClick={()=>action('end')} disabled={busy || chat.status==='closing'}>{chat.loaded_by_omm===false?L('대화 연결 종료','Disconnect chat'):L('모델 종료','Stop model')}</button>}</div>{chat.notice && <p className="notice">{chat.notice}</p>}{chat.status==='failed' && <RecoveryActions reason={chat.failure?.reason} engine={chat.engine} onRecover={onRecover}/>}<div className="chat-transcript" ref={transcript} role="log" aria-label={L('대화 내용','Conversation')} aria-live="polite" onScroll={e=>{const el=e.currentTarget;follow.current=el.scrollHeight-el.scrollTop-el.clientHeight<80;}}>{!chat.turns.length && <p className="chat-empty">{L('궁금한 내용을 입력해 보세요.','Type a question to get started.')}</p>}{chat.turns.map(turn=><React.Fragment key={turn.id}><article className="chat-message user-message"><strong>{L('나','You')}</strong><div>{turn.user}</div></article><article className="chat-message assistant-message"><strong>{L('모델','Model')}</strong><RichText text={turn.assistant || (turn.status==='generating'?L('응답을 기다리고 있어요.','Waiting for a reply.'):L('응답이 없어요.','No reply.'))}/>{turn.assistant && <CopyButton text={turn.assistant} label={L('답변 복사','Copy answer')}/>}{turn.status==='cancelled' && <small>{L('중단된 응답 · 다음 질문의 문맥에 포함하지 않아요.','Stopped reply · not included as context for the next question.')}</small>}{turn.status==='interrupted' && <small>{L('서버가 종료되기 전의 일부 응답이에요.','Partial reply from before the server stopped.')}</small>}{turn.status==='failed' && <><small role="alert">{turn.error}</small><RecoveryActions reason={turn.failure?.reason} engine={chat.engine} onRecover={onRecover}/></>}{turn.finish_reason==='length' && <small>{L('응답 길이 제한에 도달했어요.','Reply hit the length limit.')}</small>}</article></React.Fragment>)}</div></>}
    <form className="chat-composer" onSubmit={send}><label className="sr-only" htmlFor="chat-message">{L('메시지','Message')}</label><textarea id="chat-message" ref={composer} value={draft} disabled={busy || Boolean(connected && !ready)} maxLength={8192} placeholder={ready?L('메시지를 입력하세요.','Type a message.'):L('질문을 미리 적고, 위에서 모델을 연결하세요.','Write your question now, then connect a model above.')} onChange={e=>{setDraft(e.target.value);messageId.current=null;}} onCompositionStart={()=>{composing.current=true;}} onCompositionEnd={()=>{composing.current=false;}} onKeyDown={e=>{if(!composing.current && shouldSend(e) && (sendMode==='enter' || e.metaKey || e.ctrlKey)){e.preventDefault();e.currentTarget.form.requestSubmit();}}}/><div className="chat-send"><label className="send-preference"><span className="sr-only">{L('전송 키','Send key')}</span><select aria-label={L('전송 키','Send key')} value={sendMode} onChange={e=>setSendMode(e.target.value)}><option value="enter">{L('Enter 전송','Enter to send')}</option><option value="modifier">{L('⌘/Ctrl+Enter 전송','⌘/Ctrl+Enter to send')}</option></select><small>{sendMode==='enter'?L('Shift+Enter 줄바꿈','Shift+Enter for new line'):L('Enter 줄바꿈','Enter for new line')}</small></label>{generating?<button type="button" onClick={()=>action('cancel')} disabled={busy || chat.status==='cancelling'}>{L('응답 중단','Stop reply')}</button>:<button className="primary" disabled={!ready || busy || !draft.trim()}>{L('전송','Send')}</button>}</div></form>
    {chat && !connected && <div className="chat-delete">{deleteConfirm?<><span>{L('이 컴퓨터의 대화 기록을 삭제할까요?','Delete this chat from this computer?')}</span><button className="danger" onClick={()=>action('delete')} disabled={busy}>{L('삭제 확인','Confirm delete')}</button><button onClick={()=>setDeleteConfirm(false)}>{L('취소','Cancel')}</button></>:<button className="text-button danger" onClick={()=>setDeleteConfirm(true)}>{L('대화 기록 삭제','Delete chat')}</button>}</div>}
  </section>;
}
