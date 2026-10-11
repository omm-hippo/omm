import React, {useEffect, useRef, useState} from 'react';
import {api, bytes} from './api';

const tasks = {agents:'에이전트',coding:'코딩',reasoning:'추론',vision:'이미지',multilingual:'다국어',compact:'경량'};
const runtimeNames = {ollama:'Ollama',lmstudio:'LM Studio'};

function useResource(path, refreshKey = 0) {
  const [data,setData] = useState(null);
  const [error,setError] = useState('');
  const [loading,setLoading] = useState(true);
  useEffect(()=>{
    const controller = new AbortController();
    setLoading(true);setError('');
    api(path, undefined, controller.signal).then(setData).catch(e=>{
      if(!controller.signal.aborted)setError(e.message);
    }).finally(()=>{if(!controller.signal.aborted)setLoading(false);});
    return ()=>controller.abort();
  },[path,refreshKey]);
  return {data,error,loading};
}

function LoadState({resource}) {
  if(resource.error)return <p role="alert" className="error">{resource.error}</p>;
  if(resource.loading)return <p role="status">로컬 상태를 확인하고 있어요.</p>;
  return null;
}

export function WikiExplorer({document,onOpen,onSelect,profile}) {
  const [query,setQuery] = useState('');
  const [task,setTask] = useState('');
  const [selected,setSelected] = useState(null);
  const terms=query.toLocaleLowerCase('ko').trim().split(/\s+/).filter(Boolean);
  const rows=(document?.models || []).filter(model=>{
    const text=[model.name,model.repository,model.summary,model.chooseWhen,...model.strengths.map(x=>x.text),...model.tasks.map(x=>tasks[x])].join(' ').toLocaleLowerCase('ko');
    return (!task || model.tasks.includes(task)) && terms.every(x=>text.includes(x));
  });
  return <>
    <div className="explorer-toolbar"><label className="search"><input maxLength={160} value={query} onChange={e=>setQuery(e.target.value)} placeholder="모델 이름, 용도, 특징으로 검색" aria-label="모델 위키 검색"/></label><label className="field">용도<select value={task} onChange={e=>setTask(e.target.value)}><option value="">전체 용도</option>{Object.entries(tasks).map(([key,name])=><option value={key} key={key}>{name}</option>)}</select></label></div>
    <p className="footnote">기존 모델 위키 · 검토 {document?.reviewedAt || '확인 중'} · 개발사 설명과 편집 의견을 함께 보여줘요.</p>
    <div className="wiki-list">{rows.map(model=><article key={model.id}><div><h2>{model.name}</h2><p>{model.summary}</p><small>{model.tasks.map(x=>tasks[x]).join(' · ')}</small><p className="choose-when">{model.chooseWhen}</p></div><div className="row-actions"><button onClick={()=>onOpen(model)}>설명 보기</button><button className="primary" onClick={()=>setSelected(model)}>설치 파일 보기</button></div></article>)}</div>
    {document && !rows.length && <div className="empty">이 조건에 맞는 위키 설명이 없어요.</div>}
    {selected && <PackageDialog model={selected} profile={profile} onClose={()=>setSelected(null)} onSelect={row=>{setSelected(null);onSelect(row);}}/>}
  </>;
}

function PackageDialog({model,profile,onClose,onSelect}) {
  const dialog=useRef(null);
  const resource=useResource(`/api/wiki/packages?id=${encodeURIComponent(model.id)}&profile=${profile}`);
  const [remote,setRemote]=useState(null);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  useEffect(()=>{dialog.current?.showModal();},[]);
  async function discover() {
    setBusy(true);setError('');
    try {setRemote(await api('/api/wiki/discover',{id:model.id,profile}));}
    catch(e){setError(e.message);}finally{setBusy(false);}
  }
  const value=remote || resource.data;
  return <dialog className="wide-dialog" ref={dialog} onCancel={onClose}><h2>{model.name} 설치 파일</h2><p>{model.summary}</p><p className="footnote">정확한 원본 모델 관계가 확인된 GGUF 파일만 보여줘요. 용량과 설치 예산을 확인한 뒤 선택하세요. 실제 실행에는 실행 시점의 메모리 여유가 필요해요.</p><LoadState resource={resource}/>{error && <p role="alert" className="error">{error}</p>}
    <div className="package-list">{value?.packages.map(row=><article key={row.id}><div><strong>{row.filename}</strong><small>{row.wiki.packageRepository || row.wiki.repository}</small><small>{bytes(row.size_bytes)} · 예상 메모리 {row.memory_required_gb?.toFixed(1) || '확인 필요'} GB · {row.fits===true?'선택한 설치 예산에 맞음':row.fits===false?'선택한 설치 예산 초과':'메모리 판단 근거 부족'}</small><small>{row.wiki.match==='publisher_declared_base'?'업로더가 명시한 원본 모델 관계':'기존 위키의 저장소 연결 근거'}</small></div><button onClick={()=>onSelect(row)} disabled={row.fits!==true}>설치</button></article>)}</div>
    {!resource.loading && !value?.packages.length && <div className="empty">로컬 자료에 연결된 설치 파일이 없어요. 온라인에서 파일을 찾아볼 수 있어요.</div>}
    <p className="footnote">온라인 검색은 Hugging Face에 접속해 파일 목록을 확인해요. 모델 파일은 설치를 선택할 때 내려받아요.</p><div className="dialog-actions"><button onClick={onClose}>닫기</button><button onClick={discover} disabled={busy}>{busy?'설치 파일 찾는 중…':'온라인에서 설치 파일 찾기'}</button></div>
  </dialog>;
}

