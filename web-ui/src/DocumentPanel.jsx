import React, {useEffect, useRef, useState} from 'react';
import {api, bytes} from './api';
import RichText from './RichText';
import {documentProgress} from './document-progress.js';
import {L} from './i18n.js';
import {AgentSection, ConnectDialog, LocationBar, SharePanel, TeamSection, TokenSection} from './RagSharing';

const METHOD_LABEL={hybrid:L('의미+단어','Semantic + keyword'),lexical:L('단어만','Keyword only')};
function methodLabel(value){return METHOD_LABEL[value] || L('단어만','Keyword only');}

export function DocumentProgress({job}){
  const step=documentProgress(job);
  if(!step)return null;
  return <div className="document-progress" role="status"><span>{step.label}</span>{step.max?<progress value={step.value} max={step.max} aria-label={step.label}/>:<progress aria-label={step.label}/>}</div>;
}

function CollectionList({rows,current,onSelect,onCreate,busy}){
  const [open,setOpen]=useState(false);
  const [name,setName]=useState('');
  return <section className="rag-collections" aria-label={L('컬렉션','Collections')}><div className="rag-collections-heading"><h2>{L('컬렉션','Collections')}</h2><button disabled={busy} onClick={()=>setOpen(!open)}>{L('+ 새 컬렉션','+ New collection')}</button></div>
    {open && <form className="rag-inline-form" onSubmit={async e=>{e.preventDefault();if(await onCreate(name.trim())){setName('');setOpen(false);}}}><input className="rag-input" aria-label={L('새 컬렉션 이름','New collection name')} value={name} disabled={busy} onChange={e=>setName(e.target.value)} placeholder={L('예: 인사규정','e.g. HR policies')} autoFocus/><button className="primary" disabled={busy || !name.trim()}>{L('만들기','Create')}</button></form>}
    {!rows.length && <p className="footnote">{L('볼 수 있는 컬렉션이 없어요. 새로 만들거나 공유를 받아 주세요.','No collections you can see. Create one or ask someone to share one.')}</p>}
    <ul>{rows.map(row=><li key={row.name}><button className={row.name===current?'selected':''} aria-pressed={row.name===current} onClick={()=>onSelect(row.name)}><strong>{row.name}</strong><small>{L(`문서 ${row.documents}개 · 만든 사람 ${row.owner}`,`${row.documents} documents · Created by ${row.owner}`)}</small><small>{L(`내 역할: ${row.role}`,`My role: ${row.role}`)}</small></button></li>)}</ul>
  </section>;
}

