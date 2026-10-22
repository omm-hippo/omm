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