export function Connections({refreshKey,onAction}) {
  const resource=useResource('/api/connections',refreshKey);
  const [network,setNetwork]=useState(null);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  async function check() {
    setBusy(true);setError('');
    try {setNetwork(await api('/api/connections/check',{}));}
    catch(e){setError(e.message);}finally{setBusy(false);}
  }
  return <><LoadState resource={resource}/><section className="manager-section"><h2>실행 앱과 로컬 연결</h2><p>앱 설치와 로컬 서버 응답은 따로 확인해요. 서버 응답만으로 모델 실행 성공을 판단하지 않아요.</p><div className="connection-list">{resource.data?.engines.map(row=><article key={row.key}><div><h3>{row.name}</h3><p>{row.installed?'설치됨':'미설치'} · {row.api_status==='ready'?'로컬 서버 연결됨':row.api_status==='unreachable'?'로컬 서버에 연결할 수 없음':'로컬 응답 확인 API 미지원'}</p>{row.version && <small>서버 버전 {row.version}</small>}{row.api_status==='unreachable' && <small>실행 앱에서 로컬 서버를 켜고 다시 확인하세요.</small>}</div>{!row.installed && <button onClick={()=>onAction({operation:'engine_install',engine:row.key,filename:row.name})}>설치</button>}</article>)}</div></section>
    <section className="manager-section"><h2>다운로드·추천 자료 연결</h2><p>필요할 때 외부 서버 연결을 확인해요. 자동으로 반복 검사하지 않아요.</p><button onClick={check} disabled={busy}>{busy?'연결 확인 중…':'네트워크 연결 확인'}</button>{error && <p className="error" role="alert">{error}</p>}{network?.targets.map(row=><p key={row.name}>{row.name} · <strong>{row.status==='ready'?'응답 확인':'연결 확인 실패'}</strong>{row.http_status && ` (HTTP ${row.http_status})`}</p>)}{network && <small>확인 {new Date(network.checked_at).toLocaleTimeString('ko-KR')} · 인터넷 전체 상태나 모델 다운로드 성공을 뜻하지 않아요.</small>}</section>
  </>;
}

export function Diagnostics({refreshKey}) {
  const resource=useResource('/api/diagnostics',refreshKey);
  return <><LoadState resource={resource}/><p className="footnote">현재 설치와 모델 연결을 읽어서 점검해요. 아래 안내를 자동으로 실행하거나 파일을 수정하지 않아요.</p><div className="diagnostic-list">{resource.data?.checks.map((check,index)=><article key={`${check.name}-${index}`}><div className="diagnostic-title"><h2>{check.name}</h2><span className={`check-status ${check.status.toLowerCase()}`}>{check.status==='PASS'?'정상':check.status==='WARN'?'확인 필요':'문제 발견'}</span></div><p>{check.detail}</p>{check.remediation && <div className="remediation"><strong>해결 방법</strong><p>{check.remediation.message}</p>{check.remediation.command && <pre>{check.remediation.command.map(x=>JSON.stringify(x)).join(' ')}</pre>}</div>}</article>)}</div></>;
}

function downloadJSON(value,filename) {
  const url=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)+'\n'],{type:'application/json'}));
  const anchor=document.createElement('a');anchor.href=url;anchor.download=filename;anchor.click();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
}

