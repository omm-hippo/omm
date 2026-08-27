# llama.cpp 자체 실행 조사 — 2026-08-27

GitHub issue #97의 조건부 조사 결과다. 결론은 **현재 OMM에
`llama-cpp-python`을 설치하거나 내장하지 않는다**이다. CPU/Metal의 기술적
실행 가능성은 확인했지만, 배포 무결성·GPU 패키지 크기·Python 지원 범위가
OMM의 기본 runner로 채택하기에는 아직 불안정하다.

## 결론

- OMM 본체의 필수 dependency나 wheel에 `llama-cpp-python`을 넣지 않는다.
- 사용자의 첫 실행 때 임의의 wheel을 내려받는 기능도 지금은 추가하지 않는다.
- 향후 재검토하더라도 OMM 프로세스에서 `from llama_cpp import Llama`를 직접
  실행하지 않는다. 별도 Python 환경의 subprocess worker만 허용한다.
- 따라서 #97의 원래 조건부 목표 중 “용량/플랫폼 조사 후 과도하면 보류 또는
  종료” 조건을 충족하며, 구현 이슈로 전환하지 않고 종료할 수 있다.

## 실제 측정

측정 환경:

- macOS 26.5.2, Apple Silicon arm64
- Python 3.14.7
- OMM 0.2.186 wheel: 363,791 bytes
- 조사한 upstream: `llama-cpp-python` 0.3.35 (2026-08-17 공개)

공식 GitHub release asset의 압축 크기:

| Backend / platform | Wheel size | OMM wheel 대비 |
| --- | ---: | ---: |
| Windows CPU x64 | 6.76 MiB | 19.5× |
| macOS Metal arm64 | 17.32 MiB | 49.9× |
| Linux CPU x64 | 22.80 MiB | 65.7× |
| Windows CUDA (largest published) | 460.41 MiB | 1,327× |
| Linux CUDA | 725.94–1,607.32 MiB | 2,092–4,633× |
| Linux ROCm 7.2 | 818.58 MiB | 2,359× |
| PyPI source archive | 71.40 MiB | 205.8× |

