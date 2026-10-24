import React from 'react';

const stages=[['engine','실행 앱 확인'],['safety','앱 안전 검사'],['runtime','로컬 서버 확인'],['install','모델 파일 준비'],['link','실행 앱 연결'],['verify','실제 응답 확인']];

export function TaskProgress({job}) {
  return <div className="task-progress"><ol aria-label="대화 준비 단계">{stages.map(([key,label])=>{
    let state=job.steps?.[key] || 'waiting';
    if(state==='running' && ['failed','cancelled','interrupted'].includes(job.status))state=job.status;
    const text={completed:'완료',running:'진행 중',failed:'실패',cancelled:'중단',interrupted:'다시 시작 필요',waiting:'대기'}[state];
    return <li key={key} data-state={state}><span>{label}</span><small>{text}</small></li>;
  })}</ol>{job.status==='cancelling' && <p role="status">중단을 요청했어요. 진행 중인 설치·안전 검사가 끝나면 다음 단계를 실행하지 않아요.</p>}</div>;
}