export function Comparison({models,jobs,onAction}) {
  const [engine,setEngine]=useState('ollama');
  const [ids,setIds]=useState([]);
  const eligible=models.filter(x=>x.exists && x.engines.includes(engine));
  const selected=ids.filter(id=>eligible.some(x=>x.id===id));
  const report=jobs.find(x=>x.operation==='compare' && x.status==='completed')?.result;
  return <><section className="manager-section"><h2>같은 조건에서 실제로 비교</h2><p>설치된 모델을 최대 4개 선택하세요. 8문항 산술 점검과 3회 속도 측정을 실행하고 결과를 이 컴퓨터에 저장해요.</p><p className="footnote">4096 토큰 문맥과 같은 문제를 사용해요. 일반적인 답변 품질이나 전문 분야 성능 순위가 아니에요. 이미 로딩된 모델이 있으면 기존 작업을 유지하고 비교를 중단해요.</p><label className="field">실행 앱<select value={engine} onChange={e=>{setEngine(e.target.value);setIds([]);}}>{Object.entries(runtimeNames).map(([key,name])=><option key={key} value={key}>{name}</option>)}</select></label><div className="select-models">{eligible.map(row=><label key={row.id}><input type="checkbox" checked={selected.includes(row.id)} disabled={selected.length>=4 && !selected.includes(row.id)} onChange={e=>setIds(old=>e.target.checked?[...old,row.id]:old.filter(x=>x!==row.id))}/><span>{row.filename}</span></label>)}</div>{!eligible.length && <p>이 실행 앱에 연결된 모델이 없어요. 내 모델에서 먼저 연결하세요.</p>}<button className="primary" disabled={!selected.length} onClick={()=>onAction({operation:'compare',ids:selected,engine,filename:`선택한 모델 ${selected.length}개`})}>비교 시작</button></section>
    <section className="manager-section"><h2>최근 저장된 비교</h2>{report?<><div className="table-wrap comparison-table"><table><thead><tr><th>모델</th><th>산술 점검</th><th>중앙 속도</th><th>속도 표본</th></tr></thead><tbody>{report.models.map(row=><tr key={row.filename}><td>{row.filename}</td><td>{row.correct}/{row.total}</td><td>{row.tokens_per_second.toFixed(1)} tok/s</td><td>{row.samples.map(x=>x.toFixed(1)).join(' / ')}</td></tr>)}</tbody></table></div><p className="footnote">{report.limitation} · {runtimeNames[report.engine]} · {new Date(report.checked_at).toLocaleString('ko-KR')}</p><button onClick={()=>downloadJSON(report,report.report_filename)}>측정 결과 JSON 저장</button></>:<p>아직 저장된 비교 결과가 없어요.</p>}</section></>;
}

const optionLabels={context_length:'문맥 길이',cpu_threads:'CPU 스레드',batch_size:'배치 크기',gpu_layers:'GPU 레이어'};
export function RuntimeSettings({model,onClose,onAction,refreshKey}) {
  const dialog=useRef(null);
  const resource=useResource(`/api/models/${model.id}/settings`,refreshKey);
  const [engine,setEngine]=useState(model.engines.find(x=>runtimeNames[x]) || 'ollama');
  useEffect(()=>{dialog.current?.showModal();},[]);
  const state=resource.data?.engines[engine];
  return <dialog className="wide-dialog" ref={dialog} onCancel={onClose}><h2>실행 설정</h2><p className="filename">{model.filename}</p><LoadState resource={resource}/>{resource.data && <><label className="field">실행 앱<select value={engine} onChange={e=>setEngine(e.target.value)}>{Object.keys(resource.data.engines).map(key=><option key={key} value={key}>{runtimeNames[key]}</option>)}</select></label>{state?<><h3>현재 컴퓨터에 맞는 시작 설정</h3><dl className="settings-values">{Object.entries(state.proposed).map(([key,value])=><div key={key}><dt>{optionLabels[key] || key}</dt><dd>{value===-1?'전체':value}</dd></div>)}</dl><p>기준 설정과 추천 설정으로 짧은 실제 응답을 각각 확인해요. 적용된 설정과 검증용 메모리 해제를 확인한 뒤 모델별로 저장해요.</p><p className="footnote">짧은 응답 속도로 전문 분야 성능 향상을 판단하지 않아요. LM Studio의 CPU·GPU 설정은 실행 앱에서 관리해요.</p><p>저장 상태: {state.saved.status==='active'?'저장된 설정 있음':state.saved.status==='stale'?'모델 파일 변경 — 재검증 필요':'기본 설정'}</p>{state.saved.active && <dl className="settings-values">{Object.entries(state.saved.active.options).map(([key,value])=><div key={key}><dt>{optionLabels[key] || key}</dt><dd>{value===-1?'전체':value}</dd></div>)}</dl>}<div className="dialog-actions"><button onClick={()=>{onClose();onAction({...model,operation:'profile_restore',engine});}}>이전 설정 복원</button><button className="primary" onClick={()=>{onClose();onAction({...model,operation:'profile_save',engine});}}>검증 후 저장</button></div></>:<p>Ollama 또는 LM Studio에 모델을 연결한 뒤 설정을 확인하세요.</p>}</>}<div className="dialog-actions"><button onClick={onClose}>닫기</button></div></dialog>;
}

