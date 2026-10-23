export const destinations = [
  {id:'wiki', label:'모델 찾기', detail:'위키에서 용도와 특징 살펴보기', icon:'wiki'},
  {id:'recommend', label:'추천', detail:'내 컴퓨터에 맞는 모델 고르기', icon:'recommend'},
  {id:'models', label:'내 모델', detail:'설치·연결·실행 관리', icon:'models'},
  {id:'chat', label:'채팅', detail:'로컬 모델과 대화', icon:'chat'},
  {id:'documents', label:'내 문서', detail:'자료를 등록하고 근거를 찾아 질문', icon:'documents'},
  {id:'comparison', label:'성능 비교', detail:'속도와 산술 점검 결과', icon:'comparison'},
  {id:'jobs', label:'작업', detail:'진행 상황과 저장된 결과', icon:'jobs'},
  {id:'connections', label:'연결 상태', detail:'실행 앱과 서버 확인', icon:'connections', secondary:true},
  {id:'diagnostics', label:'진단', detail:'문제 확인과 해결 방법', icon:'diagnostics', secondary:true},
  {id:'settings', label:'파일·설정', detail:'저장 공간·가져오기·데이터 공유', icon:'settings', secondary:true},
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
