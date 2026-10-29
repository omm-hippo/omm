import {ModelGuide,RecoveryActions} from './Usability';
import {TaskProgress} from './TaskFlow';
import DocumentPanel, {DocumentProgress} from './DocumentPanel';
import React, {useEffect, useRef, useState} from 'react';
import {createRoot} from 'react-dom/client';
import {api, gib, bytes} from './api';
import './style.css';
import './workspace.css';
import {Sidebar,CommandPalette,WorkspaceIcon} from './Workspace.jsx';
import {destinations,readAppearance,measurementSummary,wikiSummary} from './workspace-state.js';
import ChatPanel from './ChatPanel';
import SharedAI from './SharedAI';
import {WikiExplorer,Connections,Diagnostics,Comparison,RuntimeSettings,FilesSettings} from './ManagementPanels';
import {L,lang} from './i18n.js';
document.documentElement.lang=lang;document.title=L('OMM · 로컬 모델 관리','OMM · Local model manager');

const profiles = [['balanced',L('다른 작업과 함께', 'Alongside other work')],['dedicated',L('모델에 집중', 'Focus on the model')],['minimal',L('가볍게 사용', 'Light use')]];
const purposes = {General:L('일반', 'General'),Coding:L('코딩', 'Coding'),Reasoning:L('추론·수학', 'Reasoning & math'),Writing:L('글쓰기', 'Writing'),Translation:L('번역', 'Translation'),Documents:L('문서', 'Documents'), '—':L('정보 없음', 'Unknown')};
const operationLabels={engine_start:L('Ollama 서버 시작·확인', 'Start/check Ollama server'),start_chat:L('대화 준비하고 시작', 'Prepare and start chat'),install:L('모델 설치', 'Install model'),link:L('실행 앱 연결', 'Link to app'),uninstall:L('모델 삭제', 'Delete model'),verify:L('모델 실행 확인', 'Verify model runs'),compare:L('성능 비교', 'Performance comparison'),profile_save:L('실행 설정 검증·저장', 'Test and save run settings'),profile_restore:L('실행 설정 복원', 'Restore run settings'),engine_install:L('실행 앱 설치', 'Install app'),import_scan:L('외부 모델 검색', 'Scan for external models'),import:L('모델 가져오기', 'Import models'),cleanup:L('불완전 다운로드 정리', 'Clean up incomplete downloads'),document_import:L('문서 등록', 'Add documents')};
const statuses = {queued:L('대기 중', 'Queued'),running:L('진행 중', 'Running'),cancelling:L('중단 요청됨', 'Stopping'),completed:L('완료', 'Done'),failed:L('실패', 'Failed'),cancelled:L('중단됨', 'Stopped'),interrupted:L('결과 확인 필요', 'Check result')};
const evidenceLabels = {static_rules:L('메모리 규칙', 'Memory rules'),insufficient:L('실측 근거 부족', 'Not enough measurements'),local_calibrated:L('로컬 속도 보정', 'Calibrated to local speed'),feature_support:L('유사 특성 실측 있음', 'Measured on similar setups'),synthetic_prior:L('합성 데이터 예측', 'Predicted from synthetic data')};

const evidenceBasis = {similar_measured:L('유사 구성 실측', 'Measured on similar setups'),measurement_estimate:L('실측 기반 추정', 'Estimated from measurements'),rules_or_synthetic:L('규칙·합성 기반', 'Based on rules/synthetic data'),insufficient_evidence:L('근거 부족', 'Not enough evidence')};

function Evidence({catalog,onUpdate,busy}) {
  const source = {bundled_rules:L('기본 메모리 규칙', 'Built-in memory rules'),signed_cache:L('서명을 확인한 추천 자료', 'Verified signed recommendation data'),local_cache:L('로컬 추천 자료', 'Local recommendation data')}[catalog?.catalog_source];
  const details = catalog?.catalog;
  return <section className="evidence"><div className="evidence-heading"><h2>{L('추천 근거','Why these picks')}</h2><button onClick={onUpdate} disabled={busy}>{busy?L('자료 받는 중…', 'Downloading data…'):L('추천 자료 업데이트', 'Update recommendation data')}</button></div><p>{source || L('자료 확인 중', 'Checking data')}</p>{details?.trained_at && <p>{L('학습 시점 ','Trained ')}{details.trained_at.slice(0,10)}{Number.isInteger(details.real_configuration_count) && L(` · 전체 실측 구성 ${details.real_configuration_count}개`, ` · ${details.real_configuration_count} measured configurations in total`)}</p>}{details?.measurements && <p>{measurementSummary(details.measurements)}</p>}<p>{catalog?.evidence?.reason}</p>{catalog?.evidence?.local_calibration_samples>0 && <p>{L(`로컬 보정 측정 ${catalog.evidence.local_calibration_samples}회 · 예상 속도는 Ollama 기준이에요.`, `${catalog.evidence.local_calibration_samples} local calibration runs · Estimated speed is based on Ollama.`)}</p>}<p>{L('전체 자료의 실측 수는 내 컴퓨터에서의 정확도를 뜻하지 않아요.',"The total number of measurements doesn't mean accuracy on your computer.")}</p></section>;
}

