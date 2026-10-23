# exo 클러스터 실측 결과 (#503 계획 Task 0)

- 측정일: 2026-10-22
- 장비: Mac mini 1대(`node3-macmini`, M4 Pro, 메모리 64 GiB, macOS 26.5 25F71)
- exo 버전: Homebrew cask 1.0.71
- 접속: Tailscale SSH

계획은 4대 기준이지만 이번에는 사용자 지시에 따라 **1대로만** 측정했다.
여러 대가 있어야 확인되는 항목은 "미검증"으로 적었다.

## 1. exo가 여는 포트

1.0.71 백엔드를 `--libp2p-port 52416`으로 실행하고 `lsof`로 확인했다.

| 포트 | 종류 | 묶인 주소 | 성격 |
|---|---|---|---|
| 52415 | TCP | `*` (모든 인터페이스) | HTTP API. 인증이 없다. Tailscale 주소로 이 PC에서 접근하면 200이 돌아왔다. |
| 52416 | TCP | `*` | libp2p. 기본값은 OS가 정하는 무작위 포트이고, `--libp2p-port`로 고정할 수 있다. |
| 5353 | UDP | `*` | mDNS 탐색. macOS Bonjour와 같이 쓰는 포트라 막으면 안 된다. |
| 무작위 | UDP | 인터페이스별 IP | mDNS 질의용 클라이언트 소켓이다. |
| 49153~65535 중 무작위 | TCP | `0.0.0.0` | **MLX ring.** 모델을 여러 대에 나눌 때만 열린다(`utils/ports.py:random_ephemeral_port`, `placement_utils.py`의 `Host(ip="0.0.0.0", port=ephemeral_port)`). 1대 배치에서는 listen하지 않았다. 여러 대 동작은 소스로만 확인했고 실측은 하지 않았다. |

## 2. 사내망 쪽 포트를 막아도 클러스터가 동작하는가

- 1대에서는 확인했다. pf로 52415/52416을 lo0와 Thunderbolt(`en2`/`en3`/`en4`)에서만 받게 하자, 로컬 API는 200이 나왔고 이 PC(Tailscale 경유)에서는 둘 다 막혔다. SSH(22)는 영향이 없었다.
- **여러 대 탐색과 추론은 미검증이다.**
- **STOP 조건 해당.** MLX ring 포트가 49153~65535 범위의 무작위 값이고 `0.0.0.0`에 묶인다. 포트를 지정하는 규칙으로는 이 포트만 골라 막을 수 없다.
  - 이 범위 전체를 사내망 인터페이스에서 막으면 다른 서비스가 같이 막힌다. node3에서 확인한 예는 `rapportd`(Continuity/AirPlay, `*:60963`)다.
  - 그래서 사용자 결정이 필요하다.

## 3. pf 하위 anchor

- `/etc/pf.conf` 수정 없이 `pfctl -a com.apple/omm-cluster -f <규칙>`으로 올라간다. `/etc/pf.conf`에 `anchor "com.apple/*"`가 있기 때문이다.
- node3에서는 pf가 처음에 **꺼져 있었다**(`Status: Disabled`). 규칙이 효과를 내려면 `pfctl -E`가 필요하다. `-E`는 참조 토큰을 돌려준다. 시험이 끝난 뒤 `pfctl -X <token>`으로 원래 상태(Disabled)로 되돌렸다.
- 재부팅 뒤에도 유지되는지는 **미검증**이다. 원격 장비라 재부팅하지 않았다. 계획대로 LaunchDaemon이 부팅 때 다시 올린다.

## 4. namespace 설정 방법

- 앱(Swift)은 UserDefaults의 `EXOCustomNamespace` 키를 읽는다(도메인 `exolabs.EXO`). 이 값을 백엔드 환경 변수로 넘긴다.
- **1.0.71 백엔드가 실제로 읽는 변수는 `EXO_LIBP2P_NAMESPACE`다.** native binding 문자열에서 확인했다. main 소스와 README는 `EXO_ZENOH_NAMESPACE`라고 적고 있다.
- 결론: omm은 두 변수를 **모두** 설정한다.

## 5. 응답 형태 (fixture: `tests/fixtures/exo/`)

### `/state`

