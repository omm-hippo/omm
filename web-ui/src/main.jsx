import React, {useEffect, useRef, useState} from 'react';
import {createRoot} from 'react-dom/client';
import {api, gib, bytes} from './api';
import './style.css';

const profiles = [['balanced','다른 작업과 함께'],['dedicated','모델에 집중'],['minimal','가볍게 사용']];
const purposes = {General:'일반',Coding:'코딩',Reasoning:'추론·수학',Writing:'글쓰기',Translation:'번역',Documents:'문서', '—':'정보 없음'};
const operationLabels={install:'모델 설치',link:'실행 앱 연결',uninstall:'모델 삭제',verify:'모델 실행 확인'};
const statuses = {queued:'대기 중',running:'진행 중',cancelling:'중단 요청됨',completed:'완료',failed:'실패',cancelled:'중단됨',interrupted:'결과 확인 필요'};
const evidenceLabels = {static_rules:'메모리 규칙',insufficient:'실측 근거 부족',local_calibrated:'로컬 속도 보정',feature_support:'유사 특성 실측 있음',synthetic_prior:'합성 데이터 예측'};

function Evidence({catalog,onUpdate,busy}) {
  const source = {bundled_rules:'기본 메모리 규칙',signed_cache:'서명을 확인한 추천 자료',local_cache:'로컬 추천 자료'}[catalog?.catalog_source];
  const details = catalog?.catalog;
  return <section className="evidence"><div className="evidence-heading"><h2>추천 근거</h2><button onClick={onUpdate} disabled={busy}>{busy?'자료 받는 중…':'추천 자료 업데이트'}</button></div><p>{source || '자료 확인 중'}</p>{details?.trained_at && <p>학습 시점 {details.trained_at.slice(0,10)}{Number.isInteger(details.real_configuration_count) && ` · 전체 실측 구성 ${details.real_configuration_count}개`}</p>}<p>{catalog?.evidence?.reason}</p>{catalog?.evidence?.local_calibration_samples>0 && <p>로컬 보정 측정 {catalog.evidence.local_calibration_samples}회 · 예상 속도는 Ollama 기준이에요.</p>}<p>전체 자료의 실측 수는 내 컴퓨터에서의 정확도를 뜻하지 않아요.</p></section>;
}

function Icon({name}) {
  const paths = {wiki:<><path d="M3 4h7c2 0 2 2 2 2s0-2 2-2h7v16h-7c-2 0-2 2-2 2s0-2-2-2H3Z"/><path d="M12 6v16"/></>,recommend:<><path d="M3 10 12 3l9 7v11h-6v-7H9v7H3Z"/></>,models:<><path d="m12 3 9 5-9 5-9-5Z"/><path d="M3 8v10l9 5 9-5V8M12 13v10"/></>,jobs:<><path d="M8 5h13M8 12h13M8 19h13"/><circle cx="3" cy="5" r=".8"/><circle cx="3" cy="12" r=".8"/><circle cx="3" cy="19" r=".8"/></>,search:<><circle cx="10" cy="10" r="6"/><path d="m15 15 6 6"/></>};
  return <svg aria-hidden="true" viewBox="0 0 24 24" width="21" height="21" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">{paths[name]}</svg>;
}

function Hardware({machine, budget}) {
  const fields = [['CPU',machine?.cpu||'확인 중'],['메모리',machine ? `${gib(machine.ram_available_gb)} 사용 가능 / ${gib(machine.ram_total_gb)}` : '확인 중'],['GPU',machine?.gpu_name|| (machine ? 'CPU로 실행' : '확인 중')],['모델 예산',gib(budget ?? machine?.model_budget_gb)]];
  return <div className="hardware">{fields.map(([label,value])=><div key={label}><span>{label}</span><strong>{value}</strong></div>)}</div>;
}

function ModelTable({rows, onSelect, onDetails}) {
  return <div className="table-wrap"><table><thead><tr><th>모델</th><th>용도</th><th>예상 메모리</th><th>예측 근거</th><th><span className="sr-only">작업</span></th></tr></thead><tbody>{rows.map((model,index)=><tr key={model.id}><td><strong>{model.name}</strong><small>{model.quantization}</small><button className="text-button" onClick={()=>onDetails(model.wiki || {missing:true,name:model.name,ref:model.ref})}>모델 설명</button>{model.variant_warning && <small>{model.variant_warning}</small>}</td><td>{purposes[model.use_case] || model.use_case}</td><td>{gib(model.memory_required_gb)}</td><td><span>{evidenceLabels[model.evidence.status]}</span>{model.evidence.matching_feature_configurations && <small>같은 특성의 학습 구성 {model.evidence.matching_feature_configurations.training} · 검증 구성 {model.evidence.matching_feature_configurations.holdout}</small>}{typeof model.predicted_tokens_per_second==='number' && <small>약 {model.predicted_tokens_per_second.toFixed(1)} tok/s 예상 · Ollama</small>}</td><td><button className={index===0?'primary':''} onClick={()=>onSelect(model)}>{model.installed?'설치 정보':'설치'}</button></td></tr>)}</tbody></table></div>;
}