function Icon({name}) {
  const paths = {wiki:<><path d="M3 4h7c2 0 2 2 2 2s0-2 2-2h7v16h-7c-2 0-2 2-2 2s0-2-2-2H3Z"/><path d="M12 6v16"/></>,recommend:<><path d="M3 10 12 3l9 7v11h-6v-7H9v7H3Z"/></>,models:<><path d="m12 3 9 5-9 5-9-5Z"/><path d="M3 8v10l9 5 9-5V8M12 13v10"/></>,jobs:<><path d="M8 5h13M8 12h13M8 19h13"/><circle cx="3" cy="5" r=".8"/><circle cx="3" cy="12" r=".8"/><circle cx="3" cy="19" r=".8"/></>,search:<><circle cx="10" cy="10" r="6"/><path d="m15 15 6 6"/></>};
  return <svg aria-hidden="true" viewBox="0 0 24 24" width="21" height="21" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">{paths[name]}</svg>;
}

function Hardware({machine, budget}) {
  const fields = [['CPU',machine?.cpu||L('확인 중', 'Checking')],[L('메모리', 'Memory'),machine ? L(`${gib(machine.ram_available_gb)} 사용 가능 / ${gib(machine.ram_total_gb)}`, `${gib(machine.ram_available_gb)} available / ${gib(machine.ram_total_gb)}`) : L('확인 중', 'Checking')],['GPU',machine?.gpu_name|| (machine ? L('CPU로 실행', 'Runs on CPU') : L('확인 중', 'Checking'))],[L('설치 예산', 'Install budget'),gib(budget ?? machine?.model_budget_gb)]];
  return <div className="hardware">{fields.map(([label,value])=><div key={label}><span>{label}</span><strong>{value}</strong></div>)}</div>;
}

function ModelTable({rows, onSelect, onDetails}) {
  return <div className="table-wrap"><table><thead><tr><th>{L('모델','Model')}</th><th>{L('용도','Use')}</th><th>{L('예상 메모리','Est. memory')}</th><th>{L('예측 근거','Basis')}</th><th><span className="sr-only">{L('작업','Actions')}</span></th></tr></thead><tbody>{rows.map((model,index)=><tr key={model.id}><td><strong>{model.name}</strong><small>{model.quantization}</small><button className="text-button" onClick={()=>onDetails(model.wiki || {missing:true,name:model.name,ref:model.ref})}>{L('모델 설명','Model details')}</button><small>{wikiSummary(model.wiki)}</small>{model.variant_warning && <small>{model.variant_warning}</small>}</td><td>{purposes[model.use_case] || model.use_case}</td><td>{gib(model.memory_required_gb)}</td><td><span>{evidenceLabels[model.evidence.status] || L('실측 근거 부족', 'Not enough measurements')}</span><small>{evidenceBasis[model.evidence.basis] || L('근거 부족', 'Not enough evidence')}{L(' · 보정된 정확도 없음',' · No calibrated accuracy')}</small>{model.evidence.matching_feature_configurations && <small>{L(`같은 특성의 학습 구성 ${model.evidence.matching_feature_configurations.training} · 검증 구성 ${model.evidence.matching_feature_configurations.holdout}`, `Same-feature configurations: ${model.evidence.matching_feature_configurations.training} training · ${model.evidence.matching_feature_configurations.holdout} holdout`)}</small>}{typeof model.predicted_tokens_per_second==='number' && <small>{L(`약 ${model.predicted_tokens_per_second.toFixed(1)} tok/s 예상 · Ollama`, `About ${model.predicted_tokens_per_second.toFixed(1)} tok/s expected · Ollama`)}</small>}</td><td>{!model.installed && <button className="primary" onClick={()=>onSelect({...model,operation:'start_chat'})}>{L('이 모델로 대화 시작','Start chatting with this model')}</button>}<button onClick={()=>onSelect(model)}>{model.installed?L('설치 정보', 'Install info'):L('설치', 'Install')}</button></td></tr>)}</tbody></table></div>;
}

