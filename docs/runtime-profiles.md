# 추천 설정 적용·검증·저장·복원

```text
omm tune MODEL
omm tune MODEL --apply --engine ollama
omm tune MODEL --apply --save --engine ollama --yes --json
omm setting runtime-profile MODEL --engine ollama --json
omm setting runtime-profile MODEL --engine ollama --restore
```

기본 `tune`은 제안만 보여준다. 설치된 GGUF의 문맥 한도가 확인되면 그 한도를
넘지 않도록 제안을 줄인다. `--apply`는 이전 설정(없으면 작은 기본값)과 제안
설정으로 각각 짧은 생성을 수행하고 임시 로드를 해제한다. `--save`는 이 과정이
성공하고 정리까지 확인된 경우에만 저장한다. 대화형 `--apply`는 결과를 본 뒤
저장 여부를 선택할 수 있다. 스크립트는 `--yes`로 로드에 동의해야 한다.

저장은 `OMM_HOME/runtime-profiles.json`에 모델 파일의 SHA-256과 엔진별로
구분한다. 프롬프트와 생성된 문장은 저장하지 않는다. 파일이 바뀌었거나 다른
작업이 그 사이 프리셋을 바꾸면 덮어쓰지 않는다. `--restore`는 이전 저장 상태로
돌아가며, 최초 저장을 되돌리면 기본값으로 돌아간다. 실행 중인 모델은 바꾸지 않는다.

## 엔진별 적용 범위

- Ollama: 문맥, CPU 스레드, 배치, GPU 레이어 수를 요청한다. GPU 비율은 GGUF의
  레이어 수로 환산한다. `/api/ps`의 실제 문맥 길이를 확인하고 나머지 값은 생성
  요청에도 동일하게 보낸다. 문맥을 제외한 CPU/GPU 배치의 물리적 동작까지
  측정했다고 주장하지 않는다.
- LM Studio native v1: 문맥과 배치를 요청하고 `echo_load_config`의 적용 값을
  대조한다. 이 API에 없는 CPU 스레드/GPU 레이어 설정을 적용했다고 표시하지
  않는다. GUI 자체의 기본 설정 파일은 수정하지 않는다.

런타임에 이미 로드된 모델이 있으면 설정 시험은 시작하지 않는다. 메모리는
현재 여유 RAM/VRAM과 문맥·배치에 따른 버퍼 추정으로 확인하며, 다른 앱이나
모델을 시험을 위해 종료하지 않는다. 모델 응답과 정리 중 하나라도 실패하면
기존 프리셋을 유지한다. 짧은 생성 속도는 흔들릴 수 있으며 답변 품질 평가는 아니다.

## 저장한 설정이 쓰이는 곳

- `omm verify`: 다음 OMM 소유 로드에 저장 설정을 사용한다. 이미 로드된 모델은
  기존 설정을 보존하고, 저장 설정을 다시 적용하지 않았다고 안내한다.
- `omm run` + Ollama: 원본 모델을 바꾸지 않는 임시 설정 별칭으로 네이티브 채팅을
  실행한다. 채팅 후 해당 임시 별칭을 해제·삭제한다. 시작 전에 다른 모델이
  로드돼 있으면 새 프로필 로드를 진행하지 않는다.
- `omm run` + LM Studio: 로컬 API에서 설정을 적용해 로드한 뒤 네이티브 앱에
  넘긴다. GUI가 별도로 모델을 다시 로드하거나 다른 설정을 선택하는 것은 별개다.
- `benchmark`와 `contribute`는 비교 가능한 측정을 위해 기존 측정 프로필을 유지한다.

비정상적인 프로세스 강제 종료로 Ollama 임시 별칭 정리를 확인하지 못하면
`runtime-sessions` 기록이 남고 `omm doctor`가 해당 이름을 안내한다. 다른
프로그램이 사용 중일 수 있어 자동으로 지우지 않는다. 정상 종료·오류·일반
취소는 생성한 별칭만 정리한다. 설정 시험의 Ollama 로드에는 유한한 유지 시간을 쓴다.

## 실제 검증

이 Mac에서 Ollama 0.30.10, 독립 포트와 모델 저장소, 공개 19.1 MB GGUF를 사용해
CLI 설정 시험·저장·재조회·verify·네이티브 run·실제 문맥 조회·복원·정리를 확인했다.
128 문맥 모델에 큰 값을 요청했을 때 엔진이 줄이는 상황을 재현했고, 모델의 한도를
반영한 뒤 요청값과 실제 문맥 128이 일치했다. 임시 모델 별칭과 로드는 남지 않았으며
이 검사가 시작한 서버만 종료했다. 사용자의 기본 Ollama 모델 저장소를 쓰지 않았다.

재현 스크립트는 `scripts/verify_runtime_profiles_local.py`이다. 사용자가 직접
지정한 소형 GGUF와 독립적으로 확인한 체크섬이 필요하다. 서버 주소를 격리 서버로
지정하는 연결 주입과 네이티브 자식 출력 수집 외에 실제 서비스·파일 경로를 사용한다.

LM Studio의 실제 서버/GUI, Windows/Linux 실물, 장시간 추론과 다양한 대형 모델은
미검증이다. LM Studio 요청 형식과 적용값 대조는 로컬 HTTP 계약 테스트로 확인했다.

참고: [LM Studio load](https://lmstudio.ai/docs/developer/rest/load),
[Ollama create](https://docs.ollama.com/api/create),
[시험 모델과 체크섬](https://huggingface.co/ggml-org/tiny-llamas/blob/main/stories15M-q4_0.gguf).
