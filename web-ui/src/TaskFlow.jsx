import React from 'react';
import {L} from './i18n.js';

const stages=[['engine',L('실행 앱 확인','Check runner app')],['safety',L('앱 안전 검사','App safety check')],['runtime',L('로컬 서버 확인','Check local server')],['install',L('모델 파일 준비','Prepare model file')],['link',L('실행 앱 연결','Connect runner app')],['verify',L('실제 응답 확인','Check a real response')]];

export function TaskProgress({job}) {
  return <div className="task-progress"><ol aria-label={L('대화 준비 단계','Chat setup steps')}>{stages.map(([key,label])=>{
    let state=job.steps?.[key] || 'waiting';
    if(state==='running' && ['failed','cancelled','interrupted'].includes(job.status))state=job.status;
    const text={completed:L('완료','Done'),running:L('진행 중','In progress'),failed:L('실패','Failed'),cancelled:L('중단','Stopped'),interrupted:L('다시 시작 필요','Needs restart'),waiting:L('대기','Waiting')}[state];
    return <li key={key} data-state={state}><span>{label}</span><small>{text}</small></li>;
  })}</ol>{job.status==='cancelling' && <p role="status">{L('중단을 요청했어요. 진행 중인 설치·안전 검사가 끝나면 다음 단계를 실행하지 않아요.','Stop requested. After the current install or safety check finishes, the next steps won’t run.')}</p>}</div>;
}
