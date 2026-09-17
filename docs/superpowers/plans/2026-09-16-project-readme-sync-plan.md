# GitHub Project README 자동 동기화 — 구현 계획

날짜: 2026-09-16

1. `scripts/sync_project_readme.py`에 표준 라이브러리 GraphQL 클라이언트와 페이지네이션을 구현한다.
2. 열린 이슈 자동 추가와 상태 정합성 규칙을 멱등 함수로 구현한다.
3. 보드 상태에서 결정적인 Project README를 생성하고 변경 시에만 갱신한다.
4. `tests/test_sync_project_readme.py`로 상태 전이, 추가, 탈출, no-op을 검증한다.
5. 최소 권한 GitHub App token을 쓰는 `.github/workflows/project-readme-sync.yml`을 추가한다.
6. `actionlint`, focused pytest, `--dry-run`으로 로컬 검증한다.
7. GitHub App을 조직에 설치하고 변수/비밀값을 설정한 뒤, 병합 후 수동 실행으로 실제 목적지를 검증한다.