const policyLabels={telemetry_send_policy:'벤치마크 결과 공유',error_report_send_policy:'오류 보고서 공유',usage_stats_policy:'익명 사용 통계',memory_guard_policy:'메모리 보호'};
const policyOptions={telemetry_send_policy:[['ask','매번 확인'],['never','보내지 않음'],['always','공유 허용']],error_report_send_policy:[['ask','매번 확인'],['never','보내지 않음'],['always','공유 허용']],usage_stats_policy:[['never','보내지 않음'],['enabled','공유 허용']],memory_guard_policy:[['ask','실행 전에 확인'],['block','메모리 부족 시 차단'],['observe','관찰'] ]};
export function FilesSettings({refreshKey,onAction}) {
  const files=useResource('/api/files',refreshKey);
  const imports=useResource('/api/imports',refreshKey);
  const settings=useResource('/api/settings',refreshKey);
  const [selected,setSelected]=useState([]);
  const [changes,setChanges]=useState({});
  const [saving,setSaving]=useState(false);
  const [message,setMessage]=useState('');
  const [error,setError]=useState('');
  async function save(event) {
    event.preventDefault();setSaving(true);setError('');setMessage('');
    try {await api('/api/settings',{changes,confirmed:true});setMessage('설정을 저장했어요.');}
    catch(e){setError(e.message);}finally{setSaving(false);}
  }
  const selectedFiles=selected.filter(id=>files.data?.partials.some(x=>x.id===id));
  return <><section className="manager-section"><h2>모델 저장 공간</h2><LoadState resource={files}/>{files.data && <><p className="filename">{files.data.path}</p><p>관리 중인 파일 {bytes(files.data.managed_bytes)} · 볼륨에 쓸 수 있는 공간 {bytes(files.data.available_bytes)}</p><h3>불완전 다운로드</h3><p className="footnote">선택한 파일만 지워요. 재개하려는 다운로드는 선택하지 마세요.</p>{files.data.partials.map(row=><label className="file-choice" key={row.id}><input type="checkbox" checked={selectedFiles.includes(row.id)} onChange={e=>setSelected(old=>e.target.checked?[...old,row.id]:old.filter(x=>x!==row.id))}/><span>{row.filename} · {bytes(row.size_bytes)}</span></label>)}{!files.data.partials.length && <p>정리할 불완전 다운로드가 없어요.</p>}<button disabled={!selectedFiles.length} onClick={()=>onAction({operation:'cleanup',ids:selectedFiles,filename:`선택한 파일 ${selectedFiles.length}개`})}>선택한 파일 정리</button></>}</section>
    <section className="manager-section"><h2>기존 모델 가져오기</h2><p>실행 앱의 모델 폴더에서 기존 GGUF 파일을 찾아요. 검색 단계에서는 파일을 옮기지 않아요.</p><button onClick={()=>onAction({operation:'import_scan',filename:'외부 모델 검색'})}>외부 모델 검색</button><LoadState resource={imports}/><div className="package-list">{imports.data?.map(row=><article key={row.id}><div><strong>{row.name}</strong><small>{bytes(row.size_bytes)} · {row.engines.join(', ')}</small></div><button onClick={()=>onAction({operation:'import',id:row.id,filename:row.name})}>가져오기</button></article>)}</div></section>
    <section className="manager-section"><h2>데이터 공유와 메모리 보호</h2><LoadState resource={settings}/><p>성능 비교 결과는 로컬에 저장해요. 아래 공유 설정은 CLI와 같은 설정을 사용해요.</p>{error && <p role="alert" className="error">{error}</p>}{message && <p role="status" className="notice">{message}</p>}{settings.data && <form onSubmit={save} className="policy-form">{Object.entries(policyLabels).map(([key,label])=><label className="field" key={key}>{label}<select aria-label={label} value={changes[key] ?? settings.data[key]} onChange={e=>{setChanges(old=>({...old,[key]:e.target.value}));setMessage('');}}>{policyOptions[key].map(([value,text])=><option key={value} value={value}>{text}</option>)}</select></label>)}<p className="footnote">‘공유 허용’을 선택하면 해당 종류의 자료 전송에 동의하는 설정으로 저장돼요. 검색·다운로드·업데이트의 네트워크 사용은 별개예요.</p><button className="primary" disabled={saving || !Object.keys(changes).length}>{saving?'저장 중…':'설정 저장'}</button></form>}</section></>;
}
