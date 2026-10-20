import React, {useEffect, useRef, useState} from 'react';
import {api, bytes} from './api';

export default function DocumentPanel({models, onJobs}) {
  const [library,setLibrary]=useState({documents:[],answers:[],settings:null});
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
  const request=useRef(null);
  const restored=useRef(false);
  const options=models.filter(x=>x.exists && x.engines.some(e=>['ollama','lmstudio'].includes(e)));
  const chosen=options.find(x=>x.id===model);
  async function refresh(){
    const value=await api('/api/documents');setLibrary(value);
    setSelected(old=>old.filter(id=>value.documents.some(x=>x.id===id)));
    return value;
  }
  function applyChoice(settings,announce){
    const saved=settings?.selected;
    if(!saved)return;
    const next=options.find(x=>x.id===saved.model_id && x.engines.includes(saved.engine));
    if(next){setModel(next.id);setEngine(saved.engine);}
    else if(announce)setNotice(`이전에 고른 모델(${saved.filename})을 지금은 쓸 수 없어요. 내 모델에서 연결을 확인하거나 다른 모델을 골라 주세요.`);
  }
  useEffect(()=>{refresh().catch(e=>setError(e.message));},[]);
  useEffect(()=>{
    // Restore the remembered choice once both the library and model list exist.
    if(restored.current || !library.settings || !models.length)return;
    restored.current=true;
    applyChoice(library.settings,true);
    const ids=library.settings.document_ids || [];
    setSelected(ids.filter(id=>library.documents.some(x=>x.id===id)));
  },[library,models]);
  async function remember(body){
    try{
      const value=await api('/api/documents/settings',{...body,confirmed:true});
      if(value.change)setNotice(value.change);
      return value;
    }catch(e){setError(e.message);}
  }
  function chooseModel(id){
    const next=options.find(x=>x.id===id);
    const nextEngine=next?.engines.includes('ollama')?'ollama':'lmstudio';
    setModel(id);setEngine(nextEngine);setError('');setNotice('');
    if(next)remember({model_id:id,engine:nextEngine,document_ids:selected});
  }
  function chooseEngine(value){
    setEngine(value);setError('');setNotice('');
    if(model)remember({model_id:model,engine:value,document_ids:selected});
  }
  function toggleDocument(id,checked){
    const next=checked?[...selected,id]:selected.filter(x=>x!==id);
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
        if(!record)throw new Error('작업 기록을 찾지 못했어요. 작업 탭에서 확인해 주세요.');
        if(['completed','failed','interrupted','cancelled'].includes(record.status)){
          setJob(null);setBusy(false);request.current=null;
          if(record.status!=='completed'){
            setError(record.message || '작업을 완료하지 못했어요.');
            // A failed new model may have been rolled back to the previous one.
            const value=await refresh();applyChoice(value.settings,false);
          }
          else {setNotice('저장된 결과를 확인했어요.');await refresh();}
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
  async function upload(event){
    event.preventDefault();
    if(!file)return;
    if(file.size>6*1024*1024){setError('문서는 6 MiB까지 등록할 수 있어요.');return;}
    const data=new Uint8Array(await file.arrayBuffer());
    let raw='';for(let i=0;i<data.length;i+=32768)raw+=String.fromCharCode(...data.subarray(i,i+32768));
    await run('/api/documents',{name:file.name,content:btoa(raw),replace});
  }
  async function remove(document){
    if(!window.confirm(`${document.name}의 저장된 텍스트와 색인을 삭제할까요? 이전 문서 답변 기록도 지워져요. 원본 파일은 유지돼요.`))return;
    await run('/api/documents/delete',{document_id:document.id});
  }
  return <div className="documents-panel">
    <section className="document-intro"><h2>내 자료에서 답을 찾아요</h2><p>문서와 질문은 이 컴퓨터에 저장되고 선택한 로컬 실행 앱에서 처리돼요.</p><p>텍스트 PDF · TXT · Markdown · HTML · HWPX, 파일당 6 MiB / PDF 100쪽. 스캔 PDF(OCR)와 HWP(한글 97 형식)는 아직 지원하지 않아요.</p></section>
    {error && <div className="error" role="alert">{error}</div>}{notice && <p className="notice" role="status">{notice}</p>}
    <form className="document-upload" onSubmit={upload}><label>문서 선택<input type="file" accept=".pdf,.txt,.md,.markdown,.html,.htm,.hwpx" disabled={busy} onChange={e=>setFile(e.target.files?.[0] || null)}/></label><label className="document-check"><input type="checkbox" checked={replace} disabled={busy} onChange={e=>setReplace(e.target.checked)}/>같은 이름의 문서 교체</label><button className="primary" disabled={!file || busy}>{busy?'처리 중…':'문서 등록'}</button></form>
    <p className="footnote">교체·삭제하면 이전 문서 답변 기록을 지워요. 등록 실패 시 기존 문서는 보존돼요.</p>
    <section className="document-library"><h2>등록된 문서 <small>{library.documents.length}개 · 질문할 문서를 선택하세요</small></h2>
      {!library.documents.length && <p className="empty">아직 등록한 문서가 없어요. 위에서 파일을 선택해 주세요.</p>}
      {library.documents.map(d=><article key={d.id}><label className="document-check"><input type="checkbox" checked={selected.includes(d.id)} disabled={busy} onChange={e=>toggleDocument(d.id,e.target.checked)}/><span><strong>{d.name}</strong><small>{bytes(d.size_bytes)} · {d.pages}쪽 · 검색 구간 {d.chunks}개</small>{d.index_current===false && <small className="warning">검색 색인 방식이 바뀌었어요. 원본 파일을 '같은 이름의 문서 교체'로 다시 등록해 주세요.</small>}</span></label><button className="danger" disabled={busy} onClick={()=>remove(d)}>삭제</button></article>)}
    </section>
    <form className="document-question" onSubmit={e=>{e.preventDefault();run('/api/documents/question',{question,document_ids:selected,model_id:model,engine});}}>
      <h2>문서에 질문하기</h2><div className="document-model"><label>답변할 모델<select value={model} disabled={busy} onChange={e=>chooseModel(e.target.value)}><option value="">내 모델에서 선택</option>{options.map(m=><option key={m.id} value={m.id}>{m.filename}</option>)}</select></label><label>실행 앱<select value={engine} disabled={busy} onChange={e=>chooseEngine(e.target.value)}>{(chosen?.engines || ['ollama']).filter(e=>['ollama','lmstudio'].includes(e)).map(e=><option key={e} value={e}>{e==='ollama'?'Ollama':'LM Studio'}</option>)}</select></label></div>
      {!options.length && <p>연결된 모델이 없어요. 내 모델에서 Ollama 또는 LM Studio 연결을 먼저 확인해 주세요.</p>}
      <label>질문<textarea rows={3} maxLength={600} value={question} disabled={busy} onChange={e=>setQuestion(e.target.value)} placeholder="예: 이 문서에서 안내하는 준비물은 무엇인가요?"/></label>
      <p className="footnote">선택한 문서의 단어를 검색해 근거를 찾아요. 이미 로딩된 모델이 있으면 기존 작업을 보존하고 중단해요.</p>
      <p className="footnote">고른 모델·실행 앱·문서는 다음에 열 때도 기억해요. 모델을 바꿔도 등록한 문서와 검색 색인은 그대로 쓰고, 새 모델이 첫 질문에서 실패하면 이전에 답변했던 모델로 선택을 되돌려요.</p>
      <button className="primary" disabled={busy || !model || !selected.length || question.trim().length<2}>{busy?'처리 중…':'근거를 찾아 질문하기'}</button>
    </form>
    <section className="document-answers" aria-live="polite"><h2>문서 답변 기록</h2><p className="footnote">검색 구간과 모델 답변을 함께 확인하세요. 출처 번호가 있어도 답변의 정확도를 보장하지 않아요.</p>
    {library.answers.map(a=><article key={a.id}><h3>{a.question}</h3><p className="document-answer">{a.answer}</p><small>{a.status==='answered'?'로컬 모델 답변':a.status==='no_evidence'?'근거 없음':'출처 검증 필요'} · {a.filename} · {new Date(a.created_at).toLocaleString('ko-KR')}</small>{a.sources.map(s=><details key={s.citation} open><summary>[{s.citation}] {s.name} · {s.page}쪽 · {s.start}–{s.end}자</summary><blockquote>{s.text}</blockquote></details>)}</article>)}
    </section>
  </div>;
}