function Installed({rows,onAction}) {
  if (!rows.length) return <div className="empty"><h2>아직 설치한 모델이 없어요</h2><p>추천에서 모델을 선택하면 이곳에서 연결된 실행 앱과 저장 상태를 확인할 수 있어요.</p></div>;
  return <div className="installed-list">{rows.map(model=><article key={model.id}><div><h2>{model.filename}</h2><p>{bytes(model.size_bytes)} · {model.engines.length ? model.engines.join(', ') : '연결된 실행 앱 없음'}{!model.exists && ' · 파일 없음'}</p><small>{Object.entries(model.compatibility || {}).map(([engine,result])=>`${engine}: ${result.status==='passed'?'실행 확인 통과':'실행 확인 실패'}`).join(' · ') || '아직 모델 실행을 확인하지 않았어요.'}</small></div><div className="row-actions"><button onClick={()=>onAction({...model,operation:'verify'})} disabled={!model.exists || !model.engines.some(e=>['ollama','lmstudio'].includes(e))}>실행 확인</button><button onClick={()=>onAction({...model,operation:'link'})} disabled={!model.exists}>연결 확인</button><button className="danger" onClick={()=>onAction({...model,operation:'uninstall'})}>삭제</button></div></article>)}</div>;
}

function Jobs({jobs,onCancel}) {
  if (!jobs.length) return <div className="empty"><h2>진행한 작업이 없어요</h2><p>설치와 연결, 삭제 결과가 여기에 저장돼요.</p></div>;
  return <div className="job-list">{jobs.map(job=><article key={job.id}><div><div className="job-title"><h2>{job.filename}</h2><strong className={`status ${job.status}`}>{statuses[job.status]||job.status}</strong></div><p>{operationLabels[job.operation] || job.operation}</p>{job.bytes_received>0 && <p>받은 데이터 {bytes(job.bytes_received)}{job.total_bytes ? ` / 파일 크기 ${bytes(job.total_bytes)}` : ''}</p>}{job.message && <p className="job-message">{job.message}</p>}{job.result?.linked && <p>연결된 앱: {Object.entries(job.result.linked).filter(([,ok])=>ok).map(([name])=>name).join(', ') || '없음 — 내 모델에서 연결 상태를 확인해 주세요.'}</p>}{job.operation==='install' && job.result?.runtime_verified===false && <small>파일 설치 결과를 확인했어요. 실제 모델 실행은 아직 확인하지 않았어요.</small>}{job.result?.response && <div className="probe-response"><strong>실제 응답</strong><pre>{job.result.response}</pre></div>}{job.result?.released_test_load && <small>응답 확인 후 검증용 모델의 메모리를 해제했어요.</small>}{job.result?.model_was_preloaded && <small>이미 로딩되어 있던 모델과 설정을 유지했어요.</small>}</div>{job.operation==='install' && ['queued','running'].includes(job.status) && <button onClick={()=>onCancel(job.id)}>중단</button>}</article>)}</div>;
}

