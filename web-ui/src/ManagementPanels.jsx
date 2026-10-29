import React, {useEffect, useRef, useState} from 'react';
import {api, bytes} from './api';
import {L} from './i18n.js';

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
  if(resource.loading)return <p role="status">{L('로컬 상태를 확인하고 있어요.','Checking local status.')}</p>;
  return null;
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
  return <><LoadState resource={resource}/><section className="manager-section"><h2>{L('실행 앱과 로컬 연결','Runner apps and local connections')}</h2><p>{L('앱 설치와 로컬 서버 응답은 따로 확인해요. 서버 응답만으로 모델 실행 성공을 판단하지 않아요.','App install and local server response are checked separately. A server response alone doesn’t mean a model runs.')}</p><div className="connection-list">{resource.data?.engines.map(row=><article key={row.key}><div><h3>{row.name}</h3><p>{row.installed?L('설치됨','Installed'):L('미설치','Not installed')} · {row.api_status==='ready'?L('로컬 서버 연결됨','Local server connected'):row.api_status==='unreachable'?L('로컬 서버에 연결할 수 없음','Can’t reach local server'):L('로컬 응답 확인 API 미지원','No local status API')}</p>{row.version && <small>{L('서버 버전','Server version')} {row.version}</small>}<small>{row.installer_check?L(`최근 설치 검사: ${row.installer_check.status==='named_checks_passed'?'필수 항목 통과':'검사 실패 · 실행 전 확인 필요'} · ${new Date(row.installer_check.checked_at).toLocaleString(L('ko-KR','en-US'))}`,`Last install check: ${row.installer_check.status==='named_checks_passed'?'Required checks passed':'Check failed · Review before running'} · ${new Date(row.installer_check.checked_at).toLocaleString(L('ko-KR','en-US'))}`):L('OMM 설치 검사 기록 없음','No OMM install check on record')}</small>{row.installer_check && <small>{L('검사 시점의 기록이며, 모든 취약점이나 현재 안전을 보증하지 않아요.','This is a record from the time of the check. It doesn’t guarantee there are no vulnerabilities or that the app is safe now.')}</small>}{row.api_status==='unreachable' && <small>{row.key==='ollama'?L('서버 시작 도우미를 사용할 수 있어요.','You can use the server start helper.'):L('실행 앱에서 로컬 서버를 켜고 다시 확인하세요.','Turn on the local server in the runner app, then check again.')}</small>}</div>{row.key==='ollama' && row.installed && <button onClick={()=>onAction({operation:'engine_start',engine:'ollama',filename:L('Ollama 로컬 서버','Ollama local server')})}>{row.api_status==='ready'?L('Ollama 서버 확인','Check Ollama server'):L('Ollama 서버 켜기','Start Ollama server')}</button>}{!row.installed && <button onClick={()=>onAction({operation:'engine_install',engine:row.key,filename:row.name})}>{L('설치','Install')}</button>}</article>)}</div></section>
    <section className="manager-section"><h2>{L('다운로드·추천 자료 연결','Download and recommendation data connections')}</h2><p>{L('필요할 때 외부 서버 연결을 확인해요. 자동으로 반복 검사하지 않아요.','Checks connections to external servers when you ask. It doesn’t check repeatedly on its own.')}</p><button onClick={check} disabled={busy}>{busy?L('연결 확인 중…','Checking connection…'):L('네트워크 연결 확인','Check network connection')}</button>{error && <p className="error" role="alert">{error}</p>}{network?.targets.map(row=><p key={row.name}>{row.name} · <strong>{row.status==='ready'?L('응답 확인','Responding'):L('연결 확인 실패','Connection check failed')}</strong>{row.http_status && ` (HTTP ${row.http_status})`}</p>)}{network && <small>{L('확인','Checked')} {new Date(network.checked_at).toLocaleTimeString(L('ko-KR','en-US'))} · {L('인터넷 전체 상태나 모델 다운로드 성공을 뜻하지 않아요.','This doesn’t reflect overall internet status or mean a model download will succeed.')}</small>}</section>
  </>;
}

