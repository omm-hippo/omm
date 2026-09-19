# GitHub Project README 자동 동기화 — 설계

날짜: 2026-09-16
상태: 구현 승인됨

## 배경

조직 Project #1(`omm-hippo`의 `Omm Roadmap`)은 오래된 이슈만 담고 있었고,
닫힌 이슈가 `In Progress`에 남거나 최근 열린 이슈가 누락되는 상태였다. Project
README도 수동으로 작성하면 보드의 `Status` 변경과 쉽게 어긋난다.

## 목표 / 비목표

- 목표: `omm-hippo/omm`의 열린 이슈를 Project #1에 빠짐없이 넣고, 이슈 상태와
  Project `Status`의 명백한 불일치를 고치며, 현재 보드에서 Project README를
  결정적으로 생성한다.
- 비목표: 이슈의 우선순위나 구현 순서를 AI가 판단하기, 이슈 본문을 요약하기,
  PR을 보드에 자동 추가하기, 저장소 `README.md`를 자동 커밋하기.

## 동기화 규칙

1. 저장소의 열린 이슈가 보드에 없으면 추가하고 `Todo`로 설정한다.
2. 보드의 닫힌 이슈가 `Done`이 아니면 `Done`으로 설정한다.
3. 다시 열린 이슈가 `Done`이면 `Todo`로 되돌린다.
4. 열린 이슈가 이미 `Todo` 또는 `In Progress`이면 사람의 선택을 보존한다.
5. 다른 저장소의 항목, PR, draft item은 수정하거나 README에 포함하지 않는다.
6. 생성 결과가 기존 README와 같으면 `updateProjectV2`를 호출하지 않는다.

## README 형식

- 고정된 상태 정의
- `In Progress`인 열린 이슈 목록
- `Todo`인 열린 이슈 목록
- `Done` 항목 개수와 전체 Project 링크

이슈 제목은 줄바꿈을 제거하고 Markdown 링크 텍스트를 탈출한다. 이슈 본문과 댓글은
조회하지 않아 프롬프트 주입성 콘텐츠나 불필요한 개인정보가 README로 복사되지 않게
한다. 날짜를 넣지 않아 변경이 없는 날에는 쓰기가 발생하지 않는다.

## 실행 방식

`.github/workflows/project-readme-sync.yml`이 다음 때 실행한다.

- 이슈 생성·수정·재개·종료·라벨/담당자/마일스톤 변경·이관
- 매일 00:17 KST(15:17 UTC): Project에서 사람이 직접 바꾼 `Status`도 반영
- 수동 `workflow_dispatch`

매시 정각 예약을 피하고, `concurrency`로 동시 실행을 직렬화한다.

## 인증과 권한

조직 Project는 저장소 기본 `GITHUB_TOKEN`으로 접근할 수 없으므로 전용 GitHub App을
사용한다.

- App 소유자: `omm-hippo`
- 저장소 접근: `omm` 하나
- Repository permission: Issues read
- Organization permission: Projects read/write
- Webhook: 사용하지 않음
- Repository variable: `OMM_PROJECTS_APP_CLIENT_ID`
- Repository secret: `OMM_PROJECTS_APP_PRIVATE_KEY`

워크플로는 짧게 만료되는 installation token만 생성한다. 기존 범용 PAT는 재사용하지
않고, 토큰이나 private key를 로그에 출력하지 않는다.

## 실패 처리와 멱등성

- Project, `Status` 필드, `Todo`/`Done` 옵션이 없으면 쓰기 전에 실패한다.
- GraphQL 오류는 토큰을 포함하지 않는 메시지로 실패한다.
- 중간 실패 뒤 재실행해도 이미 추가된 항목과 이미 맞는 상태는 다시 쓰지 않는다.
- API 페이지네이션을 처리해 항목/이슈가 100개를 넘어도 누락하지 않는다.

## 검증 수준

- 단위 테스트: 상태 전이, 누락 항목 추가, README 결정성/탈출, no-op을 검증한다.
- 로컬 dry-run: 현재 Project를 실제로 읽되 쓰기는 하지 않고 예상 README를 확인한다.
- Live-service verification: GitHub App 설치와 비밀값 설정 뒤 수동 workflow를 실행하고,
  Project README 목적지에서 결과를 다시 읽는다.
