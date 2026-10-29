import {L} from './i18n.js';

// Only typed server reasons select actions. Error text is never parsed.
export function recoveryActions(reason,engine) {
  if(reason==='server_unavailable')return [
    ...(engine==='ollama'?[{id:'start_ollama',label:L('Ollama 서버 켜기','Start Ollama server')}]:[]),
    {id:'connections',label:L('연결 상태 확인','Check connections')}];
  if(reason==='out_of_memory')return [{id:'smaller',label:L('더 작은 모델 찾기','Find a smaller model')}];
  if(reason==='disk_full')return [{id:'storage',label:L('저장 공간·정리 대상 보기','View storage and cleanup items')}];
  if(['model_busy','model_not_visible'].includes(reason))return [{id:'connections',label:L('연결 상태 확인','Check connections')},{id:'models',label:L('내 모델 확인','Check my models')}];
  return [{id:'diagnostics',label:L('진단에서 원인 확인','Find the cause in Diagnostics')}];
}