export function Diagnostics({refreshKey}) {
  const resource=useResource('/api/diagnostics',refreshKey);
  return <><LoadState resource={resource}/><p className="footnote">{L('현재 설치와 모델 연결을 읽어서 점검해요. 아래 안내를 자동으로 실행하거나 파일을 수정하지 않아요.','Reads your current installs and model links to check them. It doesn’t run the steps below or change any files.')}</p><div className="diagnostic-list">{resource.data?.checks.map((check,index)=><article key={`${check.name}-${index}`}><div className="diagnostic-title"><h2>{check.name}</h2><span className={`check-status ${check.status.toLowerCase()}`}>{check.status==='PASS'?L('정상','OK'):check.status==='WARN'?L('확인 필요','Needs attention'):L('문제 발견','Problem found')}</span></div><p>{check.detail}</p>{check.remediation && <div className="remediation"><strong>{L('해결 방법','How to fix')}</strong><p>{check.remediation.message}</p>{check.remediation.command && <pre>{check.remediation.command.map(x=>JSON.stringify(x)).join(' ')}</pre>}</div>}</article>)}</div></>;
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
  return <><section className="manager-section"><h2>{L('같은 조건에서 실제로 비교','Compare for real under the same conditions')}</h2><p>{L('설치된 모델을 최대 4개 선택하세요. 8문항 산술 점검과 3회 속도 측정을 실행하고 결과를 이 컴퓨터에 저장해요.','Choose up to 4 installed models. Runs an 8-question arithmetic check and 3 speed measurements, and saves the results on this computer.')}</p><p className="footnote">{L('4096 토큰 문맥과 같은 문제를 사용해요. 일반적인 답변 품질이나 전문 분야 성능 순위가 아니에요. 이미 로딩된 모델이 있으면 기존 작업을 유지하고 비교를 중단해요.','Uses a 4096-token context and the same questions. This is not a ranking of general answer quality or domain performance. If a model is already loaded, the existing work is kept and the comparison stops.')}</p><label className="field">{L('실행 앱','Runner app')}<select value={engine} onChange={e=>{setEngine(e.target.value);setIds([]);}}>{Object.entries(runtimeNames).map(([key,name])=><option key={key} value={key}>{name}</option>)}</select></label><div className="select-models">{eligible.map(row=><label key={row.id}><input type="checkbox" checked={selected.includes(row.id)} disabled={selected.length>=4 && !selected.includes(row.id)} onChange={e=>setIds(old=>e.target.checked?[...old,row.id]:old.filter(x=>x!==row.id))}/><span>{row.filename}</span></label>)}</div>{!eligible.length && <p>{L('이 실행 앱에 연결된 모델이 없어요. 내 모델에서 먼저 연결하세요.','No models are linked to this runner app. Link one in My models first.')}</p>}<button className="primary" disabled={!selected.length} onClick={()=>onAction({operation:'compare',ids:selected,engine,filename:L(`선택한 모델 ${selected.length}개`,`${selected.length} selected models`)})}>{L('비교 시작','Start comparison')}</button></section>
    <section className="manager-section"><h2>{L('최근 저장된 비교','Recent saved comparison')}</h2>{report?<><div className="table-wrap comparison-table"><table><thead><tr><th>{L('모델','Model')}</th><th>{L('산술 점검','Arithmetic check')}</th><th>{L('중앙 속도','Median speed')}</th><th>{L('속도 표본','Speed samples')}</th></tr></thead><tbody>{report.models.map(row=><tr key={row.filename}><td>{row.filename}</td><td>{row.correct}/{row.total}</td><td>{row.tokens_per_second.toFixed(1)} tok/s</td><td>{row.samples.map(x=>x.toFixed(1)).join(' / ')}</td></tr>)}</tbody></table></div><p className="footnote">{report.limitation} · {runtimeNames[report.engine]} · {new Date(report.checked_at).toLocaleString(L('ko-KR','en-US'))}</p><button onClick={()=>downloadJSON(report,report.report_filename)}>{L('측정 결과 JSON 저장','Save results as JSON')}</button></>:<p>{L('아직 저장된 비교 결과가 없어요.','No saved comparison results yet.')}</p>}</section></>;
}

