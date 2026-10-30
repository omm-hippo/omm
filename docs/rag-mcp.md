# 코딩 에이전트에서 OMM 문서 검색 쓰기 (MCP)

OMM에 등록한 문서를 Claude Code, Codex, pi, Cursor 같은 코딩 에이전트가 직접
검색할 수 있게 해 줍니다. 에이전트는 질문에 답하기 전에 OMM 문서에서 근거를 찾고,
문서 이름·쪽·원문 인용을 함께 받습니다. 처음에는 **내 PC에서 나 혼자 쓰는 경우**를
설명하고, 회사 클러스터 헤드에 문서를 모아 직원·팀별로 나눠 쓰는 방법은 아래
[사내 공유](#사내-공유)에 있습니다(이슈 #504 2단계).

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

## 사내 공유

회사에 `omm cluster`(#503)로 묶은 Mac 클러스터가 있으면, 문서를 **헤드 Mac 한 곳**에
모아 두고 직원마다 볼 수 있는 컬렉션을 나눌 수 있습니다. 직원 PC에는 문서 원본도
색인도 남지 않습니다. 신원 확인은 클러스터 직원 토큰(`omm cluster token add <이름>`)을
그대로 씁니다. 따로 로그인하지 않습니다.

### 권한은 이렇게 정해져요

- 컬렉션을 **만든 사람**이 주인입니다. 헤드 Mac에서 직접 만든 컬렉션과, 이 기능이
  생기기 전에 있던 컬렉션은 **관리자**(헤드 Mac에서 omm을 쓰는 사람) 것입니다.
- 처음에는 만든 사람과 관리자만 검색할 수 있습니다.
- 주인이나 관리자가 특정 직원(토큰 이름)이나 팀에 공유하면 그 사람들도 검색할 수
  있습니다. 공유를 거두면 바로 다시 못 봅니다.
- 문서를 넣고 빼거나, 공유를 바꾸거나, 컬렉션을 지우는 것은 주인과 관리자만 합니다.
  공유받은 사람은 검색만 합니다.
- 팀은 관리자만 만들고 사람을 넣고 뺍니다.
- 관리자가 직원 토큰을 취소(`omm cluster token revoke <이름>`)하면 그 직원은 아무
  컬렉션도 볼 수 없습니다. 같은 이름으로 토큰을 다시 만들면 예전 공유가 그대로
  살아나니, 다른 사람에게 줄 토큰에는 다른 이름을 쓰세요.
- 볼 수 없는 컬렉션은 목록에도, 검색 결과에도, 오류 문장에도 나오지 않습니다.
  "없는 컬렉션"과 같은 답을 받습니다. 다만 컬렉션 이름은 헤드 전체에서 하나뿐이라, 남의
  컬렉션과 같은 이름으로 새로 만들려 하면 "이미 있는 이름"이라고만 알려 줍니다(누구 것인지와
  내용은 알려 주지 않습니다).

### 헤드 Mac에서 (관리자)

```sh
omm cluster token add kim                 # 직원 토큰 만들기 (한 번만 보여 줘요)
omm rag team create sales                 # 팀 만들기
omm rag team add sales kim                # 팀에 사람 넣기
omm rag team list                         # 팀과 사람 보기
omm rag collection create 인사규정
omm rag add 인사규정 ~/규정/
omm rag share 인사규정 kim                # kim에게 공유
omm rag share 인사규정 --team sales       # sales 팀에 공유
omm rag unshare 인사규정 --team sales     # 팀 공유 거두기
omm rag collection list                   # 컬렉션마다 만든 사람과 공유 대상이 보여요
omm rag team remove sales kim
omm rag team delete sales                 # 팀과 그 팀에 준 공유를 함께 지워요
```

### 직원 PC에서

먼저 관리자에게 받은 토큰으로 헤드를 정합니다. `--token`을 빼면 토큰을 화면에
보이지 않게 입력받습니다(셸 기록에 남지 않게 하려면 이쪽을 쓰세요).

```sh
omm rag remote set 192.168.0.10           # 포트를 안 쓰면 52440
omm rag remote                            # 지금 쓰는 헤드 (토큰은 보여 주지 않아요)
```

그다음부터 아래 명령은 이 PC가 아니라 **헤드의 문서**를 씁니다.

```sh
omm rag collection create 영업자료        # 내가 주인인 컬렉션
omm rag add 영업자료 ./보고서/            # 파일을 헤드로 보내 헤드에서 색인
omm rag collection list                   # 내가 검색할 수 있는 컬렉션
omm rag search "지난 분기 매출"           # 볼 수 있는 컬렉션만 검색
omm rag share 영업자료 lee                # 내 컬렉션을 동료에게 공유
omm rag unshare 영업자료 lee
omm rag remove 영업자료 보고서.pdf
omm rag remote clear                      # 헤드 설정을 지우고 다시 이 PC 문서를 써요
```

`omm rag status`, `reindex`, `embedding-model`, `team` 은 늘 그 PC 자신의 문서 저장소를
다룹니다(헤드 관리용). 의미 검색에 쓰는 임베딩은 헤드 Mac의 Ollama에서 돌아갑니다.
헤드에 Ollama나 임베딩 모델이 없으면 단어 검색만 하고 그렇다고 알려 줍니다.

### 웹에서 공유하기

명령어 대신 `omm web`의 **내 문서** 탭에서 똑같이 할 수 있습니다.

1. 맨 위 **📍 지금 보는 곳**에서 [회사에 연결]을 누르고, 관리자에게 받은 head 주소와
   출입증(토큰)을 넣습니다. 토큰이 맞는지 head에 먼저 물어본 뒤에만 저장합니다.
   이제 “회사 head (주소) · 나: kim”처럼 보입니다.
2. 왼쪽 [+ 새 컬렉션]으로 내 컬렉션을 만들고, 오른쪽에서 파일을 올립니다.
3. **공유** 칸에서 [사람 추가](출입증 이름)나 [팀 추가]를 누르면 그 사람·팀도 검색할 수
   있습니다. 칩의 ✕를 누르면 공유를 거둡니다.
4. **질문하기**에 질문을 쓰면 head가 찾은 근거 구간을 원문 그대로 보여 줍니다(head 모드에서는
   답변 문장을 만들지 않습니다).
5. **AI 연결**에서 [연결]을 누르면 그 에이전트가 head의 공유 문서를 찾게 됩니다. 연결한 뒤에는
   에이전트를 다시 시작하세요.
6. [이 PC로 돌아가기]를 누르면 head 주소와 토큰을 이 PC에서 지웁니다.

헤드 Mac(관리자)의 내 문서 탭에는 **팀 관리**와 **직원 출입증 (토큰)** 칸도 있습니다.
출입증은 만들 때 한 번만 보여 주니 바로 복사해서 전해 주세요. 토큰은 브라우저로 가지 않고
이 PC의 `rag-remote.json`에만 있습니다. 자세한 화면 설명은 [로컬 웹](local-web.md#웹에서-공유하기-내-문서-탭)에 있습니다.

### 에이전트에 붙이기

```sh
omm rag connect claude-code --remote
omm rag connect codex --remote
omm rag connect cursor --remote
omm rag connect pi --remote
```

헤드의 MCP 주소(`https://<헤드>:52440/mcp`)를 각 에이전트 설정에 HTTP 방식으로 넣습니다.
에이전트가 검색하면 **그 직원 토큰으로 볼 수 있는 컬렉션만** 찾습니다.

| 에이전트 | 넣는 모양 |
| --- | --- |
| Claude Code | `"type": "http"`, `"url"`, `"headers": {"Authorization": "Bearer …"}` |
| Codex | `url = "…"`, `http_headers = { Authorization = "Bearer …" }` |
| Cursor | `"url"`, `"headers"` |
| pi | `"type": "http"`, `"url"`, `"headers"` |

- **주의:** 토큰이 그 에이전트 설정 파일 안에 들어갑니다(macOS/Linux에서는 그 파일을
  나만 읽을 수 있게 바꿉니다). 설정 파일을 남에게 보내거나 올리지 마세요.
- 백업·복원 방식은 위의 로컬 연결과 같습니다. `omm rag disconnect <에이전트>` 로 원래대로
  돌립니다. `--remote` 없이 다시 `connect` 하면 이 PC의 문서 검색으로 바뀝니다.
- `omm rag remote clear` 는 에이전트 설정에 들어간 토큰까지 지우지는 않습니다.
- 게이트웨이는 HTTPS만 받고, 인증서는 헤드가 직접 만든 것이라 에이전트가 그 인증서(`omm rag remote set`
  이 고정해 둔 `OMM_HOME/rag-remote-ca.pem`)를 믿게 해야 합니다. `connect --remote` 가 에이전트별 방법을
  알려 줍니다: Claude Code는 `~/.claude/settings.json` 의 `env.NODE_EXTRA_CA_CERTS`, Codex는
  `CODEX_CA_CERTIFICATE=<파일> codex`, pi와 Cursor는 `NODE_EXTRA_CA_CERTS=<파일>` 로 시작합니다.
  `disconnect` 도 같이 해 주세요.

### 게이트웨이 주소 (참고)

모두 `Authorization: Bearer <직원 토큰>` 이 필요합니다. 없거나 틀리면 401입니다.

| 주소 | 하는 일 |
| --- | --- |
| `POST /mcp` | MCP Streamable HTTP (JSON 응답만, 세션 없음). `GET`/`DELETE` 는 405 |
| `GET /rag/v1/whoami` | 토큰 이름 확인 |
| `GET`·`POST /rag/v1/collections`, `DELETE …?collection=` | 볼 수 있는 컬렉션 목록, 만들기, 지우기 |
| `GET`·`POST`·`DELETE /rag/v1/documents?collection=` | 문서 목록, 올리기(본문은 파일 그대로, `X-OMM-Filename` 머리글), 지우기(`&name=`) |
| `POST /rag/v1/search` | `{"query", "collection"?, "limit"?}` 검색 |
| `GET`·`POST`·`DELETE /rag/v1/shares` | 공유 보기·추가·거두기 (주인·관리자만) |

권한이 없으면 403, 볼 수 없거나 없으면 404, 파일이 6 MiB를 넘으면 413, 헤드가 바쁘면
429입니다. 컬렉션·문서 이름은 경로가 아니라 쿼리·본문·머리글로만 보내서, 게이트웨이
접속 기록에는 `/rag/v1/search` 같은 경로만 남습니다.

### 아직 확인하지 못한 것

- `omm web` 내 문서 탭으로는 실제 macOS 헤드(omm 0.4.43 게이트웨이)에 Windows PC에서
  붙어, 직원 토큰 두 개로 컬렉션 만들기·올리기·검색·공유·공유 거두기를 확인했습니다.
- Claude Code는 웹의 [연결]로 실제 헤드의 `/mcp`를 넣은 뒤 `claude mcp list`에서
  Connected를 확인했습니다. Codex, Cursor, pi가 HTTP MCP로 실제 헤드에 붙는지는 각 공식
  문서의 설정 모양대로 넣고 자동 테스트로만 확인했습니다(미검증).
- 문서 개수(100개)와 검색 텍스트(200만 자) 제한은 헤드 전체에 걸린 한도라서, 여러 직원이
  함께 씁니다.

## 의미 검색 준비

뜻이 비슷한 문장까지 찾는 의미 검색은 **문서를 저장한 컴퓨터**의 Ollama와 임베딩 모델
(기본 `bge-m3`)이 있어야 합니다. 혼자 쓸 때는 이 PC, 사내 공유에서는 회사 head Mac입니다.
없으면 검색은 멈추지 않고 단어 검색만 합니다. 그래서 지금 상태를 먼저 알려 줍니다.

- `omm rag status` (또는 `--json`의 `semantic_search`)가 한 줄로 알려 줍니다.

  ```text
  의미 검색: 준비됨 (Ollama · bge-m3)
  의미 검색: 꺼짐 (임베딩 모델 없음) - Ollama에 임베딩 모델 bge-m3이(가) 없어요. `ollama pull bge-m3`로 임베딩 모델을 받아 주세요. 지금은 단어 검색만 해요.
  의미 검색: 꺼짐 (Ollama가 꺼져 있음) - Ollama가 설치돼 있지만 켜져 있지 않아요. `open -a Ollama`로 Ollama 앱을 켜 주세요. 지금은 단어 검색만 해요.
  의미 검색: 꺼짐 (Ollama 설치 안 됨) - Ollama를 찾지 못했어요. `brew install ollama`로 설치하고 ... 그다음 `ollama pull bge-m3`. 지금은 단어 검색만 해요.
  ```

  켜는 방법은 운영체제와 설치 방식에 맞춰 바뀝니다. macOS는 Ollama 앱이면 `open -a Ollama`,
  Homebrew로 설치했으면 `brew services start ollama`, Windows는 시작 메뉴의 Ollama 앱,
  Linux는 `sudo systemctl start ollama`(설치 스크립트의 서비스가 있을 때) 또는 `ollama serve`.
  설치가 안 돼 있으면 macOS는 `brew install ollama`(또는 ollama.com 앱), Windows·Linux는
  https://ollama.com/download 의 공식 설치 방법을 알려 줍니다.
- 회사 head에서는 `omm cluster status`에도 `문서 의미 검색: …` 한 줄이 나옵니다.
- 직원 PC에서는 `omm rag remote`와 `omm rag status`가 head의 상태를 보여 줍니다. 꺼져 있으면
  "회사 head의 의미 검색이 꺼져 있어요(…) - 관리자에게 알려 주세요."가 나옵니다. head가
  알려 주는 것은 상태와 모델 이름뿐입니다(경로·명령은 보내지 않아요). head의 omm이 이
  확인보다 오래됐으면 "상태를 알 수 없어요"가 나옵니다.
- `omm web` 내 문서 탭의 "지금 보는 곳" 아래에도 같은 경고가 나옵니다. 이 PC면 고치는
  방법을, 회사 head면 관리자에게 알리라는 말을 보여 줍니다.

OMM은 이 확인 때 아무것도 설치하거나 켜거나 등록하지 않습니다. 이 컴퓨터의 Ollama 주소에
목록 요청 하나를 보내고(2초 제한) 파일 몇 개가 있는지만 봅니다.

### 재부팅 뒤에도 켜져 있게 하려면

손으로 `ollama serve`를 켰다면 재부팅이나 로그아웃 뒤에는 꺼지고, 검색은 조용히 단어 검색으로
떨어집니다. 자동으로 켜 둘지는 직접 고릅니다. OMM은 보이는 경우에만 한 줄로 권합니다.

- **macOS, Ollama 앱**: 앱이 로그인할 때 스스로 켜집니다. 시스템 설정 > 일반 > 로그인 항목에서
  Ollama가 켜져 있는지 보세요.
- **macOS, Homebrew(`brew install ollama`)**: `brew services start ollama`를 한 번 실행하면
  로그인할 때마다 켜집니다. 이미 손으로 켠 `ollama serve`가 있으면 먼저 끄세요(같은 포트를 씁니다).
  `omm rag status`는 `brew services`에 등록돼 있지 않으면 이 명령을 권합니다.
- **Windows**: 설치 프로그램이 시작 프로그램에 Ollama를 넣습니다. 빠져 있으면 설정 > 앱 >
  시작 프로그램에서 켜세요.
- **Linux**: 공식 설치 스크립트가 만든 서비스를 `sudo systemctl enable ollama`로 켜 둡니다.
- 켜 두지 않기로 했다면, 재부팅 뒤 `omm rag status`(head면 `omm cluster status`)로 확인하고
  필요할 때 켜면 됩니다.

## 데이터는 어디에 머무나요?

- 문서 원문, 색인, 검색은 모두 이 PC의 `OMM_HOME/documents` 안에서 끝납니다.
  OMM 검색 서버는 포트를 열지 않고, 의미 검색에 필요한 임베딩만 이 PC의 Ollama
  (루프백 주소)에 요청합니다. 인터넷으로 나가는 요청은 없습니다.
- 질문과 인용은 OMM의 실행 기록(run log), 사용 통계, 오류 보고, 벤치마크 업로드
  어디에도 남지 않습니다. 진단 메시지는 stderr에 고정된 문장만 씁니다.
- **주의:** 검색 결과(원문 인용)는 에이전트에게 전달되고, 에이전트는 그것을 자기가
  쓰는 모델에 보냅니다. 에이전트가 클라우드 모델을 쓰면 인용도 그 회사 서버로 갑니다.
  민감한 문서라면 에이전트를 로컬 모델로 연결하세요(`omm agent local`,
  [로컬 에이전트 연결](agent-connections.md)).
- 연결 백업(`OMM_HOME/rag-connections/`)에는 에이전트 설정 파일 원본이 들어 있어서
  그 에이전트의 다른 설정이 포함될 수 있습니다. 이슈에 올리지 마세요.

## 속도와 메모리

- 의미 검색은 질문을 임베딩 모델(기본 `bge-m3`)로 바꾼 뒤 찾습니다. 그래픽카드 없이
  CPU만 쓰는 노트북에서는 이 모델을 메모리에 올리는 데만 3~4초가 걸리고, 이미 올라와
  있으면 질문 하나에 0.1초 정도면 됩니다.
- 그래서 OMM은 검색이나 문서 등록이 끝난 뒤에도 임베딩 모델을 **5분 동안** 메모리에
  남겨 둡니다. 에이전트가 검색을 여러 번 연달아 해도 처음 한 번만 느립니다. 5분 동안
  쓰지 않으면 Ollama가 알아서 내립니다.
- 대신 그동안 메모리(RAM)를 약 1.2 GB 더 씁니다. 메모리가 빠듯하면 시간을 줄이거나
  바로 내리게 바꿀 수 있습니다.

  ```sh
  omm rag embedding-model set bge-m3 --keep-alive 1m   # 1분만 유지
  omm rag embedding-model set bge-m3 --keep-alive 0    # 매번 바로 내림 (예전 방식, 검색마다 느림)
  ```

  지금 값은 `omm rag embedding-model` 이나 `omm rag status` 에서 보입니다.
- 임베딩 모델이 남아 있어도 `omm web` 에서 대화 모델을 준비하거나 문서 질문에 답하는 것,
  저장한 실행 설정으로 `omm run` 하는 것은 막지 않습니다. 다만 그 사이 메모리 여유는 그만큼
  줄어서, 큰 대화 모델이면 "메모리 여유가 부족해요"가 나올 수 있습니다. 그럴 땐 5분 기다리거나
  위처럼 `--keep-alive 0` 으로 바꾸세요.
- 실행 설정 시험(`omm tune --apply`)과 웹의 성능 비교는 측정값이 흔들리지 않도록 다른 모델이
  하나도 올라와 있지 않아야 시작합니다. 검색 직후라면 임베딩 모델 때문에 "이미 로딩된 모델이
  있다"며 멈출 수 있으니 5분 뒤 다시 하세요.
- 문서 등록(`omm rag add`)과 재색인(`omm rag reindex`)은 조각(약 900자)마다 임베딩을 만들어서
  CPU에서는 조각 하나에 1초 남짓 걸립니다. 52 KB짜리 README는 70여 조각이라 1분 넘게 걸립니다.
  터미널에서 실행하면 `README.md: 의미 색인 만드는 중 32/73 조각` 처럼 어디까지 했는지 한 줄로
  보여 주고, 끝나면 그 줄은 지워집니다. `--json` 이나 `--quiet` 를 붙이면 진행 표시는 나오지 않습니다.

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
  뜻이 비슷한 문장도 찾습니다. 고치는 명령은 그 줄에 나옵니다([의미 검색 준비](#의미-검색-준비)).
- "재색인 필요"가 보이면: `omm rag reindex` 를 실행합니다. "원본 다시 등록 필요"면
  원본을 같은 이름으로 다시 등록합니다.