- 최상위 키: `instances`, `runners`, `downloads`, `tasks`, `lastSeen`, `topology`, `nodeIdentities`, `nodeMemory`, `nodeDisk`, `nodeSystem`, `nodeNetwork`, `nodeThunderbolt`, `nodeThunderboltBridge`, `nodeRdmaCtl`, `thunderboltBridgeCycles`
- 노드 키: libp2p peer ID다. **exo를 재시작할 때마다 바뀐다** (`12D3KooWCf8…` → `12D3KooWCBeL…`).
- 메모리: `nodeMemory[id].ramTotal.inBytes`, `nodeMemory[id].ramAvailable.inBytes`
- 신원: `nodeIdentities[id] = {modelId, chipId, friendlyName, osVersion, osBuildVersion}`
- RDMA: `nodeRdmaCtl[id].enabled` (bool)
- Thunderbolt: `nodeThunderbolt[id].interfaces[] = {rdmaInterface, domainUuid, linkSpeed}`
- 연결: `topology.connections[source][sink] = [edge…]`. 이 PC에서는 빈 dict였다(1대).
  - 여러 대일 때의 edge 모양은 소스(`shared/types/topology.py`)로만 확인했다. `{sourceRdmaIface, sinkRdmaIface}` 또는 `{sinkMultiaddr}`다.
  - `state.json` fixture는 이 모양으로 만든 **합성 4노드**다. 실측 1노드는 `state-1node.json`에 있다.
- 실행 중인 모델: `instances[id] = {"MlxRingInstance" | "MlxJacclInstance": {instanceId, shardAssignments: {modelId, runnerToShard, nodeToRunner}, hostsByNode, ephemeralPort}}`
  - 준비 상태는 `runners[runnerId] = {"RunnerReady": {}}`다.
- **주의:** 실행 PATH에 `/usr/sbin`과 `/sbin`이 없으면 `networksetup`, `system_profiler`, `scutil`을 찾지 못한다. 그러면 `nodeIdentities`, `nodeSystem`, `nodeNetwork`, `nodeThunderbolt`가 빈 값으로 남는다.

### `/models`, `/v1/models`

- 모양: `{"object":"list","data":[{id, storage_size_megabytes, supports_tensor, context_length, capabilities, …}]}`
- 크기는 MB 단위(`storage_size_megabytes`)다.

### `/instance/previews?model_id=`

- 모양: `{"previews":[{model_id, sharding: "Pipeline"|"Tensor", instance_meta: "MlxRing"|"MlxJaccl", instance, memory_delta_by_node, error}]}`

### `/instance` 생성

- `POST {"instance": <preview.instance>}`를 보내면 `{message, command_id, model_card}`가 돌아온다.

### `/instance/await`

- **1.0.71에는 없다(404).** 준비 여부는 `/state`의 runner 상태를 주기적으로 읽어 판단해야 한다.

### `rdma_ctl status`

- 켜져 있을 때 출력은 `enabled\n`이다(실측).
- 꺼져 있을 때의 출력은 측정하지 못했다. 끄려면 복구 모드가 필요하다. fixture의 `disabled\n`은 **합성값**이다.

## 6. 하네스 경로

이 PC에 경로 기록용 중계 서버를 띄워 측정했다.

- Claude Code는 `POST /v1/messages?beta=true`를 부른다. query string이 붙어 있다.
- 각 경로를 node3에서 직접 호출한 결과:
  - `/v1/messages` (일반·스트리밍): 200
  - `/v1/chat/completions`: 200
  - `/v1/responses`: 200
  - `/v1/models`: 200
  - `/models`: 200
  - `/ollama/api/tags`: 200
  - `/v1/messages/count_tokens`: 405
- **호환성 문제:** Claude Code는 `messages` 안에 `role: "system"` 메시지를 넣어 보낸다. exo는 Claude API 역할을 `user`/`assistant`만 허용한다(`api/types/claude_api.py:ClaudeRole`). 그래서 exo가 **422**를 돌려주고, 그대로는 동작하지 않는다.
  - 중계 서버에서 그 메시지를 최상위 `system` 필드로 옮기자 200이 나왔다.
  - 다만 1B 모델은 Claude Code의 긴 시스템 지시를 따라가지 못해 의미 없는 출력을 냈다. 도구 호출 작업 완주는 **미검증**이며, 더 큰 모델이 필요하다.
