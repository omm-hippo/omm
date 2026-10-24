// Only typed server reasons select actions. Error text is never parsed.
export function recoveryActions(reason,engine) {
  if(reason==='server_unavailable')return [
    ...(engine==='ollama'?[{id:'start_ollama',label:'Ollama 서버 켜기'}]:[]),
    {id:'connections',label:'연결 상태 확인'}];
  if(reason==='out_of_memory')return [{id:'smaller',label:'더 작은 모델 찾기'}];
  if(reason==='disk_full')return [{id:'storage',label:'저장 공간·정리 대상 보기'}];
  if(['model_busy','model_not_visible'].includes(reason))return [{id:'connections',label:'연결 상태 확인'},{id:'models',label:'내 모델 확인'}];
  return [{id:'diagnostics',label:'진단에서 원인 확인'}];
}
