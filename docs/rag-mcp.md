# 코딩 에이전트에서 OMM 문서 검색 쓰기 (MCP)

OMM에 등록한 문서를 Claude Code, Codex, pi, Cursor 같은 코딩 에이전트가 직접
검색할 수 있게 해 줍니다. 에이전트는 질문에 답하기 전에 OMM 문서에서 근거를 찾고,
문서 이름·쪽·원문 인용을 함께 받습니다. 이슈 #504의 1단계로, **내 PC에서 나 혼자
쓰는 경우**만 다룹니다. 직원별 권한과 사내 게이트웨이는 다음 단계(#503 이후)입니다.

## MCP가 뭔가요?

MCP(Model Context Protocol)는 코딩 에이전트에 "도구"를 붙이는 공통 규격입니다.
에이전트가 OMM을 하나의 작은 프로그램으로 실행해 두고, 필요할 때 "이 질문으로 문서를
찾아 줘"라고 요청합니다. 둘은 인터넷이 아니라 그 프로그램의 표준 입출력(stdin/stdout)
으로만 대화합니다. 그래서 네트워크 포트가 열리지 않습니다.

OMM이 주는 도구는 두 개입니다.

- `search_documents(query, collection?, limit?)` — 문서를 찾아 문서 이름, 쪽, 원문 인용,
  검색 방식을 돌려줍니다. 근거가 없으면 빈 결과가 아니라 **"근거 없음"** 이라고 분명히
  알려 줍니다. 에이전트는 이때 추측하지 말고 근거가 없다고 말해야 합니다.
- `list_collections()` — 문서 묶음(컬렉션) 목록입니다. 아직 컬렉션 기능이 없는
  버전에서는 등록된 문서 목록을 보여 줍니다.

## 연결하기

먼저 문서를 등록합니다. `omm rag collection create 인사규정` 으로 묶음을 만들고
`omm rag add 인사규정 <파일이나 폴더>` 로 넣습니다. `omm web`의 문서 탭에서도 등록할 수 있습니다.
그다음 쓰는 에이전트를 골라 연결합니다.

```sh
omm rag connect claude-code   # Claude Code
omm rag connect codex         # Codex
omm rag connect pi            # pi
omm rag connect cursor        # Cursor
```

연결한 뒤에는 에이전트를 **새로 시작**해야 도구가 보입니다. Claude Code가 켜져 있으면
완전히 끄고 다시 켜 주세요(켜져 있는 Claude Code가 설정 파일을 덮어쓸 수 있습니다).

연결을 끊을 때는 다음을 실행합니다.

```sh
omm rag disconnect claude-code
```

### 무엇이 바뀌나요?

OMM은 각 에이전트의 사용자 설정 파일에 `omm-documents`라는 항목 **하나만** 넣습니다.

| 에이전트 | 설정 파일 | 넣는 위치 |
| --- | --- | --- |
| Claude Code | `~/.claude.json` (`CLAUDE_CONFIG_DIR`가 있으면 그 안의 `.claude.json`) | `mcpServers.omm-documents` (모든 프로젝트에서 쓰는 user 범위) |
| Codex | `~/.codex/config.toml` (`CODEX_HOME`) | `[mcp_servers.omm-documents]` |
| pi | `~/.pi/agent/mcp.json` (`PI_CODING_AGENT_DIR`) | `mcpServers.omm-documents` |
| Cursor | `~/.cursor/mcp.json` | `mcpServers.omm-documents` |

- 바꾸기 전에 원래 파일 내용을 `OMM_HOME/rag-connections/`에 따로 보관합니다.
- `disconnect`는 그 뒤로 아무도 파일을 건드리지 않았으면 원래 파일을 그대로 되돌립니다.
  그사이 다른 설정이 바뀌었으면 `omm-documents` 항목만 지우고 나머지는 그대로 둡니다.
- 여러 번 `connect`해도 항목이 늘어나지 않습니다. omm을 다시 설치해 실행 경로가 바뀌었으면
  `connect`를 한 번 더 실행하면 항목이 새 경로로 바뀝니다.
- 같은 이름의 항목을 사용자가 직접 만들어 두었다면 건드리지 않고 멈춥니다.
- OMM이 넣은 항목을 사용자가 고쳤다면 `disconnect`는 지우지 않고 알려 줍니다.
- 에이전트를 찾지 못하면(명령도 설정 폴더도 없으면) 설치부터 하라고 알려 줍니다.

등록되는 실행 명령은 지금 쓰는 omm 설치 방식에 맞춰 정해집니다. pipx·pip 설치는
그 설치의 Python으로 `python -m omm.rag_mcp`를, npm·포터블 설치는 그 실행 파일로
`omm rag mcp`를 실행합니다. 둘 다 절대 경로라서 PATH 설정에 기대지 않습니다.
`OMM_HOME`을 바꿔 쓰고 있으면 그 값도 함께 등록되어 같은 문서 저장소를 봅니다.

### pi는요?

pi는 0.99(2026-09) 버전부터 MCP를 기본으로 지원합니다. 그보다 오래된 pi라면 pi를
업데이트하는 것이 가장 간단합니다. 업데이트할 수 없으면 pi가 셸 명령을 도구로 쓰게
할 수 있습니다. 문서 검색 명령 `omm rag search "<질문>" --json` 의
출력을 읽도록 프롬프트나 확장에서 안내하면 됩니다.

## 데이터는 어디에 머무나요?

- 문서 원문, 색인, 검색은 모두 이 PC의 `OMM_HOME/documents` 안에서 끝납니다.
  OMM 검색 서버는 포트를 열지 않고, 의미 검색에 필요한 임베딩만 이 PC의 Ollama
  (루프백 주소)에 요청합니다. 인터넷으로 나가는 요청은 없습니다.
- 질문과 인용은 OMM의 실행 기록(run log), 사용 통계, 오류 보고, 벤치마크 업로드
  어디에도 남지 않습니다. 진단 메시지는 stderr에 고정된 문장만 씁니다.
- **주의:** 검색 결과(원문 인용)는 에이전트에게 전달되고, 에이전트는 그것을 자기가
  쓰는 모델에 보냅니다. 에이전트가 클라우드 모델을 쓰면 인용도 그 회사 서버로 갑니다.
  민감한 문서라면 에이전트를 로컬 모델로 연결하세요(`omm setting agent local`,
  [로컬 에이전트 연결](agent-connections.md)).
- 연결 백업(`OMM_HOME/rag-connections/`)에는 에이전트 설정 파일 원본이 들어 있어서
  그 에이전트의 다른 설정이 포함될 수 있습니다. 이슈에 올리지 마세요.

## 확인된 것과 아직 아닌 것

- Claude Code 2.1.283: 격리된 설정 폴더(`CLAUDE_CONFIG_DIR`)에서 `omm rag connect
  claude-code` 후 `claude mcp list`가 `omm-documents ✔ Connected`를 보였고,
  `disconnect` 후 원래 파일로 돌아왔습니다. 실제 모델 대화로 도구를 호출해 보지는
  않았습니다.
- Codex, pi, Cursor: 공식 문서의 설정 형식대로 쓰고 자동 테스트로 확인했지만, 실제
  프로그램으로는 아직 확인하지 않았습니다(미검증).
- 지금 검색은 단어 일치 검색(`local_lexical_v1`)이고 결과마다 점수 대신 순위를 줍니다.
  의미 검색과 컬렉션이 들어오면 같은 도구가 그 기능을 그대로 씁니다.

## 문제 해결

- 도구가 안 보이면: 에이전트를 새로 시작했는지 확인하고, Claude Code는
  `claude mcp get omm-documents`, Codex는 `codex mcp list`, pi는 `pi mcp list`로
  상태를 봅니다.
- "근거 없음"만 나오거나 검색 방식이 "단어만"이면: `omm rag status` 로 의미 검색 상태를
  확인하세요. Ollama가 켜져 있고 임베딩 모델(기본 `bge-m3`, `ollama pull bge-m3`)이 있어야
  뜻이 비슷한 문장도 찾습니다.
- "재색인 필요"가 보이면: `omm rag reindex` 를 실행합니다. "원본 다시 등록 필요"면
  원본을 같은 이름으로 다시 등록합니다.