const optionLabels={context_length:L('문맥 길이','Context length'),cpu_threads:L('CPU 스레드','CPU threads'),batch_size:L('배치 크기','Batch size'),gpu_layers:L('GPU 레이어','GPU layers')};
export function RuntimeSettings({model,onClose,onAction,refreshKey}) {
  const dialog=useRef(null);
  const resource=useResource(`/api/models/${model.id}/settings`,refreshKey);
  const [engine,setEngine]=useState(model.engines.find(x=>runtimeNames[x]) || 'ollama');
  useEffect(()=>{dialog.current?.showModal();},[]);
  const state=resource.data?.engines[engine];
  return <dialog className="wide-dialog" ref={dialog} onCancel={onClose}><h2>{L('실행 설정','Run settings')}</h2><p className="filename">{model.filename}</p><LoadState resource={resource}/>{resource.data && <><label className="field">{L('실행 앱','Runner app')}<select value={engine} onChange={e=>setEngine(e.target.value)}>{Object.keys(resource.data.engines).map(key=><option key={key} value={key}>{runtimeNames[key]}</option>)}</select></label>{state?<><h3>{L('현재 컴퓨터에 맞는 시작 설정','Starting settings for this computer')}</h3><dl className="settings-values">{Object.entries(state.proposed).map(([key,value])=><div key={key}><dt>{optionLabels[key] || key}</dt><dd>{value===-1?L('전체','All'):value}</dd></div>)}</dl><p>{L('기준 설정과 추천 설정으로 짧은 실제 응답을 각각 확인해요. 적용된 설정과 검증용 메모리 해제를 확인한 뒤 모델별로 저장해요.','Checks a short real response with the baseline settings and with the recommended settings. Saves per model after confirming the settings applied and the test memory was freed.')}</p><p className="footnote">{L('짧은 응답 속도로 전문 분야 성능 향상을 판단하지 않아요. LM Studio의 CPU·GPU 설정은 실행 앱에서 관리해요.','Short response speed doesn’t show better domain performance. LM Studio’s CPU and GPU settings are managed in that app.')}</p><p>{L('저장 상태:','Saved state:')} {state.saved.status==='active'?L('저장된 설정 있음','Settings saved'):state.saved.status==='stale'?L('모델 파일 변경 — 재검증 필요','Model file changed — verify again'):L('기본 설정','Default settings')}</p>{state.saved.active && <dl className="settings-values">{Object.entries(state.saved.active.options).map(([key,value])=><div key={key}><dt>{optionLabels[key] || key}</dt><dd>{value===-1?L('전체','All'):value}</dd></div>)}</dl>}<div className="dialog-actions"><button onClick={()=>{onClose();onAction({...model,operation:'profile_restore',engine});}}>{L('이전 설정 복원','Restore previous settings')}</button><button className="primary" onClick={()=>{onClose();onAction({...model,operation:'profile_save',engine});}}>{L('검증 후 저장','Verify and save')}</button></div></>:<p>{L('Ollama 또는 LM Studio에 모델을 연결한 뒤 설정을 확인하세요.','Link the model to Ollama or LM Studio, then check the settings.')}</p>}</>}<div className="dialog-actions"><button onClick={onClose}>{L('닫기','Close')}</button></div></dialog>;
}