- pi는 측정하지 않았다(미검증). Codex는 이 PC에 설치돼 있지 않아 미검증이다.

## 7. 설치 경로에서 만난 문제 (omm 설계에 반영할 것)

1. **앱 번들 안의 백엔드를 SSH나 LaunchAgent로 직접 실행하면 멈춘다.** `/Applications/EXO.app/Contents/Resources/exo/exo`는 `launched-suspended` 상태로 dyld에서 멈췄다. 격리 속성을 지운 뒤에도 같았다. 앱이 처음 실행될 때 화면에 승인 창이 뜨는데, 원격으로는 누를 수 없기 때문으로 추정한다.
   - 런타임 폴더를 번들 밖으로 복사(`cp -R`)하면 정상 실행된다. 이번 측정은 그 방식으로 진행했다.
2. **`sudo EXO --install`(앱의 네트워크 설정 비대화형 설치)도 같은 이유로 멈췄다.** 하위 프로세스를 하나도 띄우지 않았다.
3. Homebrew cask는 `com.apple.quarantine`을 붙인다. 앱 자체는 Gatekeeper를 통과한다(`accepted`, Notarized Developer ID, Exo Technologies Ltd, TeamID AF25623428).
4. node3에서는 Thunderbolt Bridge가 켜져 있었다(`nodeThunderboltBridge.enabled = true`). 앱은 RDMA 클러스터용으로 이를 끄는 LaunchDaemon을 설치한다. omm이 앱을 거치지 않고 백엔드를 띄운다면 이 설정도 omm이 맡아야 한다. 여러 대에서 미검증이다.

## node3에 남긴 것

- 유지한 것: `/Applications/EXO.app`(1.0.71, 격리 속성 제거됨), `~/omm-spike/`(복사한 런타임, 로그, 원본 응답), `~/Library/LaunchAgents/omm.spike.exo.plist`(실행 중), `~/.exo/`, 다운로드한 `mlx-community/Llama-3.2-1B-Instruct-4bit`
- 되돌린 것: pf 규칙(anchor 비움, `pfctl -X`로 Disabled 복귀), 멈춰 있던 `EXO --install` 프로세스와 GUI 앱

## 실기기 수용 검증 (계획 Task 9, 1대)

- 측정일: 2026-10-22 ~ 2026-10-23
- 장비: `node3-macmini` 1대. Wi-Fi(en1)로 연결, FileVault 켜짐, 애플리케이션 방화벽(ALF) 켜짐
- 설치: 이 브랜치를 `~/omm-verify` venv에 editable로 설치하고 `OMM_HOME=~/omm-verify/home`으로 실행. 사용자의 평소 omm은 건드리지 않았다.
- 클라이언트: 클러스터 밖 맥북. 같은 학교 LAN(10.80.x)과 Tailscale 두 경로로 접속

### 결과

| 계획 단계 | 결과 |
| --- | --- |
| Step 1 `init` / `status` | 통과(1대). `exo is running`, RDMA `enabled`, firewall `active`, gateway `running`, 문제 없음. `join`은 Mac이 더 있어야 해서 미검증 |
| Step 2 `model` | `Llama-3.2-1B-Instruct-4bit`는 16초, `Qwen3.5-9B-4bit`(5.5 GiB, 처음 다운로드 포함)는 2분 19초 걸려 `Serving … at http://10.80.118.65:52440` 출력. 한 대 메모리를 넘는 모델은 미검증 |
| Step 3 토큰 | 토큰 없음 401, 폐기한 토큰 401, 유효한 토큰은 SSE 스트리밍 200("안녕하세요…"). Claude Code(`ANTHROPIC_BASE_URL`=gateway, 모델 Qwen3.5-9B)가 Read 도구로 파일을 읽고 정확한 내용을 답함(2턴, 53초). 1B 모델은 도구를 부르지 못하고 스키마를 그대로 출력했다. 모델 능력 문제이고, gateway나 exo 문제는 아니다 |
| Step 4 포트 | nmap이 없어서 node3에서 LISTEN 중인 포트를 모두 열어 봤다. LAN과 Tailscale 모두 22와 52440만 열렸고, 52415, 52416, 62841(rapportd)은 닫혔다 |
| Step 5 RDMA 성능 비교 | 미검증(여러 대와 Recovery 모드가 필요) |
| Step 6 노드 분리 | 미검증(여러 대가 필요) |
| `leave` 정리 | LaunchAgent와 LaunchDaemon 제거, pf anchor 비움, 잠자기 값 복원(1), omm이 추가한 ALF 항목 제거, 토큰 보존을 확인 |