근거는 [PyPI 0.3.35](https://pypi.org/project/llama-cpp-python/0.3.35/),
[GitHub 0.3.35 release](https://github.com/abetlen/llama-cpp-python/releases/tag/v0.3.35),
그리고 backend별 0.3.35 release assets다. Upstream README는 CPU, Metal,
CUDA, ROCm/HIP, Vulkan, SYCL을 서로 다른 빌드/index로 설명하며, 가속 wheel의
Python 범위를 3.10–3.12로 명시한다.

현재 macOS wheel은 크기 외에도 무결성 문제가 있었다.

| Asset | 게시 SHA-256 일치 | ZIP/pip 설치 |
| --- | --- | --- |
| `v0.3.35-metal` macOS arm64 | yes | CRC 오류 |
| `v0.3.35` macOS arm64 | yes | `libggml-base.0.20.0.dylib` CRC 오류 |
| `v0.3.34` macOS arm64 | yes | CRC 오류 |

즉 네트워크 중간 손상이 아니라, 내려받은 파일이 GitHub가 게시한 digest와
일치하는 상태에서 `unzip -t`와 pip가 모두 거부했다. 같은 wheel을 재시도하는
것으로 해결되지 않는다.

소스 빌드 대안은 다음과 같이 동작했다.

- `CMAKE_ARGS=-DGGML_METAL=on`, `--no-binary llama-cpp-python`
- 0.3.35 build/install 성공
- 깨끗한 Python venv 대비 설치 footprint 증가: 84,332 KiB
- `ggml-org/tiny-llamas/stories260K.gguf` 1,185,376 bytes를 LFS SHA-256으로 검증
- Metal `n_gpu_layers=-1`로 모델 load 성공
- 8-token completion: 비어 있지 않은 결과, load 6.801초, generation 0.127초

이는 **소스 빌드된 한 Mac에서 completion이 실행됨**을 뜻할 뿐이다. 공식
wheel 설치, 다른 OS/GPU, 실제 OMM benchmark/contribute, chat completion,
여러 모델 품질을 증명하지 않는다.

## 남은 기술 위험

### Native crash 격리

`llama-cpp-python`은 ctypes로 네이티브 llama.cpp를 같은 프로세스에 올린다.
segfault, abort, Metal/CUDA driver crash는 Python `try/except`로 복구할 수
없다. OMM CLI 전체가 함께 종료되므로 in-process 채택은 허용할 수 없다.

재검토 시 구조는 다음과 같아야 한다.

1. OMM 관리 디렉터리에 backend별 별도 Python 환경을 설치한다.
2. OMM은 JSONL/stdin-stdout 프로토콜의 subprocess worker만 실행한다.
3. worker가 signal로 종료되면 OMM은 `load_failed`/`gpu_crash`로 분류하고
   체크포인트를 보존한다.
4. 모델 path, context, offload 값은 allowlist된 필드로만 전달한다.
5. worker dependency digest와 wheel integrity를 설치 전/후 검증한다.

### Chat template

Upstream은 GGUF metadata의 chat template과 `create_chat_completion()`을
지원하지만, 모델별 template 해석은 llama.cpp 릴리스에 따라 바뀐다. 이번
실측은 tiny completion 모델 하나였으며 다음은 **미검증**이다.

- OMM이 실제 추천하는 dense/MoE/reasoning 모델들의 chat template
- Ollama 대비 prompt와 output 품질 동등성
- tool/reasoning template, stop token, BOS/EOS 처리

### GPU offload와 메모리 가드

`n_gpu_layers=-1`은 “전부 GPU”라는 요청일 뿐, 사용 가능한 VRAM에 맞춘
안전한 layer 수가 아니다. OMM의 현재 Memory Guard는 Ollama/LM Studio의
resident model과 실제 runtime 상태를 읽는다. llama.cpp worker에는 다음이
별도로 필요하다.

- GGUF layer/tensor별 실제 memory estimate
- KV cache/context/batch 포함 peak allocation
- unified memory, partial offload, CUDA version/driver 호환성
- worker가 실제 적용한 layer 수와 backend의 관측 가능한 receipt

## 재검토 조건

다음 조건을 모두 만족하기 전에는 구현 이슈를 다시 열지 않는다.

1. OMM 지원 Python/OS matrix에서 공식 wheel의 digest와 ZIP integrity가 통과한다.
2. 설치 가능한 CPU/Metal/CUDA/ROCm/Vulkan asset이 backend별로 명확히 구분되고,
   없는 조합은 source build 없이 fail-closed 안내가 가능하다.
3. GPU wheel의 다운로드/디스크 예산을 제품 정책으로 승인한다.
4. subprocess crash-isolation prototype이 signal termination, timeout, cleanup,
   checkpoint recovery 테스트를 통과한다.
5. 최소 3개 실제 OMM 추천 모델(dense, MoE, reasoning)의 chat 품질과 template을
   Ollama baseline과 비교한다.
6. 실제 적용된 offload와 메모리 사용량을 관측해 Memory Guard contract에
   연결한다.

## 검증 수준

- **Implemented**: 조사/결정 문서와 재검토 기준.
- **Unit-verified**: 해당 없음. OMM runtime 코드는 추가하지 않았다.
- **Simulator-verified**: 해당 없음.
- **Physical-device-verified**: 이 Mac에서 0.3.35 Metal source build와 tiny GGUF
  completion만 확인했다.
- **Not verified / 미검증**: 공식 wheel 설치 성공, Windows/Linux/AMD/NVIDIA,
  실제 OMM 경로, 여러 모델 chat 품질, subprocess crash recovery, 자동 layer
  계산.