const policyLabels={telemetry_send_policy:L('벤치마크 결과 공유','Share benchmark results'),error_report_send_policy:L('오류 보고서 공유','Share error reports'),usage_stats_policy:L('익명 사용 통계','Anonymous usage stats'),memory_guard_policy:L('메모리 보호','Memory guard')};
const policyOptions={telemetry_send_policy:[['ask',L('매번 확인','Ask every time')],['never',L('보내지 않음','Don’t send')],['always',L('공유 허용','Allow sharing')]],error_report_send_policy:[['ask',L('매번 확인','Ask every time')],['never',L('보내지 않음','Don’t send')],['always',L('공유 허용','Allow sharing')]],usage_stats_policy:[['never',L('보내지 않음','Don’t send')],['enabled',L('공유 허용','Allow sharing')]],memory_guard_policy:[['ask',L('실행 전에 확인','Ask before running')],['block',L('메모리 부족 시 차단','Block when memory is low')],['observe',L('관찰','Observe only')] ]};
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
    try {await api('/api/settings',{changes,confirmed:true});setMessage(L('설정을 저장했어요.','Settings saved.'));}
    catch(e){setError(e.message);}finally{setSaving(false);}
  }
  const selectedFiles=selected.filter(id=>files.data?.partials.some(x=>x.id===id));
  return <><section className="manager-section"><h2>{L('모델 저장 공간','Model storage')}</h2><LoadState resource={files}/>{files.data && <><p className="filename">{files.data.path}</p><p>{L('관리 중인 파일','Managed files')} {bytes(files.data.managed_bytes)} · {L('볼륨에 쓸 수 있는 공간','Free space on volume')} {bytes(files.data.available_bytes)}</p><p>{L('기존 모델 가져오기로 아낀 공간','Space saved by importing existing models')} {bytes(files.data.storage_saved_bytes)}</p><p className="footnote">{L('실행 앱에 있던 같은 파일을 복사하지 않고 공유해서 줄인 누적 용량이에요. CLI의 omm scan과 같은 기록을 보여줘요.','Total space saved by sharing files runner apps already had instead of copying them. Shows the same record as omm scan in the CLI.')}</p><h3>{L('불완전 다운로드','Incomplete downloads')}</h3><p className="footnote">{L('선택한 파일만 지워요. 재개하려는 다운로드는 선택하지 마세요.','Only the selected files are deleted. Don’t select downloads you want to resume.')}</p>{files.data.partials.map(row=><label className="file-choice" key={row.id}><input type="checkbox" checked={selectedFiles.includes(row.id)} onChange={e=>setSelected(old=>e.target.checked?[...old,row.id]:old.filter(x=>x!==row.id))}/><span>{row.filename} · {bytes(row.size_bytes)}</span></label>)}{!files.data.partials.length && <p>{L('정리할 불완전 다운로드가 없어요.','No incomplete downloads to clean up.')}</p>}<button disabled={!selectedFiles.length} onClick={()=>onAction({operation:'cleanup',ids:selectedFiles,filename:L(`선택한 파일 ${selectedFiles.length}개`,`${selectedFiles.length} selected files`)})}>{L('선택한 파일 정리','Clean up selected files')}</button></>}</section>
    <section className="manager-section"><h2>{L('기존 모델 가져오기','Import existing models')}</h2><p>{L('실행 앱의 모델 폴더에서 기존 GGUF 파일을 찾아요. 검색 단계에서는 파일을 옮기지 않아요.','Looks for existing GGUF files in runner apps’ model folders. Nothing is moved while searching.')}</p><button onClick={()=>onAction({operation:'import_scan',filename:L('외부 모델 검색','Search external models')})}>{L('외부 모델 검색','Search external models')}</button><LoadState resource={imports}/><div className="package-list">{imports.data?.map(row=><article key={row.id}><div><strong>{row.name}</strong><small>{bytes(row.size_bytes)} · {row.engines.join(', ')}</small></div><button onClick={()=>onAction({operation:'import',id:row.id,filename:row.name})}>{L('가져오기','Import')}</button></article>)}</div></section>
    <section className="manager-section"><h2>{L('데이터 공유와 메모리 보호','Data sharing and memory guard')}</h2><LoadState resource={settings}/><p>{L('성능 비교 결과는 로컬에 저장해요. 아래 공유 설정은 CLI와 같은 설정을 사용해요.','Performance comparison results are saved locally. The sharing settings below are the same ones the CLI uses.')}</p>{error && <p role="alert" className="error">{error}</p>}{message && <p role="status" className="notice">{message}</p>}{settings.data && <form onSubmit={save} className="policy-form">{Object.entries(policyLabels).map(([key,label])=><label className="field" key={key}>{label}<select aria-label={label} value={changes[key] ?? settings.data[key]} onChange={e=>{setChanges(old=>({...old,[key]:e.target.value}));setMessage('');}}>{policyOptions[key].map(([value,text])=><option key={value} value={value}>{text}</option>)}</select></label>)}<p className="footnote">{L('‘공유 허용’을 선택하면 해당 종류의 자료 전송에 동의하는 설정으로 저장돼요. 검색·다운로드·업데이트의 네트워크 사용은 별개예요.','Choosing ‘Allow sharing’ saves your consent to send that kind of data. Network use for search, download, and update is separate.')}</p><button className="primary" disabled={saving || !Object.keys(changes).length}>{saving?L('저장 중…','Saving…'):L('설정 저장','Save settings')}</button></form>}</section></>;
}