### 검증하다 찾아서 고친 문제

1. **잠자기 때문에 노드가 몇 분마다 끊김.** `pmset -g log`를 보면 원래 설정(`sleep 1`)대로 11:10에 `Idle Sleep`에 들어간 뒤 한 번도 완전히 깨지 않았다. 네트워크 패킷이 올 때마다 약 45초만 `DarkWake`로 깼다가 다시 `Maintenance Sleep`에 들어갔다(321회). 깨어 있는 짧은 동안 `sleep 0`으로 바꿔도 이 반복은 끝나지 않았고, 전원 버튼으로 완전히 깨운 뒤에야 계속 켜져 있었다.
   - 수정: `init`/`join`이 `pmset -a sleep 0`을 적용하고 원래 값을 `OMM_HOME/cluster/sleep-before.txt`에 저장한다. `leave`가 그 값을 복원하고, `status`는 잠자기가 켜져 있으면 문제로 보고한다.
   - 실측: 저장된 값 1, 적용 후 0, `leave` 후 1로 돌아옴.
2. **ALF가 gateway 연결을 붙잡아 둠.** TCP 연결은 되는데 HTTP 응답이 오지 않았다(curl `000`). 127.0.0.1로 접속하면 401이 정상으로 왔다. `socketfilterfw` 로그에는 `Enqueuing new inbound flow for Python.app` / `Not prompting user until existing response received`가 찍혀 있었다. 화면의 허용 창을 아무도 누르지 않아서 연결이 계속 대기한 것이다.
   - 수정: `init`이 gateway를 시작하기 전에 gateway가 실제로 실행되는 Python 바이너리(Homebrew framework의 `Resources/Python.app/Contents/MacOS/Python`)와 exo를 `socketfilterfw --add` / `--unblockapp`로 등록한다. `join`은 exo만 등록한다.
   - ALF는 서명된 바이너리를 코드 서명 기준으로 묶는다. 그래서 omm이 옮겨 둔 exo 복사본은 기존 exo 항목에 합쳐지고, 자기 경로로는 목록에 나타나지 않는다. 그래서 등록 뒤 **추가한 경로 그대로** 목록에 나타난 항목만 기록하고, `leave`도 그 항목만 지운다. `--getappblocked`는 등록되지 않은 `/bin/ls`도 "permitted"라고 답해서 판단에 쓸 수 없다.
   - 실측: 등록 뒤 첫 응답까지 약 37~38초가 걸렸다(이틀 모두). node3에 오래 남아 있던 응답 대기 중인 허용 창 때문인지, ALF 정책 반영이 원래 이만큼 늦는지는 Mac 한 대로는 구분하지 못했다. 새 Mac에서 다시 확인이 필요하다.

### 고치지 않고 남긴 것

- 깨끗한 `OMM_HOME`에서 처음 `omm cluster init`을 실행하면 첫 실행 설정 마법사 질문("Run the first-time setup wizard now?")이 먼저 뜬다. 화면 없이 SSH로 실행하면 여기서 멈춘다.
- FileVault가 켜진 Mac은 정전 뒤 자동 재부팅(`autorestart 1`)이 되어도 잠금 해제 화면에서 멈춘다. 그래서 SSH, exo, gateway가 모두 올라오지 않는다. omm이 바꿀 설정은 아니고 운영 안내가 필요하다.
- 디스크 잠자기가 0인 Mac에서 `leave`가 잠자기 값을 1로 복원하면 pmset이 경고를 출력한다(`Disk sleep should be non-zero whenever system sleep is non-zero`). 동작에는 영향이 없다.

### node3 현재 상태 (2026-10-23)

