import React,{useEffect,useState} from 'react';
import {PackageDialog} from './ManagementPanels';
import {api,gib} from './api';
import {recoveryActions} from './recovery-state.js';

export function RecoveryActions({reason,engine,onRecover}) {
  return <div className="recovery-actions" aria-label="문제 해결">{recoveryActions(reason,engine).map(action=><button key={action.id} onClick={()=>onRecover(action.id)}>{action.label}</button>)}</div>;
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
  return <section className="model-guide manager-section" aria-label="용도별 모델 선택 가이드"><h2>어떤 작업을 하고 싶으세요?</h2><p>용도에 맞는 후보를 최대 3개로 좁히고, 설치 파일의 메모리 조건을 확인해요.</p><div className="profiles">{[['korean','한국어 대화'],['coding','코딩'],['documents','문서 질문']].map(([id,label])=><button key={id} aria-pressed={purpose===id} onClick={()=>setPurpose(id)}>{label}</button>)}</div>
    {resource.loading && <p role="status">용도와 메모리를 확인하고 있어요.</p>}{resource.error && <p role="alert" className="error">{resource.error}</p>}
    {resource.data && <><p className="footnote">{resource.data.limitation}</p>{purpose==='documents' && <p>문서 등록과 근거 검색은 ‘내 문서’에서 진행해요. 모델 자체의 PDF·이미지 읽기 지원과는 별개예요.</p>}{!resource.data.models.length && <p>로컬 추천 자료만으로 설치 파일과 메모리를 함께 확인할 수 없어, 먼저 살펴볼 위키 후보를 보여줘요. 메모리 적합성은 설치 파일을 찾은 뒤 판단해요.</p>}
      {!resource.data.models.length && <div className="guide-candidates">{resource.data.wiki_options?.map(model=><article key={model.id}><h3>{model.name}</h3><p>{model.chooseWhen}</p><p className="footnote">설치 파일·메모리 적합성 미확인 · {model.sizeLabel}은 모델 크기이며 사용 메모리가 아니에요.</p><div className="row-actions"><button className="primary" onClick={()=>setSelected(model)}>설치 파일 확인</button><button onClick={()=>onDetails(model)}>근거와 주의점</button></div></article>)}</div>}
      <div className="guide-candidates">{resource.data.models.map(model=><article key={model.id}><h3>{model.name}</h3><p>{model.quantization} · 예상 메모리 {gib(model.memory_required_gb)}</p><ul>{model.reasons.map((reason,index)=><li key={index}>{reason}</li>)}</ul><p className="footnote">위키 검토 {model.wiki.reviewedAt}{model.wiki.stale?' · 자료 재확인 필요':''} · 실행 성능은 사용 전 확인해요.</p><div className="row-actions"><button className="primary" onClick={()=>onSelect({...model,operation:'start_chat'})}>{model.installed?'이 모델로 대화 준비':'이 모델로 대화 시작'}</button><button onClick={()=>onDetails(model.wiki)}>근거와 주의점</button></div></article>)}</div></>}
    {selected && <PackageDialog model={selected} profile={profile} onClose={()=>setSelected(null)} onSelect={row=>{setSelected(null);onSelect(row);}}/>}
  </section>;
}