function Installed({rows,onAction,onSettings,onChat}) {
  if (!rows.length) return <div className="empty"><h2>{L('아직 설치한 모델이 없어요','No models installed yet')}</h2><p>{L('추천에서 모델을 선택하면 이곳에서 연결된 실행 앱과 저장 상태를 확인할 수 있어요.','Pick a model from Recommended to see its linked apps and storage status here.')}</p></div>;
  return <div className="installed-list">{rows.map(model=><article key={model.id}><div><h2>{model.filename}</h2><p>{bytes(model.size_bytes)} · {model.engines.length ? model.engines.join(', ') : L('연결된 실행 앱 없음', 'No linked apps')}{!model.exists && L(' · 파일 없음', ' · File missing')}</p><small>{Object.entries(model.compatibility || {}).map(([engine,result])=>`${engine}: ${result.status==='passed'?L('실행 확인 통과', 'Run check passed'):L('실행 확인 실패', 'Run check failed')}`).join(' · ') || L('아직 모델 실행을 확인하지 않았어요.', 'Model run not verified yet.')}</small></div><div className="row-actions"><button className="primary" onClick={()=>onAction({...model,operation:'start_chat'})} disabled={!model.exists}>{L('대화 준비하고 시작','Prepare and start chat')}</button><button onClick={()=>onChat(model)} disabled={!model.exists || !model.engines.some(e=>['ollama','lmstudio'].includes(e))}>{L('대화','Chat')}</button><button onClick={()=>onSettings(model)} disabled={!model.exists || !model.engines.some(e=>['ollama','lmstudio'].includes(e))}>{L('실행 설정','Run settings')}</button><button onClick={()=>onAction({...model,operation:'verify'})} disabled={!model.exists || !model.engines.some(e=>['ollama','lmstudio'].includes(e))}>{L('실행 확인','Verify')}</button><button onClick={()=>onAction({...model,operation:'link'})} disabled={!model.exists}>{L('연결 확인','Check links')}</button><button className="danger" onClick={()=>onAction({...model,operation:'uninstall'})}>{L('삭제','Delete')}</button></div></article>)}</div>;
}

function Jobs({jobs,onCancel,onRetry,onChat,onRecover}) {
  if (!jobs.length) return <div className="empty"><h2>{L('진행한 작업이 없어요','No tasks yet')}</h2><p>{L('설치와 연결, 삭제 결과가 여기에 저장돼요.','Install, link, and delete results are saved here.')}</p></div>;
  return <div className="job-list">{jobs.map(job=><article key={job.id}><div><div className="job-title"><h2>{job.filename}</h2><strong className={`status ${job.status}`}>{statuses[job.status]||job.status}</strong></div><p>{operationLabels[job.operation] || job.operation}</p>{job.bytes_received>0 && <p>{L('받은 데이터 ','Received ')}{bytes(job.bytes_received)}{job.total_bytes ? L(` / 파일 크기 ${bytes(job.total_bytes)}`, ` / File size ${bytes(job.total_bytes)}`) : ''}</p>}{job.operation!=='document_import' && job.total_count>0 && <p>{L('측정한 모델 ','Models measured ')}{job.completed_count || 0}/{job.total_count}</p>}<DocumentProgress job={job}/>{job.result?.saved && <p>{L('실제 설정과 응답을 확인하고 실행 설정을 저장했어요.','Checked the applied settings and response, then saved the run settings.')}</p>}{job.result?.restored && <p>{L('이전 실행 설정으로 복원했어요.','Restored the previous run settings.')}</p>}{job.result?.report_filename && <p>{L('비교 결과 저장: ','Comparison saved: ')}{job.result.report_filename}</p>}{job.operation==='import_scan' && job.result && <p>{L(`외부 모델 ${job.result.count}개를 찾았어요. 파일·설정에서 가져올 모델을 선택하세요.`, `Found ${job.result.count} external models. Choose which to import in Files & settings.`)}</p>}{job.result?.warnings?.map((text,index)=><p key={index}>{text}</p>)}{job.operation==='start_chat' && <TaskProgress job={job}/>} {job.operation==='start_chat' && ['failed','cancelled','interrupted'].includes(job.status) && <button onClick={()=>onRetry(job)}>{L('다시 시작','Restart')}</button>} {job.operation==='start_chat' && job.status==='completed' && job.result?.chat_ready && <button className="primary" onClick={()=>onChat(job)}>{L('대화 열기','Open chat')}</button>} {job.message && <p className="job-message">{job.message}</p>}{job.status==='failed' && <RecoveryActions reason={job.failure?.reason} engine={job.engine || job.task_request?.engine} onRecover={onRecover}/>}{job.result?.server_ready && <p role="status">{job.result.reused?L('이미 켜진 Ollama 서버를 재사용해요.', 'Reusing the Ollama server that was already running.'):L('Ollama 서버가 응답했어요.', 'The Ollama server responded.')}{L(' 모델 준비가 중단됐었다면 해당 작업의 ‘다시 시작’을 누르세요.',' If model setup was interrupted, select ‘Restart’ on that task.')}</p>}{job.result?.linked && <p>{L('연결된 앱: ','Linked apps: ')}{Object.entries(job.result.linked).filter(([,ok])=>ok).map(([name])=>name).join(', ') || L('없음 — 내 모델에서 연결 상태를 확인해 주세요.', 'None — check link status in My models.')}</p>}{job.operation==='install' && job.result?.runtime_verified===false && <small>{L('파일 설치 결과를 확인했어요. 실제 모델 실행은 아직 확인하지 않았어요.',"Checked the file install. The model hasn't been run yet.")}</small>}{job.result?.response && <div className="probe-response"><strong>{L('실제 응답','Actual response')}</strong><pre>{job.result.response}</pre></div>}{job.result?.released_test_load && <small>{L('응답 확인 후 검증용 모델의 메모리를 해제했어요.','Freed the test model from memory after checking the response.')}</small>}{job.result?.model_was_preloaded && <small>{L('이미 로딩되어 있던 모델과 설정을 유지했어요.','Kept the model that was already loaded and its settings.')}</small>}</div>{['install','compare','start_chat'].includes(job.operation) && ['queued','running'].includes(job.status) && <button onClick={()=>onCancel(job.id)}>{L('중단','Stop')}</button>}</article>)}</div>;
}

