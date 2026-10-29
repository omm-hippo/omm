import {L} from './i18n.js';

export const destinations = [
  {id:'wiki', label:L('모델 찾기','Find models'), detail:L('위키에서 용도와 특징 살펴보기','Browse uses and features in the wiki'), icon:'wiki'},
  {id:'recommend', label:L('추천','Recommend'), detail:L('내 컴퓨터에 맞는 모델 고르기','Pick models that fit this computer'), icon:'recommend'},
  {id:'models', label:L('내 모델','My models'), detail:L('설치·연결·실행 관리','Manage installs, links, and runs'), icon:'models'},
  {id:'chat', label:L('채팅','Chat'), detail:L('로컬 모델과 대화','Chat with a local model'), icon:'chat'},
  {id:'shared', label:L('공유 AI','Shared AI'), detail:L('팀 서버 연결·이용 토큰·모델 대화','Team server connection, access tokens, model chat'), icon:'shared'},
  {id:'documents', label:L('내 문서','My documents'), detail:L('자료를 등록하고 근거를 찾아 질문','Add files and ask questions with sources'), icon:'documents'},
  {id:'comparison', label:L('성능 비교','Performance'), detail:L('속도와 산술 점검 결과','Speed and arithmetic check results'), icon:'comparison'},
  {id:'jobs', label:L('작업','Jobs'), detail:L('진행 상황과 저장된 결과','Progress and saved results'), icon:'jobs'},
  {id:'connections', label:L('연결 상태','Connections'), detail:L('실행 앱과 서버 확인','Check runner apps and servers'), icon:'connections', secondary:true},
  {id:'diagnostics', label:L('진단','Diagnostics'), detail:L('문제 확인과 해결 방법','Find problems and how to fix them'), icon:'diagnostics', secondary:true},
  {id:'settings', label:L('파일·설정','Files & settings'), detail:L('저장 공간·가져오기·데이터 공유','Storage, import, data sharing'), icon:'settings', secondary:true},
];

export function searchDestinations(query) {
  const words=query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
  return destinations.filter(item=>words.every(word=>`${item.label} ${item.detail}`.toLocaleLowerCase().includes(word)));
}

export function readAppearance() {
  try {
    const value=localStorage.getItem('omm.web.appearance.v1');
    return ['system','light','dark'].includes(value)?value:'system';
  } catch { return 'system'; }
}

export function shouldSend(event) {
  return event.key==='Enter' && !event.shiftKey && !event.altKey &&
    !event.nativeEvent?.isComposing && event.keyCode!==229;
}

// Recommendation evidence and wiki summaries. Counts stay separate and no
// accuracy figure is shown because none is calibrated.
const wikiTasks = {general:L('일반 텍스트','General text'),agents:L('에이전트','Agents'),coding:L('코딩','Coding'),reasoning:L('추론','Reasoning'),vision:L('이미지 입력','Image input'),multilingual:L('다국어','Multilingual'),compact:L('경량','Lightweight')};
const count = (value, unit) => Number.isInteger(value) ? `${value.toLocaleString()}${unit}` : L('확인 불가','Unknown');
export function measurementSummary(m) {
  return L(`원시 측정 ${count(m.raw_rows,'행')}(유효 ${count(m.valid_rows,'행')}) · 반복 측정 병합 ${count(m.repeat_samples_collapsed,'회')} · 서로 다른 구성 ${count(m.distinct_configurations,'개')} · 독립 장비 수 확인 불가 · 보정된 신뢰구간 없음`, `Raw measurements ${count(m.raw_rows,' rows')} (${count(m.valid_rows,' valid')}) · Repeat runs merged ${count(m.repeat_samples_collapsed,' times')} · Distinct setups ${count(m.distinct_configurations,'')} · Independent machine count unknown · No calibrated confidence interval`);
}
export function wikiSummary(wiki) {
  if (!wiki) return L('위키 정보 없음','No wiki info');
  const tasks = (wiki.tasks || []).map(x=>wikiTasks[x] || x).join(' · ') || L('정보 없음','No info');
  return L(`용도: ${tasks} · 검토 ${wiki.reviewedAt || '정보 없음'}`, `Uses: ${tasks} · Reviewed ${wiki.reviewedAt || 'No info'}`);
}
