# exo Mac 클러스터 + 사내 게이트웨이 설계 (#503)

중소기업 로컬 에이전트 로드맵 2/3. 1/3은 #502(하네스 연결), 3/3은 #504(RAG MCP)다.
결정은 2026-10-21~22 브레인스토밍에서 나왔다. "(사용자 결정)"은 사용자가 정했고,
"(설계자 판단)"은 설계자가 정했다. 설계자 판단은 사용자가 계획 리뷰 때 뒤집을 수 있다.

## 의도

비개발자가 omm 명령만으로 사무실 Mac 여러 대를 하나의 클러스터로 묶는다.
목적은 한 대에 들어가지 않는 큰 모델을 메모리를 합쳐 돌리는 것이다. 부하 분산은 목적이 아니다.
직원은 자기 자리 PC에서 사내망을 통해 이 클러스터에 접속하고, Claude Code·Codex·pi로 일한다.
사내 민감 자료를 다루는 환경이므로, 클러스터가 사내망에 **인증 없이 열리는 일이 없어야** 한다.

## 결정 사항

- 백엔드는 **exo**다(사용자 결정). llama.cpp RPC와 비교해 실측 없이 확정했다.
- 1순위 환경은 **Thunderbolt 5 케이블로 서로 모두 연결한 Mac**이다. 시험 장비는 M4 Pro Mac mini 4대다
  (한 대당 TB5 포트 3개 = 4대 full mesh).
- 명령은 새 상위 그룹 **`omm cluster`**다(사용자 결정).
  하위 명령은 `init`(대표 Mac), `join`(나머지 Mac), `status`, `model`, `token add|list|revoke`, `leave`다.
  `leave`는 설계자 판단이다. 설치한 방화벽·서비스를 되돌리는 길이 필요해서 넣었다.
- **클러스터의 모든 Mac에 omm을 설치한다**(사용자 결정). 대표 Mac은 `init`, 나머지는 `join`을 실행한다.
- exo API는 `0.0.0.0:52415`에 인증 없이 열린다(exo `src/exo/api/listening_socket.py`).
  이를 **macOS pf 방화벽 규칙**으로 막는다(사용자 결정). Mac마다 관리자 암호를 한 번 입력한다.
- exo 설치는 `omm cluster init/join` 안에서 `brew install --cask exo`로 한다(설계자 판단).
  `omm engine install`은 `linker.ENGINES`, 즉 GGUF를 링크받는 러너 목록에 묶여 있다. 그래서
  exo를 거기 넣으면 링크 흐름 전체에 섞인다.
- exo는 **자기 모델을 직접 받고 관리하는 러너**로 다룬다. MLX 파일은 omm 허브(`registry.py`,
  `linker.py`)에 넣지 않는다(설계자 판단). 이 설계는 #29(omm이 임의 MLX 모델을 직접 관리)를 해결하지 않는다.
- 대표 Mac의 **omm 게이트웨이**만 사내망에 열린다. 게이트웨이는 직원 토큰을 확인한 뒤
  추론 경로만 `127.0.0.1:52415`로 넘긴다(설계자 판단). #504 RAG도 같은 게이트웨이와 토큰을 쓴다.
- 클러스터 격리를 위해 exo namespace(`EXO_ZENOH_NAMESPACE`)를 `init`이 만들고,
  `join <namespace>`로 나머지 Mac에 전달한다(설계자 판단). 같은 사내망의 다른 exo가 섞이지 않게 하려는 것이다.

## 목표

1. `init` 한 번과 `join` 세 번으로 4대 클러스터가 생기고, `status`가 노드·메모리·RDMA·TB 연결을 보여 준다.
2. `omm cluster model`이 클러스터 전체 메모리에 들어가는 모델을 추천하고, 고른 모델을 올린다.
3. 직원 토큰이 있는 요청만 게이트웨이를 통과한다. 폐기한 토큰은 즉시 거절된다.
4. 사내망의 다른 PC에서는 게이트웨이 포트 외의 exo 포트에 닿지 않는다.