function Confirmation({model,machine,onClose,onSubmit,busy,error}) {
  const operation=model.operation || 'install';
  const needsEngine=['install','link','verify','profile_save','profile_restore','start_chat'].includes(operation);
  const runtimeOnly=['verify','profile_save','profile_restore'].includes(operation);
  const taskFlow=operation==='start_chat';
  const engines=(machine?.engines || []).filter(x=>(taskFlow ? ['ollama','lmstudio'].includes(x.key) : x.installed) && (!runtimeOnly || ['ollama','lmstudio'].includes(x.key) && model.engines?.includes(x.key)));
  const [engine,setEngine]=useState(model.engine || (runtimeOnly || taskFlow ? engines.find(x=>x.installed)?.key || engines[0]?.key || '' : ''));
  const explanations={
    engine_start:L('Ollama의 로컬 서버를 확인하고, 꺼져 있으면 필수 앱 안전 검사 후 백그라운드에서 시작해요. 이미 켜진 서버와 모델은 유지해요. 관리 서버를 종료하면 여기서 새로 시작한 서버만 종료해요.', "Checks the Ollama local server. If it's off, runs the required app safety check and starts it in the background. A server and models already running are kept. Stopping the manager server stops only a server started here."),
    start_chat:L('실행 앱 설치·안전 검사, 모델 다운로드·연결, 짧은 실제 응답 검사를 한 번에 진행한 뒤 채팅을 열어요. 앱 설치·다운로드·모델 메모리 로딩에 동의해요. 앱의 로컬 서버가 켜져 있어야 하며, 준비되지 않았으면 다운로드 전에 중단해요. 파일은 재시도할 때 재사용하고 외부로 응답을 업로드하지 않아요.', "Installs and safety-checks the app, downloads and links the model, checks a short real response, then opens chat. You agree to the app install, download, and loading the model into memory. The app's local server must be on; if it isn't ready, this stops before downloading. Files are reused on retry, and responses are never uploaded."),
    install:L('파일을 내려받고 선택한 실행 앱에 연결해요. 설치만으로 모델 실행을 확인하지는 않아요.', "Downloads the file and links it to the selected app. Installing alone doesn't verify the model runs."),
    link:L('기존 파일을 사용해 실행 앱 연결을 확인해요.', 'Checks the app link using the existing file.'),
    uninstall:L('OMM이 관리하는 모델 파일과 기록된 연결을 삭제해요.', 'Deletes the model file OMM manages and its recorded links.'),
    verify:L('짧은 실제 응답을 확인해요. 검증 때문에 새로 로딩한 모델만 해제하고 기존 로딩은 유지해요.', 'Checks a short real response. Only a model loaded for this check is unloaded; models already loaded stay.'),
    compare:L('선택한 모델로 8문항 산술 점검과 3회 속도 측정을 실행해요. 몇 분 걸릴 수 있으며 결과는 이 컴퓨터에 저장돼요. 다른 모델이 로딩되어 있으면 중단해요.', 'Runs an 8-question arithmetic check and 3 speed runs on the selected models. This can take a few minutes, and results are saved on this computer. Stops if another model is loaded.'),
    profile_save:L('기준 설정과 추천 설정으로 각각 짧은 실제 응답을 확인해요. 적용된 설정과 메모리 해제를 확인한 뒤 저장해요. 기존 로딩이 있으면 중단해요.', 'Checks a short real response with the baseline and the recommended settings. Saves after confirming the applied settings and memory release. Stops if a model is already loaded.'),
    profile_restore:L('이 모델과 실행 앱에 저장된 이전 설정을 복원해요. 모델을 로딩하거나 다른 모델의 설정을 바꾸지 않아요.', "Restores the previous settings saved for this model and app. Doesn't load the model or change other models' settings."),
    engine_install:L('선택한 실행 앱을 공식 설치 경로로 설치해요. 다운로드와 운영체제의 설치 확인이 필요할 수 있어요.', 'Installs the selected app from its official source. A download and an OS install prompt may be needed.'),
    import_scan:L('실행 앱의 모델 폴더를 검색하고 파일을 확인해요. 모델 파일을 옮기거나 삭제하지 않아요.', "Scans the apps' model folders and checks the files. Model files aren't moved or deleted."),
    import:L('선택한 파일을 OMM 모델 폴더로 가져오고 기존 실행 앱에는 연결을 남겨요. 원래 위치의 파일이 연결로 바뀔 수 있어요.', 'Imports the selected files into the OMM model folder and leaves links in the original apps. Files in the original location may be replaced by links.'),
    cleanup:L('선택한 불완전 다운로드만 삭제해요. 해당 파일로 다운로드를 재개할 수 없게 돼요.', "Deletes only the selected incomplete downloads. Those downloads can't be resumed afterward.")
  };
  const dialog=useRef(null);useEffect(()=>{dialog.current?.showModal();},[]);
  return <dialog ref={dialog} onCancel={event=>{if(busy)event.preventDefault();else onClose();}}><form onSubmit={event=>{event.preventDefault();onSubmit(engine);}}><h2>{taskFlow?L('이 모델로 대화 시작', 'Start chatting with this model'):operationLabels[operation]}{L(' 진행','')}</h2><p className="filename">{model.filename}</p><p>{explanations[operation]}</p>{error && <p role="alert" className="error">{error}</p>}{needsEngine && <label>{L('실행 앱','App')}<select value={engine} onChange={event=>setEngine(event.target.value)}>{!runtimeOnly && !taskFlow && <option value="">{L('설치된 실행 앱 모두','All installed apps')}</option>}{engines.map(x=><option key={x.key} value={x.key}>{x.name}</option>)}</select></label>}<div className="dialog-actions"><button type="button" disabled={busy} onClick={onClose}>{L('취소','Cancel')}</button><button className={['uninstall','cleanup'].includes(operation)?'danger-fill':'primary'} disabled={busy || ((runtimeOnly||taskFlow)&&!engine)}>{busy?L('요청 중…', 'Requesting…'):L('확인하고 시작', 'Confirm and start')}</button></div></form></dialog>;
}