function Confirmation({model,machine,onClose,onSubmit,busy,error}) {
  const operation=model.operation || 'install';
  const available=(machine?.engines || []).filter(x=>x.installed);
  const engines=operation==='verify' ? available.filter(x=>['ollama','lmstudio'].includes(x.key) && model.engines.includes(x.key)) : available;
  const [engine,setEngine]=useState(()=>operation==='verify' ? engines[0]?.key || '' : '');
  const title={install:'모델을 설치할까요?',link:'실행 앱 연결을 확인할까요?',uninstall:'모델을 삭제할까요?',verify:'모델 실행을 확인할까요?'}[operation];
  const action={install:'설치 시작',link:'연결 확인',uninstall:'삭제',verify:'실행 확인'}[operation];
  const dialog=useRef(null);
  useEffect(()=>{dialog.current?.showModal();},[]);
  return <dialog ref={dialog} onCancel={event=>{if(busy)event.preventDefault();else onClose();}}>
    <form onSubmit={event=>{event.preventDefault();onSubmit(engine);}}>
      <h2>{title}</h2><p className="filename">{model.filename}</p>
      {error && <p role="alert" className="error">{error}</p>}
      {operation==='uninstall' ? <p>OMM이 관리하는 모델 파일과 기록된 연결을 정리해요. 다른 모델은 삭제하지 않아요.</p> : <>
        <p>{operation==='verify' ? '실행 앱의 로컬 서버가 켜져 있어야 해요. 모델을 메모리에 올려 짧은 실제 응답을 확인하고, 검증 때문에 새로 로딩한 모델만 해제해요. 이미 로딩된 모델은 유지해요.' : operation==='install' ? '파일을 내려받고 선택한 실행 앱에 연결해요. 모델을 자동으로 메모리에 올리거나 벤치마크를 전송하지 않아요.' : '기존 파일을 사용해 연결을 다시 확인해요.'}</p>
        <label>{operation==='verify'?'응답을 확인할 실행 앱':'연결할 실행 앱'}
          <select value={engine} onChange={event=>setEngine(event.target.value)}>
            {operation!=='verify' && <option value="">설치된 실행 앱 모두</option>}
            {engines.map(x=><option key={x.key} value={x.key}>{x.name}</option>)}
          </select>
        </label>
      </>}
      <div className="dialog-actions"><button type="button" disabled={busy} onClick={onClose}>취소</button><button className={operation==='uninstall'?'danger-fill':'primary'} disabled={busy || (operation==='verify'&&!engine)}>{busy?'요청 중…':action}</button></div>
    </form>
  </dialog>;
}

function FirstUse({machine,onModels}) {
  const available=(machine?.engines || []).filter(e=>e.installed && ['ollama','lmstudio'].includes(e.key));
  return <section className="first-use"><h2>처음 시작할 때</h2>
    <p>1. Ollama 또는 LM Studio를 준비하고 앱의 로컬 서버를 켜세요.</p>
    <p>2. 아래 추천에서 모델을 설치하고 연결할 실행 앱을 선택하세요.</p>
    <p>3. 내 모델의 실행 확인에서 첫 응답을 확인하세요.</p>
    {available.length ? <small>설치된 실행 앱: {available.map(e=>e.name).join(', ')}</small> : <a href="https://github.com/omm-hippo/omm#installation" target="_blank" rel="noreferrer">설치 안내 보기</a>}
    <button className="text-button" onClick={onModels}>내 모델 확인</button>
  </section>;
}

const wikiTasks={agents:'에이전트',coding:'코딩',reasoning:'추론',vision:'이미지',multilingual:'다국어',compact:'경량'};
function WikiList({document,onOpen}) {
  const [query,setQuery]=useState('');
  const rows=(document?.models || []).filter(x=>`${x.name} ${x.repository} ${x.summary}`.toLowerCase().includes(query.toLowerCase()));
  return <><p className="footnote">자료 {document?.contentVersion} · 검토 {document?.reviewedAt}. 설명은 개발사 자료와 편집 의견이며 OMM의 품질 순위가 아니에요.</p>
    <label className="search wiki-search"><Icon name="search"/><input value={query} onChange={event=>setQuery(event.target.value)} placeholder="모델 설명에서 찾기" aria-label="모델 설명에서 찾기"/></label>
    <div className="wiki-list">{rows.map(model=><article key={model.id}><div><h2>{model.name}</h2><p>{model.summary}</p><small>{model.tasks.map(t=>wikiTasks[t]).join(' · ')}</small></div><button onClick={()=>onOpen(model)}>설명 보기</button></article>)}</div>
    {!rows.length && <div className="empty">등록된 설명을 찾지 못했어요.</div>}
  </>;
}
function WikiDetail({model,onClose}) {
  const dialog=useRef(null);useEffect(()=>{dialog.current?.showModal();},[]);
  return <dialog className="wiki-dialog" ref={dialog} onCancel={onClose}><h2>{model.name}</h2>
    {model.missing ? <><p>이 설치 대상에 정확하게 연결된 위키 설명이 아직 없어요.</p><p className="filename">{model.ref}</p></> : <>
      <p>{model.summary}</p><h3>선택할 때</h3><p>{model.chooseWhen}</p>
      <h3>특징과 주의점</h3>{[...model.strengths,...model.cautions].map((claim,index)=><p key={index}><small>{claim.basis==='publisher'?'개발사 설명':'편집 의견'}</small>{claim.text}</p>)}
      <h3>실행 조건</h3><p>{model.runtimeNote}</p>
      {model.match==='declared_base_repository' && <p>원본 체크포인트 설명을 연결했어요. 변환본의 양자화·실행 앱 호환성은 따로 확인해야 해요.</p>}
      <p className="filename">{model.packageRepository || model.repository}</p>
      <h3>출처</h3>{model.sources.map(source=><p key={source.id}><a href={source.url} target="_blank" rel="noreferrer">{source.title}</a></p>)}
      <small>검토 {model.reviewedAt} · OMM 실측 품질 평가 없음</small>{model.stale && <p>검토 후 시간이 지난 자료예요. 현재 지원 여부는 출처에서 다시 확인해 주세요.</p>}
    </>}
    <div className="dialog-actions"><button onClick={onClose}>닫기</button></div>
  </dialog>;
}

