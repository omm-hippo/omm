import React,{useEffect,useState} from 'react';
import {PackageDialog} from './ManagementPanels';
import {api,gib} from './api';
import {recoveryActions} from './recovery-state.js';
import {L} from './i18n.js';

export function RecoveryActions({reason,engine,onRecover}) {
  return <div className="recovery-actions" aria-label={L('문제 해결','Troubleshooting')}>{recoveryActions(reason,engine).map(action=><button key={action.id} onClick={()=>onRecover(action.id)}>{action.label}</button>)}</div>;
}

export function ModelGuide({profile,refreshKey,onSelect,onDetails}) {
  const [purpose,setPurpose]=useState('');
  const [selected,setSelected]=useState(null);
  const [state,setResource]=useState({});
  const resource=state.purpose===purpose && state.profile===profile ? state : {loading:Boolean(purpose)};
  useEffect(()=>{
    if(!purpose){setResource({});return;}
    const controller=new AbortController();
    setResource({purpose,profile,loading:true});
    api(`/api/guide?purpose=${purpose}&profile=${profile}`,undefined,controller.signal)
      .then(data=>{if(!controller.signal.aborted)setResource({purpose,profile,data});})
      .catch(error=>{if(!controller.signal.aborted)setResource({purpose,profile,error:error.message});});
    return ()=>controller.abort();
  },[purpose,profile,refreshKey]);
  return <section className="model-guide manager-section" aria-label={L('용도별 모델 선택 가이드','Model guide by task')}><h2>{L('어떤 작업을 하고 싶으세요?','What do you want to do?')}</h2><p>{L('용도에 맞는 후보를 최대 3개로 좁히고, 설치 파일의 메모리 조건을 확인해요.','Narrows the options to at most 3 for your task and checks the memory needs of the install file.')}</p><div className="profiles">{[['korean',L('한국어 대화','Korean chat')],['coding',L('코딩','Coding')],['documents',L('문서 질문','Document questions')]].map(([id,label])=><button key={id} aria-pressed={purpose===id} onClick={()=>setPurpose(id)}>{label}</button>)}</div>
    {resource.loading && <p role="status">{L('용도와 메모리를 확인하고 있어요.','Checking your task and memory.')}</p>}{resource.error && <p role="alert" className="error">{resource.error}</p>}
    {resource.data && <><p className="footnote">{resource.data.limitation}</p>{purpose==='documents' && <p>{L('문서 등록과 근거 검색은 ‘내 문서’에서 진행해요. 모델 자체의 PDF·이미지 읽기 지원과는 별개예요.','Add documents and search for sources in ‘My documents’. This is separate from whether the model itself can read PDFs or images.')}</p>}{!resource.data.models.length && <p>{L('로컬 추천 자료만으로 설치 파일과 메모리를 함께 확인할 수 없어, 먼저 살펴볼 위키 후보를 보여줘요. 메모리 적합성은 설치 파일을 찾은 뒤 판단해요.','The local recommendation data alone can’t check install files and memory together, so here are wiki candidates to look at first. Memory fit is checked after an install file is found.')}</p>}
      {!resource.data.models.length && <div className="guide-candidates">{resource.data.wiki_options?.map(model=><article key={model.id}><h3>{model.name}</h3><p>{model.chooseWhen}</p><p className="footnote">{L(`설치 파일·메모리 적합성 미확인 · ${model.sizeLabel}은 모델 크기이며 사용 메모리가 아니에요.`,`Install file and memory fit not checked · ${model.sizeLabel} is the model size, not the memory it uses.`)}</p><div className="row-actions"><button className="primary" onClick={()=>setSelected(model)}>{L('설치 파일 확인','Check install files')}</button><button onClick={()=>onDetails(model)}>{L('근거와 주의점','Sources and caveats')}</button></div></article>)}</div>}
      <div className="guide-candidates">{resource.data.models.map(model=><article key={model.id}><h3>{model.name}</h3><p>{model.quantization} · {L('예상 메모리','Estimated memory')} {gib(model.memory_required_gb)}</p><ul>{model.reasons.map((reason,index)=><li key={index}>{reason}</li>)}</ul><p className="footnote">{L('위키 검토','Wiki reviewed')} {model.wiki.reviewedAt}{model.wiki.stale?L(' · 자료 재확인 필요',' · Needs a recheck'):''} · {L('실행 성능은 사용 전 확인해요.','Performance is checked before use.')}</p><div className="row-actions"><button className="primary" onClick={()=>onSelect({...model,operation:'start_chat'})}>{model.installed?L('이 모델로 대화 준비','Get ready to chat with this model'):L('이 모델로 대화 시작','Start chatting with this model')}</button><button onClick={()=>onDetails(model.wiki)}>{L('근거와 주의점','Sources and caveats')}</button></div></article>)}</div></>}
    {selected && <PackageDialog model={selected} profile={profile} onClose={()=>setSelected(null)} onSelect={row=>{setSelected(null);onSelect(row);}}/>}
  </section>;
}