## 비목표

- llama.cpp RPC, Windows/Linux 클러스터 노드. 직원 PC는 어느 OS든 게이트웨이에 접속할 수 있다.
- 요청 부하 분산, 여러 모델 동시 서빙 스케줄링.
- SSO·AD·Google Workspace 연동, 인터넷 공개.
- 하네스 설정 쓰기. 그건 #502가 한다. 이 설계는 #502가 쓸 주소와 토큰을 만든다.
- 게이트웨이 TLS. v1은 사내망 평문 HTTP다(설계자 판단, 아래 "남은 위험" 참고).

## 반목표 (기술적으로 동작해도 실패인 것)

- exo API, Mac 간 통신 포트, 게이트웨이 관리 경로가 토큰 없이 사내망에서 닿는다.
- 토큰 파일이 깨졌을 때 게이트웨이가 모든 요청을 통과시킨다. 반드시 **모두 거절**해야 한다(fail closed).
- 프롬프트·응답 본문이 run log, telemetry, usage, crash report에 남는다.
- RDMA가 꺼져 있거나 Mac 한 대가 빠졌는데 `status`가 정상처럼 보인다.

## 제약

- exo macOS 앱은 macOS 26.2 이상이 필요하다. RDMA를 쓰려면 모든 Mac의 OS 버전이 정확히 같아야 한다
  (exo README "Important Caveats").
- RDMA 켜기(복구 모드에서 `rdma_ctl enable`)는 omm이 대신할 수 없다. 단계 안내와 `rdma_ctl status` 점검만 한다.
- 파이프라인 모드가 텐서 모드보다 안정적이었다는 보고가 있다. 그래서 배치를 고를 때 파이프라인을 우선한다(설계자 판단).
- CLI를 바꾸면 `docs/commands.json`, README `## Usage`, `scripts/check_docs_sync.py`를 함께 맞춘다(CLAUDE.md).
- 숨은 백그라운드 명령(`_cluster-gateway-run`)은 `cli.py`의 skip-set 5곳에 모두 등록한다.
- `questionary`, `requests` 등은 함수 안에서 늦게 import한다(시작 속도).

## 실물로 먼저 확인할 것 (계획 Task 0)

설계는 아래 질문의 답에 기대고 있다. 0단계 실측에서 답이 예상과 다르면 구현을 멈추고 설계로 돌아온다.

1. exo가 여는 포트 전체 목록(API, zenoh, MLX ring/JACCL), 그리고 각 포트를 묶는 주소.
2. 사내망(이더넷/Wi-Fi) 쪽 exo 포트를 막아도 TB 연결만으로 탐색과 추론이 되는지.
3. pf 규칙을 `/etc/pf.conf` 수정 없이 `com.apple/*` 하위 anchor로 올릴 수 있는지, 그리고 재부팅 후에도 유지되는지.
4. exo 앱에 namespace를 omm이 설정하는 방법.
5. `rdma_ctl status`의 실제 출력 문구, 그리고 `/state`·`/models`·`/instance/previews`의 실제 JSON 모양.
6. Claude Code가 `127.0.0.1:52415/v1/messages`로 도구 호출 작업을 끝까지 하는지.

## 남은 위험

- **평문 토큰.** 사내망을 엿볼 수 있는 사람은 토큰을 가로챌 수 있다. TLS를 넣으려면 하네스마다 사설 인증서를
  신뢰하게 설정해야 해서(#502 범위) 후속으로 미룬다.
- exo는 젊은 프로젝트라 API가 바뀔 수 있다. exo 응답 해석은 `cluster/exo_client.py` 한 파일에 모으고,
  0단계에서 캡처한 실제 JSON을 테스트 fixture로 고정한다.

## 구현자에게 맡기는 것

내부 함수 분해, 화면 문구의 세부 표현(아래 계획이 고정한 문구 제외), 진행 표시 방식.