function FirstUse({machine,onModels,onAction}) {
  const available=(machine?.engines || []).filter(e=>e.installed && ['ollama','lmstudio'].includes(e.key));
  return <section className="first-use"><h2>{L('처음 시작할 때','Getting started')}</h2>
    <p>{L('1. Ollama 서버 시작 도우미를 사용하거나 LM Studio의 로컬 서버를 켜세요.','1. Use the Ollama server helper, or turn on the LM Studio local server.')}</p>
    <p>{L('2. 추천 모델의 ‘이 모델로 대화 시작’을 누르고 실행 앱을 선택하세요.','2. On a recommended model, select ‘Start chatting with this model’ and choose an app.')}</p>
    <p>{L('3. 안전 검사·모델 준비·실제 응답 확인을 마치면 대화로 이어져요.','3. After the safety check, model setup, and response check, the chat opens.')}</p>
    <button onClick={()=>onAction({operation:'engine_start',engine:'ollama',filename:L('Ollama 로컬 서버', 'Ollama local server')})}>{L('Ollama 서버 켜기','Start Ollama server')}</button>
    {available.length ? <small>{L('설치된 실행 앱: ','Installed apps: ')}{available.map(e=>e.name).join(', ')}</small> : <a href="https://github.com/omm-hippo/omm#installation" target="_blank" rel="noreferrer">{L('설치 안내 보기','View install guide')}</a>}
    <button className="text-button" onClick={onModels}>{L('내 모델 확인','View my models')}</button>
  </section>;
}

function WikiDetail({model,onClose}) {
  const dialog=useRef(null);useEffect(()=>{dialog.current?.showModal();},[]);
  return <dialog className="wiki-dialog" ref={dialog} onCancel={onClose}><h2>{model.name}</h2>
    {model.missing ? <><p>{L('이 설치 대상에 정확하게 연결된 위키 설명이 아직 없어요.',"There's no wiki entry matched to this download yet.")}</p><p className="filename">{model.ref}</p></> : <>
      <p>{model.summary}</p>
      <dl><dt>{L('개발사 · 규모 표기','Publisher · Stated size')}</dt><dd>{model.publisher} · {model.sizeLabel || L('미확인', 'Unknown')}</dd>
      <dt>{L('구조','Architecture')}</dt><dd>{model.architecture==='moe'?'MoE':model.architecture==='dense'?'Dense':L('미확인', 'Unknown')}</dd>
      <dt>{L('공개 문서·설정의 문맥 길이','Context length (public docs/config)')}</dt><dd>{Number.isInteger(model.context?.tokens)?L(`${model.context.tokens.toLocaleString()} 토큰`, `${model.context.tokens.toLocaleString()} tokens`):L('미확인', 'Unknown')}</dd>
      <dt>{L('확인된 입력','Confirmed inputs')}</dt><dd>{(model.inputs || []).join(', ') || L('미확인', 'Unknown')}</dd>
      <dt>{L('도구 호출','Tool calling')}</dt><dd>{model.toolCalling===true?L('개발사 지원 표기 · 실행 미검증', 'Supported per publisher · Not verified by running'):model.toolCalling===false?L('개발사 미지원 표기', 'Not supported per publisher'):model.toolFormatDeclared?L('공식 템플릿에 호출 형식 포함 · 작업 성공 미검증', 'Call format in official template · Task success not verified'):L('미확인', 'Unknown')}</dd>
      <dt>{L('라이선스 표기','Stated license')}</dt><dd><a href={model.license?.url} target="_blank" rel="noreferrer">{model.license?.label || L('미확인', 'Unknown')}</a></dd></dl>
      <h3>{L('선택할 때','When to choose')}</h3><p>{model.chooseWhen}</p>
      <h3>{L('특징과 주의점','Strengths and cautions')}</h3>{[...model.strengths,...model.cautions].map((claim,index)=><p key={index}><small>{claim.basis==='publisher'?L('개발사 설명', 'Publisher'):L('편집 의견', 'Editor note')}</small>{claim.text}</p>)}
      <h3>{L('실행 조건','Requirements')}</h3><p>{model.runtimeNote}</p>
      {['declared_base_repository','publisher_declared_base'].includes(model.match) && <p>{L('원본 체크포인트 설명을 연결했어요. 변환본의 양자화·실행 앱 호환성은 따로 확인해야 해요.',"This links the original checkpoint's description. Check the converted file's quantization and app compatibility separately.")}</p>}
      <p className="filename">{model.packageRepository || model.repository}</p>
      <h3>{L('출처','Sources')}</h3>{model.sources.map(source=><p key={source.id}><a href={source.url} target="_blank" rel="noreferrer">{source.title}</a></p>)}
      <small>{L(`검토 ${model.reviewedAt} · OMM 실측 품질 평가 없음`, `Reviewed ${model.reviewedAt} · No OMM-measured quality rating`)}</small>{model.stale && <p>{L('검토 후 시간이 지난 자료예요. 현재 지원 여부는 출처에서 다시 확인해 주세요.','This entry was reviewed a while ago. Check the sources for current support.')}</p>}
    </>}
    <div className="dialog-actions"><button onClick={onClose}>{L('닫기','Close')}</button></div>
  </dialog>;
}