function App() {
  const readTab = ()=>['recommend','models','jobs','wiki'].includes(location.hash.slice(1)) ? location.hash.slice(1) : 'recommend';
  const [tab,setCurrentTab] = useState(readTab);
  function setTab(value) {
    setCurrentTab(value);
    if(location.hash!==`#${value}`)history.pushState(null,'',`#${value}`);
  }
  useEffect(()=>{
    const update=()=>setCurrentTab(readTab());
    window.addEventListener('popstate',update);
    window.addEventListener('hashchange',update);
    return ()=>{window.removeEventListener('popstate',update);window.removeEventListener('hashchange',update);};
  },[]);
  const [machine,setMachine] = useState(null);
  const [profile,setProfile] = useState('balanced');
  const [query,setQuery] = useState('');
  const [catalog,setCatalog] = useState(null);
  const [models,setModels] = useState([]);
  const [jobs,setJobs] = useState([]);
  const [error,setError] = useState('');
  const [notice,setNotice] = useState('');
  const [wiki,setWiki]=useState(null);
  const [description,setDescription]=useState(null);
  const [loading,setLoading] = useState(true);
  const [selected,setSelected] = useState(null);
  const [submitting,setSubmitting] = useState(false);
  const [updating,setUpdating] = useState(false);
  const [refreshKey,setRefreshKey] = useState(0);
  const requestSequence = useRef(0);
  const operationId = useRef(null);
  const active = jobs.some(job=>['queued','running','cancelling'].includes(job.status));
  async function refresh(signal) {
    setError('');setLoading(true);
    const sequence = ++requestSequence.current;
    try {
      const [hw,recs,list,history] = await Promise.all([api('/api/hardware'),api(`/api/recommendations?profile=${profile}&q=${encodeURIComponent(query)}`),api('/api/models'),api('/api/jobs')]);
      if (signal?.aborted || sequence !== requestSequence.current) return;
      setMachine(hw);setCatalog(recs);setModels(list);setJobs(history);
    } catch(e){if(!signal?.aborted && sequence===requestSequence.current)setError(e.message);} finally {if(!signal?.aborted && sequence===requestSequence.current)setLoading(false);}
  }
  useEffect(()=>{api('/api/wiki').then(setWiki).catch(e=>setError(e.message));},[]);
  useEffect(()=>{
    const controller = new AbortController();
    const timer = setTimeout(()=>refresh(controller.signal),query ? 250 : 0);
    return ()=>{controller.abort();clearTimeout(timer);};
  },[profile,query,refreshKey]);
  useEffect(()=>{
    if (!active) return;
    let live=true;
    let timer;
    async function poll() {
      try {
        const history=await api('/api/jobs');
        if(!live) return;
        setJobs(history);
        if(!history.some(j=>['queued','running','cancelling'].includes(j.status))) {
          setRefreshKey(key=>key+1);
          return;
        }
      } catch(e) {if(live)setError(e.message);}
      if(live)timer=setTimeout(poll,1000);
    }
    timer=setTimeout(poll,1000);
    return ()=>{live=false;clearTimeout(timer);};
  },[active]);
  async function submit(engine) {
    setSubmitting(true);setError('');
    operationId.current ||= crypto.randomUUID();
    try {
      const job=await api('/api/jobs',{operation:selected.operation||'install',id:selected.id,engine,confirmed:true,request_id:operationId.current});
      setJobs(old=>[job,...old.filter(x=>x.id!==job.id)]);setSelected(null);operationId.current=null;setTab('jobs');
    } catch(e){setError(e.message);} finally {setSubmitting(false);}
  }
  async function cancel(id) {try{const job=await api(`/api/jobs/${id}/cancel`,{});setJobs(old=>old.map(x=>x.id===id?job:x));}catch(e){setError(e.message);}}
  async function updateCatalog() {
    setUpdating(true);setError('');
    try {await api('/api/catalog/refresh',{});setRefreshKey(key=>key+1);}
    catch(e){setError(e.message);}
    finally{setUpdating(false);}
  }
  const title = tab==='recommend'?'내 컴퓨터에 맞는 모델':tab==='models'?'내 모델':tab==='wiki'?'모델 설명':'작업';
  const subtitle = tab==='recommend'?'모델을 선택하고 설치하면 여러 실행 앱에서 함께 사용할 수 있어요.':tab==='models'?'OMM이 관리하는 모델과 실행 앱 연결 상태를 확인해요.':tab==='wiki'?'정확한 체크포인트의 용도와 출처를 확인하고 모델을 선택해요.':'실제 진행 상황과 저장된 결과를 확인해요.';
  const visible = catalog?.models || [];
  function choose(model){
    setError('');
    setNotice('');
    if(model.installed){
      if(!model.managed_by_omm)setNotice(`${model.name}은 ${model.installed_engines.join(', ') || '다른 실행 앱'}에 설치되어 있어요. OMM 관리 목록에는 포함되지 않아 여기서 삭제할 수 없어요.${model.installation_match==='model_identity'?' 같은 모델 계열이 확인됐고 양자화 형식은 다를 수 있어요.':''}`);
      setTab('models');setRefreshKey(key=>key+1);return;
    }
    operationId.current=null;setSelected(model);
  }
  return <div className="app"><aside><a className="brand" href="/">omm</a><nav aria-label="주 메뉴">{[['recommend','추천'],['models','내 모델'],['wiki','모델 설명'],['jobs','작업']].map(([key,label])=><button aria-current={tab===key?'page':undefined} className={tab===key?'selected':''} key={key} onClick={()=>setTab(key)}><Icon name={key}/>{label}</button>)}</nav><p className="local-note"><span/>이 컴퓨터에서 실행 중</p></aside><main><header><div><h1>{title}</h1><p>{subtitle}</p></div><button onClick={()=>setRefreshKey(key=>key+1)} disabled={loading}>{loading?'확인 중…':'다시 확인'}</button></header>{error && <div role="alert" className="error">{error}</div>}{notice && <div role="status" className="notice">{notice}</div>}<Hardware machine={machine} budget={catalog?.budget_gb}/>{tab==='recommend' && <>{!models.length && <FirstUse machine={machine} onModels={()=>setTab('models')}/>}<div className="toolbar"><div className="profiles" aria-label="메모리 사용 방식">{profiles.map(([key,label])=><button key={key} aria-pressed={profile===key} onClick={()=>setProfile(key)}>{label}</button>)}</div><label className="search"><Icon name="search"/><input maxLength={160} value={query} onChange={event=>setQuery(event.target.value)} placeholder="모델 이름으로 찾기" aria-label="모델 이름으로 찾기"/></label></div>{loading?<div className="empty" role="status">컴퓨터와 추천 자료를 확인하고 있어요.</div>:visible.length?<ModelTable rows={visible} onSelect={choose} onDetails={setDescription}/>:<div className="empty"><h2>{query?'검색 결과가 없어요':'현재 예산에 맞는 모델이 없어요'}</h2><p>{query?'다른 이름으로 검색해 보세요.':'사용 방식을 바꾸거나 다른 앱을 닫은 뒤 다시 확인해 주세요.'}</p></div>}<p className="footnote">예상 속도는 실제 측정과 다를 수 있어요. 메모리 수치는 파일 크기나 모델 사양으로 계산한 추정치예요.</p><Evidence catalog={catalog} onUpdate={updateCatalog} busy={updating}/><section className="before"><h2>설치 전에 확인할 것</h2><p>설치한 모델 파일은 이 컴퓨터에 저장돼요. 여러 실행 앱에서 파일을 공유할 수 있어요.</p><p>설치와 모델 실행은 별도로 확인해요. 다운로드 중에는 이 서버를 켜 두세요.</p></section></>}{tab==='wiki' && <WikiList document={wiki} onOpen={setDescription}/>} {tab==='models' && <Installed rows={models} onAction={choose}/>} {tab==='jobs' && <Jobs jobs={jobs} onCancel={cancel}/>}</main>{description && <WikiDetail model={description} onClose={()=>setDescription(null)}/>} {selected && <Confirmation model={selected} machine={machine} busy={submitting} error={error} onClose={()=>{setSelected(null);operationId.current=null;}} onSubmit={submit}/>}</div>;
}

createRoot(document.getElementById('root')).render(<App/>);