export default function DocumentPanel({models, onJobs}) {
  const [rag,setRag]=useState(null);
  const [rows,setRows]=useState([]);
  const [current,setCurrent]=useState('');
  const [docs,setDocs]=useState([]);
  const [library,setLibrary]=useState({answers:[],settings:null});
  const [selected,setSelected]=useState([]);
  const [file,setFile]=useState(null);
  const [replace,setReplace]=useState(false);
  const [question,setQuestion]=useState('');
  const [model,setModel]=useState('');
  const [engine,setEngine]=useState('ollama');
  const [error,setError]=useState('');
  const [notice,setNotice]=useState('');
  const [busy,setBusy]=useState(false);
  const [job,setJob]=useState(null);
  const [live,setLive]=useState(null);
  const [hits,setHits]=useState(null);
  const [connecting,setConnecting]=useState(false);
  const [lists,setLists]=useState({people:[],teams:[]});
  const request=useRef(null);
  const restored=useRef(false);
  const fileInput=useRef(null);
  const remote=rag?.mode==='remote';
  const collection=rows.find(x=>x.name===current);
  const options=models.filter(x=>x.exists && x.engines.some(e=>['ollama','lmstudio'].includes(e)));
  const chosen=options.find(x=>x.id===model);
  const embedding=library.settings?.index?.embedding_model || 'bge-m3';
  const askable=selected.filter(id=>docs.some(d=>d.id===id));

  async function loadState(){const value=await api('/api/rag/state');setRag(value);return value;}
  async function loadCollections(prefer){
    const value=await api('/api/rag/collections');setRows(value.collections);
    setCurrent(old=>{const want=prefer ?? old;return value.collections.some(x=>x.name===want)?want:(value.collections[0]?.name || '');});
    return value.collections;
  }
  async function loadLibrary(){const value=await api('/api/documents');setLibrary({answers:value.answers,settings:value.settings});return value;}
  async function reloadAll(prefer){
    setError('');setHits(null);
    const state=await loadState();
    if(state.error){setRows([]);setDocs([]);setError(state.error);return;}
    await loadCollections(prefer);
    if(state.mode==='local')await loadLibrary();
  }
  useEffect(()=>{reloadAll().catch(e=>setError(e.message));},[]);
  useEffect(()=>{
    if(!current){setDocs([]);return;}
    let live=true;
    api(`/api/rag/documents?collection=${encodeURIComponent(current)}`).then(value=>{if(live)setDocs(value.documents);}).catch(e=>{if(live)setError(e.message);});
    return()=>{live=false;};
  },[current,rows]);
  useEffect(()=>{
    // A collection's question starts with that collection's documents.
    if(remote || !docs.length)return;
    const saved=(library.settings?.document_ids || []).filter(id=>docs.some(d=>d.id===id));
    setSelected(saved.length?saved:docs.slice(0,20).map(d=>d.id));
  },[docs]);
  function applyChoice(settings,announce){
    const saved=settings?.selected;
    if(!saved)return;
    const next=options.find(x=>x.id===saved.model_id && x.engines.includes(saved.engine));
    if(next){setModel(next.id);setEngine(saved.engine);}
    else if(announce)setNotice(L(`이전에 고른 모델(${saved.filename})을 지금은 쓸 수 없어요. 내 모델에서 연결을 확인하거나 다른 모델을 골라 주세요.`,`The model you chose before (${saved.filename}) isn't available now. Check its link in My models or choose another model.`));
  }
  useEffect(()=>{
    if(restored.current || !library.settings || !models.length)return;
    restored.current=true;
    applyChoice(library.settings,true);
  },[library,models]);
  async function remember(body){
    try{
      const value=await api('/api/documents/settings',{...body,confirmed:true});
      if(value.change)setNotice(value.change);
      if(value.settings)setLibrary(old=>({...old,settings:value.settings}));
      return value;
    }catch(e){setError(e.message);}
  }
  function chooseModel(id){
    const next=options.find(x=>x.id===id);
    const nextEngine=next?.engines.includes('ollama')?'ollama':'lmstudio';
    setModel(id);setEngine(nextEngine);setError('');setNotice('');
    if(next)remember({model_id:id,engine:nextEngine,document_ids:askable});
  }
  function chooseEngine(value){
    setEngine(value);setError('');setNotice('');
    if(model)remember({model_id:model,engine:value,document_ids:askable});
  }
  function toggleDocument(id,checked){
    const next=checked?[...askable,id]:askable.filter(x=>x!==id);
    setSelected(next);remember({document_ids:next});
  }
  useEffect(()=>{
    if(!job) return;
    let live=true, timer;
    async function poll(){
      try{
        const history=await api('/api/jobs'); if(!live)return;
        onJobs(history);
        const record=history.find(x=>x.id===job);
        if(!record)throw new Error(L('작업 기록을 찾지 못했어요. 작업 탭에서 확인해 주세요.','Couldn\'t find the job record. Check the Jobs tab.'));
        setLive(record);
        if(['completed','failed','interrupted','cancelled'].includes(record.status)){
          setJob(null);setLive(null);setBusy(false);request.current=null;
          if(record.status!=='completed'){
            setError(record.message || L('작업을 완료하지 못했어요.','The job didn\'t finish.'));
            if(!remote){const value=await loadLibrary();applyChoice(value.settings,false);}
          }
          else {
            const result=record.result || {};
            setNotice(record.operation==='document_import'?L(`${result.name || '문서'}을(를) 등록했어요${result.unchanged?' (변경 없음)':''}.${result.semantic_notice?` ${result.semantic_notice}`:''}`,`Imported ${result.name || 'document'}${result.unchanged?' (no changes)':''}.${result.semantic_notice?` ${result.semantic_notice}`:''}`):L('저장된 결과를 확인했어요.','The saved result is ready.'));
            if(record.operation==='document_import'){setFile(null);if(fileInput.current)fileInput.current.value='';}
            await loadCollections();if(!remote)await loadLibrary();
          }
          return;
        }
      }catch(e){if(live)setError(e.message);}
      if(live)timer=setTimeout(poll,1000);
    }
    poll();return()=>{live=false;clearTimeout(timer);};
  },[job]);
  async function run(path,body){
    setError('');setNotice('');setBusy(true);
    // Preserve the exact UUID/payload after an uncertain transport result.
    const signature=JSON.stringify({path,body});
    if(request.current?.signature!==signature)request.current={signature,id:crypto.randomUUID()};
    try{
      const value=await api(path,{...body,confirmed:true,request_id:request.current.id});
      setJob(value.id);onJobs(await api('/api/jobs'));
    }catch(e){setError(e.message);setBusy(false);}
  }
  async function direct(path,body,done){
    setError('');setNotice('');setBusy(true);
    try{const value=await api(path,body);if(done)setNotice(done(value));return value;}
    catch(e){setError(e.message);return null;}
    finally{setBusy(false);}
  }
  async function upload(event){
    event.preventDefault();
    if(!file || !current)return;
    if(file.size>6*1024*1024){setError(L('문서는 6 MiB까지 등록할 수 있어요.','Documents can be up to 6 MiB.'));return;}
    const data=new Uint8Array(await file.arrayBuffer());
    let raw='';for(let i=0;i<data.length;i+=32768)raw+=String.fromCharCode(...data.subarray(i,i+32768));
    await run('/api/rag/upload',{collection:current,name:file.name,content:btoa(raw),replace});
  }
  async function remove(document){
    if(!window.confirm(L(`'${current}' 컬렉션의 '${document.name}' 문서와 색인을 지울까요? 이전 문서 답변 기록도 지워져요. 원본 파일은 그대로예요.`,`Delete the document '${document.name}' and its index from the '${current}' collection? Past document answers are deleted too. The original file stays as is.`)))return;
    if(await direct('/api/rag/documents/remove',{collection:current,name:document.name,confirmed:true},()=>L(`'${document.name}' 문서를 지웠어요. 이전 문서 답변 기록도 지웠어요.`,`Deleted '${document.name}' and its past document answers.`))){await loadCollections();if(!remote)await loadLibrary();}
  }
  async function createCollection(name){
    const value=await direct('/api/rag/collections/create',{name},v=>L(`'${v.name}' 컬렉션을 만들었어요. 문서를 넣어 주세요.${remote?' 지금은 나만 검색할 수 있어요. 아래 공유에서 사람이나 팀을 더해요.':''}`,`Created the '${v.name}' collection. Add documents to it.${remote?' Only you can search it for now. Add people or teams under Sharing below.':''}`));
    if(value)await loadCollections(value.name);
    return !!value;
  }
  async function deleteCollection(){
    if(!window.confirm(L(`'${current}' 컬렉션과 문서 ${collection?.documents ?? 0}개의 저장된 텍스트·색인을 지울까요? 원본 파일은 그대로예요.`,`Delete the '${current}' collection and the saved text and index of its ${collection?.documents ?? 0} documents? The original files stay as is.`)))return;
    if(await direct('/api/rag/collections/delete',{name:current,confirmed:true},v=>L(`'${v.name}' 컬렉션을 지웠어요 (문서 ${v.documents}개). 이전 문서 답변 기록도 지웠어요.`,`Deleted the '${v.name}' collection (${v.documents} documents) and its past document answers.`)))await loadCollections('');
  }
  async function backToLocal(){
    if(!window.confirm(L('회사 head 연결과 저장한 출입증(토큰)을 이 PC에서 지우고 이 PC의 문서로 돌아갈까요?','Remove the company head connection and the saved pass (token) from this PC, and go back to this PC\'s documents?')))return;
    if(await direct('/api/rag/remote/clear',{confirmed:true},()=>L('회사 연결과 토큰을 지웠어요. 이제 이 PC의 문서를 보여 줘요. AI 도구를 회사 head로 연결해 두었다면 아래 AI 연결에서 해제하거나 다시 연결해 주세요.','Removed the company connection and token. Now showing this PC\'s documents. If you connected AI tools to the company head, disconnect or reconnect them under AI connection below.')))await reloadAll('');
  }
  async function findSources(){
    setHits(null);
    const value=await direct('/api/rag/search',{collection:current,query:question});
    if(value)setHits(value);
  }
  async function ask(event){
    event.preventDefault();
    if(remote)return findSources();
    setHits(null);
    run('/api/documents/question',{question,document_ids:askable,model_id:model,engine});
  }
  const answers=library.answers.filter(a=>!a.sources?.length || a.sources.some(s=>!s.collection || s.collection===current));
  return <div className="documents-panel">
    <section className="document-intro"><h2>{L('내 자료에서 답을 찾아요','Find answers in your documents')}</h2><p>{remote?L('회사 head에 모인 문서 중 나에게 공유된 컬렉션을 보고, 내 컬렉션을 다른 사람·팀과 나눠요. 문서 원본과 색인은 회사 head에만 있어요.','See the collections shared with you on the company head, and share your own collections with people and teams. Original documents and indexes stay only on the company head.'):L('문서와 질문은 이 컴퓨터에 저장되고 선택한 로컬 실행 앱에서 처리돼요.','Documents and questions are saved on this computer and processed by the local runner you choose.')}</p><p>{L('텍스트 PDF · TXT · Markdown · HTML · HWPX, 파일당 6 MiB / PDF 100쪽. 스캔 PDF(OCR)와 HWP(한글 97 형식)는 아직 지원하지 않아요.','Text PDF · TXT · Markdown · HTML · HWPX, up to 6 MiB per file / 100 PDF pages. Scanned PDFs (OCR) and HWP (Hangul 97 format) aren\'t supported yet.')}</p></section>
    <LocationBar rag={rag} busy={busy} onConnect={()=>setConnecting(true)} onLocal={backToLocal}/>
    {connecting && <ConnectDialog onClose={()=>setConnecting(false)} onDone={async value=>{setConnecting(false);await reloadAll('');setNotice(L(`${value.url} 회사 head에 '${value.name}'(으)로 연결했어요. 토큰은 이 PC의 OMM 폴더에만 저장했어요. AI 도구도 회사 문서를 쓰게 하려면 아래 AI 연결에서 다시 연결해 주세요.`,`Connected to the company head at ${value.url} as '${value.name}'. The token is saved only in this PC's OMM folder. To let AI tools use company documents too, reconnect them under AI connection below.`));}}/>}
    {error && <div className="error" role="alert">{error}</div>}{notice && <p className="notice" role="status">{notice}</p>}
    <div className="rag-layout">
      <CollectionList rows={rows} current={current} busy={busy} onSelect={name=>{setCurrent(name);setHits(null);setError('');setNotice('');}} onCreate={createCollection}/>
      <div className="rag-detail">{!collection?<p className="empty">{L('왼쪽에서 컬렉션을 고르거나 새로 만들어 주세요.','Choose a collection on the left or create a new one.')}</p>:<>
        <div className="rag-detail-heading"><div><h2>{collection.name}</h2><small>{L(`문서 ${collection.documents}개 · 만든 사람 ${collection.owner} · 내 역할: ${collection.role}`,`${collection.documents} documents · Created by ${collection.owner} · My role: ${collection.role}`)}</small></div>{collection.can_manage && !collection.default && <button className="danger" disabled={busy} onClick={deleteCollection}>{L('컬렉션 삭제','Delete collection')}</button>}</div>
        {collection.can_manage?<><form className="document-upload" onSubmit={upload}><label>{L('문서 선택','Choose document')}<input ref={fileInput} type="file" accept=".pdf,.txt,.md,.markdown,.html,.htm,.hwpx" disabled={busy} onChange={e=>setFile(e.target.files?.[0] || null)}/></label><label className="document-check"><input type="checkbox" checked={replace} disabled={busy} onChange={e=>setReplace(e.target.checked)}/>{L('같은 이름의 문서 교체','Replace document with the same name')}</label><button className="primary" disabled={!file || busy}>{busy && live?.operation==='document_import'?L('처리 중…','Working…'):L('문서 등록','Import document')}</button></form>
          <DocumentProgress job={live}/>
          <p className="footnote">{L('교체·삭제하면 이전 문서 답변 기록을 지워요. 등록 실패 시 기존 문서는 보존돼요.','Replacing or deleting removes past document answers. If an import fails, the existing document is kept.')}{remote?L(' 파일은 회사 head로 보내 그곳에서 텍스트를 뽑고 색인해요.',' Files are sent to the company head, which extracts the text and indexes it.'):''}</p></>
          :<p className="footnote">{L(`공유받은 컬렉션이에요. 검색만 할 수 있고 문서를 넣거나 지우는 일은 만든 사람(${collection.owner})이나 관리자가 해요.`,`This collection is shared with you. You can only search it; its creator (${collection.owner}) or an admin adds and deletes documents.`)}</p>}
        <section className="document-library"><h3>{L('문서','Documents')} <small>{L(`${docs.length}개`,`${docs.length}`)}{!remote && docs.length>0?L(' · 질문할 문서를 선택하세요',' · Select documents to ask about'):''}</small></h3>
          {!docs.length && <p className="footnote">{L('아직 이 컬렉션에 문서가 없어요.','No documents in this collection yet.')}</p>}
          {docs.map(d=><article key={d.id || d.name}><label className="document-check">{!remote && <input type="checkbox" checked={askable.includes(d.id)} disabled={busy} onChange={e=>toggleDocument(d.id,e.target.checked)}/>}<span><strong>{d.name}</strong><small>{bytes(d.size_bytes)} · {L(`${d.pages}쪽 · 검색 구간 ${d.chunks}개`,`${d.pages} pages · ${d.chunks} search chunks`)}</small>{d.index_current===false && <small className="warning">{L('검색 색인 방식이 바뀌었어요. 원본 파일을 \'같은 이름의 문서 교체\'로 다시 등록해 주세요.','The search index format changed. Import the original file again with \'Replace document with the same name\'.')}</small>}{d.semantic==='missing' && <small className="warning">{L('의미 색인이 없어 단어 검색만 해요.','No semantic index, so only keyword search works.')}{remote?L(' 회사 head 관리자에게 알려 주세요.',' Let the company head admin know.'):L(` Ollama에 ${embedding} 모델을 받은 뒤 omm rag reindex를 실행해 주세요.`,` Pull the ${embedding} model in Ollama, then run omm rag reindex.`)}</small>}{d.semantic==='stale' && <small className="warning">{L('임베딩 모델이 바뀌어 재색인이 필요해요.','The embedding model changed, so a reindex is needed.')}{remote?L(' 회사 head 관리자에게 알려 주세요.',' Let the company head admin know.'):L(' omm rag reindex를 실행해 주세요.',' Run omm rag reindex.')}</small>}</span></label>{collection.can_manage && <button className="danger" disabled={busy} onClick={()=>remove(d)}>{L('삭제','Delete')}</button>}</article>)}
        </section>
        {collection.can_manage && <SharePanel collection={collection} people={lists.people} teams={lists.teams} busy={busy} onError={setError} onChange={async text=>{setError('');setNotice(text);await loadCollections();}}/>}
        <form className="document-question" onSubmit={ask}>
          <h3>{L('질문하기','Ask a question')}</h3>
          {remote?<p className="footnote">{L(`회사 head 모드에서는 답변을 만들지 않고, '${collection.name}' 컬렉션에서 찾은 근거 구간을 원문 그대로 보여 줘요. 직접 읽고 판단하거나 AI 연결로 코딩 에이전트에게 맡겨 주세요.`,`In company head mode, no answer is generated. You see the source passages found in the '${collection.name}' collection, word for word. Read them yourself, or hand them to a coding agent through AI connection.`)}</p>
          :<><div className="document-model"><label>{L('답변할 모델','Answering model')}<select value={model} disabled={busy} onChange={e=>chooseModel(e.target.value)}><option value="">{L('내 모델에서 선택','Choose from My models')}</option>{options.map(m=><option key={m.id} value={m.id}>{m.filename}</option>)}</select></label><label>{L('실행 앱','Runner')}<select value={engine} disabled={busy} onChange={e=>chooseEngine(e.target.value)}>{(chosen?.engines || ['ollama']).filter(e=>['ollama','lmstudio'].includes(e)).map(e=><option key={e} value={e}>{e==='ollama'?'Ollama':'LM Studio'}</option>)}</select></label></div>
            {!options.length && <p>{L('연결된 모델이 없어요. 내 모델에서 Ollama 또는 LM Studio 연결을 먼저 확인해 주세요.','No linked models. First check the Ollama or LM Studio link in My models.')}</p>}</>}
          <label>{L('질문','Question')}<textarea rows={3} maxLength={600} value={question} disabled={busy} onChange={e=>setQuestion(e.target.value)} placeholder={L('예: 이 문서에서 안내하는 준비물은 무엇인가요?','e.g. What does this document say to bring?')}/></label>
          {!remote && <p className="footnote">{L(`선택한 문서에서 의미와 단어로 근거를 찾아요(로컬 Ollama 임베딩 모델 ${embedding}). 임베딩 모델을 쓸 수 없으면 단어 검색만 하고 답변 기록에 알려 줘요. 이미 로딩된 모델이 있으면 기존 작업을 보존하고 중단해요. 고른 모델·실행 앱·문서는 다음에 열 때도 기억해요.`,`Finds sources in the selected documents by meaning and keywords (local Ollama embedding model ${embedding}). If the embedding model isn't available, only keyword search runs and the answer history says so. If a model is already loaded, the existing work is kept and this stops. Your chosen model, runner, and documents are remembered next time.`)}</p>}
          <div className="row-actions rag-ask-actions"><button className="primary" disabled={busy || question.trim().length<2 || (remote?!docs.length:(!model || !askable.length))}>{busy?L('처리 중…','Working…'):remote?L('근거 찾기','Find sources'):L('근거를 찾아 질문하기','Find sources and ask')}</button>{!remote && <button type="button" disabled={busy || question.trim().length<2 || !docs.length} onClick={findSources}>{L('답변 없이 근거만 찾기','Find sources only, no answer')}</button>}</div>
        </form>
        {hits && <section className="document-answers" aria-live="polite"><h3>{L('찾은 근거','Sources found')}</h3><p className="footnote">{hits.message}{hits.method?L(` · 검색: ${methodLabel(hits.method)}${remote?' (회사 head)':''}`,` · Search: ${methodLabel(hits.method)}${remote?' (company head)':''}`):''}{L(` · 모델 답변 없이 '${collection.name}' 컬렉션 전체에서 찾은 원문 구간이에요.`,` · Original passages found across the whole '${collection.name}' collection, without a model answer.`)}</p>{hits.notice && <small className="warning">{hits.notice}</small>}
          {hits.results.map(r=><details key={r.rank} open><summary>[{r.rank}] {r.document} · {L(`${r.page}쪽 · ${r.start}–${r.end}자`,`p. ${r.page} · chars ${r.start}–${r.end}`)}</summary><blockquote>{r.quote}</blockquote></details>)}
          {hits.skipped_stale?.length>0 && <small className="warning">{L('색인을 다시 만들어야 해서 검색하지 않은 문서:','Not searched because the index needs rebuilding:')} {hits.skipped_stale.join(', ')}</small>}
        </section>}
        {!remote && <section className="document-answers" aria-live="polite"><h3>{L('문서 답변 기록','Document answer history')}</h3><p className="footnote">{L('검색 구간과 모델 답변을 함께 확인하세요. 출처 번호가 있어도 답변의 정확도를 보장하지 않아요.','Check the source passages together with the model\'s answer. Source numbers don\'t guarantee the answer is accurate.')}</p>
          {answers.map(a=><article key={a.id}><h3>{a.question}</h3><div className="document-answer"><RichText text={a.answer}/></div><small>{a.status==='answered'?L('로컬 모델 답변','Local model answer'):a.status==='no_evidence'?L('근거 없음','No sources'):L('출처 검증 필요','Sources need checking')} · {L('검색:','Search:')} {methodLabel(a.retrieval)} · {a.filename} · {new Date(a.created_at).toLocaleString(L('ko-KR','en-US'))}</small>{a.retrieval_notice && <small className="warning">{a.retrieval_notice}</small>}{a.sources.map(s=><details key={s.citation} open><summary>[{s.citation}] {s.name} · {L(`${s.page}쪽 · ${s.start}–${s.end}자`,`p. ${s.page} · chars ${s.start}–${s.end}`)}</summary><blockquote>{s.text}</blockquote></details>)}</article>)}
        </section>}
      </>}</div>
    </div>
    <AgentSection rag={rag} onRefresh={loadState}/>
    {rag?.admin && <TeamSection onPeople={value=>setLists({people:value.people,teams:value.teams.map(t=>t.name)})}/>}
    {rag?.admin && rag?.tokens && <TokenSection onChanged={()=>api('/api/rag/teams').then(value=>setLists({people:value.people,teams:value.teams.map(t=>t.name)})).catch(()=>{})}/>}
  </div>;
}