function App() {
  const readTab = ()=>destinations.some(item=>item.id===location.hash.slice(1)) ? location.hash.slice(1) : 'wiki';
  const [tab,setCurrentTab] = useState(readTab);
  const [sidebar,setSidebar]=useState(()=>!window.matchMedia('(max-width: 760px)').matches);
  const [mobile,setMobile]=useState(()=>window.matchMedia('(max-width: 760px)').matches);
  const [palette,setPalette]=useState(false);
  useEffect(()=>{
    const media=window.matchMedia('(max-width: 760px)');
    const changed=event=>{setMobile(event.matches);setSidebar(!event.matches);};
    media.addEventListener('change',changed);
    return ()=>media.removeEventListener('change',changed);
  },[]);
  const [appearance,setAppearance]=useState(readAppearance);
  const [visited,setVisited]=useState(()=>new Set([readTab()]));
  useEffect(()=>{setVisited(old=>old.has(tab)?old:new Set([...old,tab]));},[tab]);
  useEffect(()=>{
    document.documentElement.dataset.appearance=appearance;
    try {localStorage.setItem('omm.web.appearance.v1',appearance);}catch {}
  },[appearance]);
  useEffect(()=>{
    function key(event){
      if((event.metaKey || event.ctrlKey) && event.key.toLowerCase()==='k' && !event.isComposing){
        // Never compete with an installation/approval dialog.
        if(document.querySelector('dialog[open]:not(.command-palette)'))return;
        event.preventDefault();setPalette(value=>!value);
      }
      if(event.key==='Escape' && window.matchMedia('(max-width: 760px)').matches)setSidebar(false);
    }
    window.addEventListener('keydown',key);return ()=>window.removeEventListener('keydown',key);
  },[]);
  function setTab(value) {
    setCurrentTab(value);
    if(window.matchMedia('(max-width: 760px)').matches)setSidebar(false);
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
  const [settingsModel,setSettingsModel]=useState(null);
  const [chatModel,setChatModel]=useState(null);
  const [preparedTask,setPreparedTask]=useState(null);
  const pendingTask=useRef(null);
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
        const prepared=history.find(job=>job.id===pendingTask.current && job.status==='completed' && job.result?.chat_ready);
        if(prepared){pendingTask.current=null;openPreparedChat(prepared);}
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
      const body={operation:selected.operation||'install',confirmed:true,request_id:operationId.current};
      if(selected.id)body.id=selected.id;
      if(selected.ids)body.ids=selected.ids;
      if(engine)body.engine=engine;
      const job=await api(selected.retryJob?`/api/jobs/${selected.retryJob}/retry`:'/api/jobs',selected.retryJob?{confirmed:true,request_id:operationId.current}:body);
      if(job.operation==='start_chat')pendingTask.current=job.id;
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
  const titles={wiki:L('모델 찾기', 'Find models'),recommend:L('내 컴퓨터에 맞는 추천', 'Recommended for this computer'),models:L('내 모델', 'My models'),chat:L('로컬 모델과 대화', 'Chat with a local model'),shared:L('공유 AI', 'Shared AI'),documents:L('내 문서', 'My documents'),comparison:L('성능 비교', 'Performance comparison'),jobs:L('작업', 'Tasks'),connections:L('연결 상태', 'Connections'),diagnostics:L('진단', 'Diagnostics'),settings:L('파일·설정', 'Files & settings')};
  const subtitles={wiki:L('모델 위키에서 용도와 특징을 살펴보고 설치할 파일을 선택하세요.', 'Browse uses and traits in the model wiki, then choose a file to install.'),recommend:L('현재 메모리 여유에 맞는 후보와 추천 근거를 확인하세요.', 'See models that fit your free memory and why they were picked.'),models:L('설치된 모델의 연결, 실행과 설정을 관리하세요.', 'Manage links, runs, and settings for installed models.'),chat:L('모델의 실제 응답을 받고 대화를 이어가세요.', 'Get real responses from a model and keep chatting.'),shared:L('팀의 공유 서버에 연결하고 모델의 실제 답변을 확인하세요.', "Connect to your team's shared server and see the model's real answers."),documents:L('등록한 문서를 로컬 모델에 질문하고 원문 출처를 확인해요.', 'Ask a local model about your documents and see the original sources.'),comparison:L('실제 로컬 응답으로 속도와 산술 점검 결과를 비교하세요.', 'Compare speed and arithmetic check results using real local responses.'),jobs:L('진행 상황과 저장된 작업 결과를 확인하세요.', 'See progress and saved task results.'),connections:L('실행 앱과 로컬 서버, 다운로드 서버의 연결을 확인하세요.', 'Check connections to apps, local servers, and download servers.'),diagnostics:L('문제가 있는 지점과 해결 방법을 확인하세요.', 'See what went wrong and how to fix it.'),settings:L('저장 공간과 기존 모델, 데이터 공유 설정을 관리하세요.', 'Manage storage, existing models, and data sharing settings.')};
  const title=titles[tab],subtitle=subtitles[tab];
  const visible = catalog?.models || [];
  function openPreparedChat(job){setPreparedTask({...job.result,id:crypto.randomUUID(),job_id:job.id});setChatModel({id:job.result.model_id,filename:job.result.filename,engines:[job.result.engine],exists:true});setRefreshKey(key=>key+1);setTab('chat');}
  function recover(action){
    if(action==='start_ollama')return choose({operation:'engine_start',engine:'ollama',filename:L('Ollama 로컬 서버', 'Ollama local server')});
    if(action==='smaller'){setProfile('minimal');setQuery('');setNotice(L('가볍게 사용 예산으로 후보를 다시 골랐어요. 필요한 메모리를 비교해 더 작은 모델을 선택하세요.', 'Picked models again with the Light use budget. Compare the memory needed and choose a smaller model.'));setTab('recommend');return;}
    if(action==='storage'){setNotice(L('정리할 파일을 먼저 확인하세요. 선택하고 확인하기 전에는 삭제하지 않아요.', 'Review the files to clean up first. Nothing is deleted until you select and confirm.'));setTab('settings');return;}
    setTab(action);
  }
  function choose(model){
    setError('');
    setNotice('');
    if(model.installed && model.operation==='start_chat' && model.managed_by_omm){
      const managed=models.find(row=>row.filename===model.filename && row.exists);
      if(managed){operationId.current=null;setSelected({...managed,operation:'start_chat'});return;}
    }
    if(model.installed){
      if(!model.managed_by_omm)setNotice(L(`${model.name}은 ${model.installed_engines.join(', ') || '다른 실행 앱'}에 설치되어 있어요. OMM 관리 목록에는 포함되지 않아 여기서 삭제할 수 없어요.${model.installation_match==='model_identity'?' 같은 모델 계열이 확인됐고 양자화 형식은 다를 수 있어요.':''}`, `${model.name} is installed in ${model.installed_engines.join(', ') || 'another app'}. It isn't in the OMM managed list, so it can't be deleted here.${model.installation_match==='model_identity'?' The same model family was found, but the quantization may differ.':''}`));
      setTab('models');setRefreshKey(key=>key+1);return;
    }
    operationId.current=null;setSelected(model);
  }
  return <div className={`app workspace ${sidebar?'sidebar-open':'sidebar-closed'} ${tab==='chat'?'chat-workspace':''}`}>
    {sidebar && <button className="sidebar-backdrop" aria-label={L('메뉴 닫기','Close menu')} onClick={()=>setSidebar(false)}/>}
    <aside id="workspace-sidebar" aria-label={L('OMM 작업 공간','OMM workspace')} hidden={!sidebar}>{mobile && <button className="icon-button sidebar-dismiss" aria-label={L('사이드바 닫기','Close sidebar')} onClick={()=>setSidebar(false)}><WorkspaceIcon name="close"/></button>}<Sidebar tab={tab} onNavigate={setTab} onSearch={()=>setPalette(true)} appearance={appearance} onAppearance={setAppearance} activeJobs={jobs.filter(j=>['queued','running','cancelling'].includes(j.status)).length}/></aside>
    <main id="workspace-main" inert={mobile && sidebar?true:undefined}><div className="workspace-topbar"><div className="workspace-location"><button className="icon-button" aria-label={sidebar?L('사이드바 접기', 'Collapse sidebar'):L('사이드바 펼치기', 'Expand sidebar')} aria-expanded={sidebar} aria-controls="workspace-sidebar" onClick={()=>setSidebar(value=>!value)}><WorkspaceIcon name="menu"/></button><span>{L('내 작업 공간','My workspace')}</span><span className="breadcrumb-separator">/</span><strong>{destinations.find(x=>x.id===tab)?.label}</strong></div><button className="icon-button" aria-label={L('메뉴 검색 열기','Open menu search')} title={L('메뉴 검색 (⌘/Ctrl K)','Search menu (⌘/Ctrl K)')} onClick={()=>setPalette(true)}><WorkspaceIcon name="search"/></button></div>
    <div className="workspace-content"><header><div><h1>{title}</h1><p>{subtitle}</p></div>{tab!=='shared'&&<button className="refresh-button" onClick={()=>setRefreshKey(key=>key+1)} disabled={loading}><WorkspaceIcon name="refresh" size={16}/>{loading?L('확인 중…', 'Checking…'):L('다시 확인', 'Check again')}</button>}</header>{error && <div role="alert" className="error">{error}</div>}{notice && <div role="status" className="notice">{notice}</div>}{['wiki','recommend','models'].includes(tab) && <Hardware machine={machine} budget={catalog?.budget_gb}/>}
    {tab==='wiki' && <WikiExplorer document={wiki} profile={profile} onOpen={setDescription} onSelect={choose}/>}
    {tab==='recommend' && <>{!models.length && <FirstUse machine={machine} onAction={choose} onModels={()=>setTab('models')}/>}<div className="toolbar"><div className="profiles" aria-label={L('메모리 사용 방식','Memory usage mode')}>{profiles.map(([key,label])=><button key={key} aria-pressed={profile===key} onClick={()=>setProfile(key)}>{label}</button>)}</div><label className="search"><Icon name="search"/><input maxLength={160} value={query} onChange={event=>setQuery(event.target.value)} placeholder={L('모델 이름으로 찾기','Search by model name')} aria-label={L('모델 이름으로 찾기','Search by model name')}/></label></div><ModelGuide profile={profile} refreshKey={refreshKey} onSelect={choose} onDetails={setDescription}/>{loading?<div className="empty" role="status">{L('컴퓨터와 추천 자료를 확인하고 있어요.','Checking your computer and recommendation data.')}</div>:visible.length?<ModelTable rows={visible} onSelect={choose} onDetails={setDescription}/>:<div className="empty"><h2>{query?L('검색 결과가 없어요','No results'):L('현재 예산에 맞는 모델이 없어요', 'No models fit the current budget')}</h2><p>{query?L('다른 이름으로 검색해 보세요.','Try a different name.'):L('사용 방식을 바꾸거나 다른 앱을 닫은 뒤 다시 확인해 주세요.', 'Change the usage mode or close other apps, then check again.')}</p></div>}<p className="footnote">{L('예상 속도는 실제 측정과 다를 수 있어요.','Estimated speed may differ from actual measurements.')}</p><Evidence catalog={catalog} onUpdate={updateCatalog} busy={updating}/></>}
    {tab==='models' && <Installed rows={models} onAction={choose} onSettings={setSettingsModel} onChat={model=>{setChatModel(model);setTab('chat');}}/>}
    {(visited.has('chat') || tab==='chat') && <div hidden={tab!=='chat'}><ChatPanel onRecover={recover} models={models} initialModel={chatModel} preparedTask={preparedTask} onModelStatusChange={()=>setRefreshKey(key=>key+1)} onModels={()=>setTab('models')}/></div>}
    {(visited.has('shared') || tab==='shared') && <div hidden={tab!=='shared'}><SharedAI/></div>}
    {(visited.has('documents') || tab==='documents') && <div hidden={tab!=='documents'}><DocumentPanel models={models} onJobs={setJobs}/></div>}
    {tab==='jobs' && <Jobs jobs={jobs} onRecover={recover} onCancel={cancel} onChat={openPreparedChat} onRetry={job=>choose({operation:'start_chat',filename:job.filename,engine:job.task_request?.engine,retryJob:job.id})}/>}
    {tab==='connections' && <Connections refreshKey={refreshKey} onAction={choose}/>}
    {tab==='diagnostics' && <Diagnostics refreshKey={refreshKey}/>}
    {tab==='comparison' && <Comparison models={models} jobs={jobs} onAction={choose}/>}
    {tab==='settings' && <FilesSettings refreshKey={refreshKey} onAction={choose}/>}
    </div></main>{palette && <CommandPalette onClose={()=>setPalette(false)} onNavigate={setTab}/>} {description && <WikiDetail model={description} onClose={()=>setDescription(null)}/>} {settingsModel && <RuntimeSettings model={settingsModel} refreshKey={refreshKey} onClose={()=>setSettingsModel(null)} onAction={choose}/>} {selected && <Confirmation model={selected} machine={machine} busy={submitting} error={error} onClose={()=>{setSelected(null);operationId.current=null;}} onSubmit={submit}/>}</div>;

}

createRoot(document.getElementById('root')).render(<App/>);