- 클러스터 구성은 `leave`로 정리했다. 사용자 요청에 따라 잠자기는 끈 채로 두었다(`sleep 0`, `autorestart 1`, `disksleep 0`, `displaysleep 0`).
- 남긴 것: `~/omm-verify/`(검증용 venv, `OMM_HOME`, 토큰 파일), exo가 받은 모델(`Llama-3.2-1B-Instruct-4bit`, `Qwen3.5-9B-4bit`), spike 때 생긴 ALF의 exo 허용 항목, `/Applications/EXO.app`

## 2대 클러스터 실측 (node4 head + node3 member, Thunderbolt 케이블 1개, 2026-10-23)

`init`(node4)과 `join`(node3)로 두 Mac이 한 클러스터가 됐고, head의 `status`에 두 노드와 RDMA 링크(`rdma_en3` ↔ `rdma_en3`)가 보였다. Step 1은 통과했다. Step 2~6은 모델 다운로드(83GB)와 3대 구성이 필요해 미검증이다.

### 찾아서 고친 문제

1. **서로 발견하는 데 40초 이상 걸리거나 아예 못 찾음.** exo(libp2p)는 Mac의 모든 주소(Wi-Fi, Tailscale, 링크로컬, Thunderbolt)를 광고하고 상대의 모든 주소로 연결을 시도한다. 우리 pf 규칙이 Thunderbolt가 아닌 경로의 SYN을 조용히 버려서(`block drop`), 그 시도들이 타임아웃까지 기다리며 Thunderbolt 시도를 늦췄다. exo 프로세스를 동시에 재시작한 뒤 "노드 2개가 보일 때까지"를 쟀다.
   - pf 규칙 없음: 3/3, 약 10초.
   - 기존 `block drop`: 3번 중 2번 성공(40초 걸린 것 포함), 1번은 40초 안에 실패. 별도 시도에서는 못 찾는 경우가 더 많았다.
   - `block return`(RST로 즉시 거절): 5/5, 약 10초. 코드로 설치한 뒤 다시 5/5(그중 한 번은 30초).
   - 수정: `firewall.render_rules`의 두 `block` 규칙을 `block return`으로 바꿨다. 포트는 여전히 닫혀 있다. 노트북에서 head의 Wi-Fi 주소로 확인하니 52415, 52416, 임의의 높은 포트(60000)는 `closed`, 52440만 `open`이었다.
   - 확인 못 한 것: 정확히 어떤 주소·어떤 시점의 시도가 막혔는지는 패킷으로 직접 보지 못했다. 위 비교 결과가 근거의 전부다. `no state`로 상태 추적만 끄는 변형은 효과가 뚜렷하지 않았다.
2. **exo 바이너리가 `_dyld_start`에서 멈춤.** 새로 설치된 Mac(brew cask가 quarantine을 붙인 `EXO.app`)에서 처음 실행하면, 화면에 뜨는 macOS 허용 창을 누를 때까지 `omm`이 옮겨 둔 복사본도, SSH로 직접 실행한 `exo --help`도 멈춘다(RSS 32KB, CPU 0%). 화면 앞에서 허용을 누르자 곧바로 시작됐다.
   - 수정: `init`/`join`이 "첫 실행에는 이 Mac 화면에 허용 창이 뜰 수 있다"는 안내를 출력한다. 원격에서 대신 해결하는 방법은 찾지 못했다(`xattr -d com.apple.quarantine`은 효과가 없었다).

### 고치지 않고 남긴 것

- `status`가 "Thunderbolt Bridge is on... turn it off for an RDMA cluster"라고 경고한다. 그런데 이 Mac들은 Thunderbolt Bridge에 `10.0.0.3`/`10.0.0.4`를 수동으로 설정해 두었고, 서비스를 끄면(`networksetup -setnetworkserviceenabled "Thunderbolt Bridge" off`) 그 주소가 사라져 두 Mac이 서로를 찾지 못했다. 다시 켰다. 경고와 실제 동작이 어긋난다.
- 3대를 삼각형으로 연결하면 Thunderbolt Bridge가 루프를 이뤄 패킷 폭주가 날 수 있다는 가설(node3 과열)은 아직 검증하지 못했다.
- 첫 `init`이 `exit_code 1`로 20초 만에 끝난 적이 있다. 에러 본문은 로그에 남지 않아 원인을 모른다. 같은 명령을 다시 실행하자 성공했다.
- 방금 재시작한 exo는 `status`에서 메모리가 `0/0 GiB free`로 잠깐 보인다.
