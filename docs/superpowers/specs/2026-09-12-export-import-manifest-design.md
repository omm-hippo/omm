# export/import 매니페스트 — 설계

날짜: 2026-09-12
상태: 승인됨
관련 이슈: [omm-hippo/omm#296](https://github.com/omm-hippo/omm/issues/296),
[omm-hippo/omm#297](https://github.com/omm-hippo/omm/issues/297)

## 배경

지금 모델이 허브 밖으로 나가는 유일한 경로는 지원 러너에 링크되는 것뿐이다. 인터넷 없는
(에어갭) 머신으로 모델을 옮기고 싶을 때 방법이 없다. `omm import [directory]`는 이미 있어서
"가져오기"는 되지만(`scan_import.py`가 디렉토리를 스캔해 sha256로 묶고 허브에 편입), 그 경로로
들어온 모델은 출처(repo_id)/설치일/버전 정보가 전부 유실된다 — `adopt_group()`이 새로 편입하는
모델에는 `repo_id=None`, `source="imported"`, `installed_at=지금 시각`을 그냥 박아넣는다.

이 스펙은 두 가지를 추가한다: `omm unlink <model> --runner <engine|all>` (issue #297, 이미
구현/테스트 완료 — 이 문서는 기록 목적) 와, GGUF 옆에 출처 정보를 담은 사이드카 매니페스트를
같이 복사해서 반대편 머신의 `omm import`가 그 정보를 복원할 수 있게 하는 `omm export`
확장(issue #296).

## 목표

- `omm export <model> <destination>`: 허브 GGUF를 하드링크(불가능하면 실제 복사, 심링크는
  절대 사용 안 함)로 내보내고, 같은 디렉토리에 출처/체크섬 매니페스트를 같이 씀.
- `omm import <destination>` (기존 명령, 이미 `scan_directory()`로 임의 디렉토리를 스캔함):
  매니페스트가 있고 sha256이 실제 파일과 일치하면 그 정보로 registry를 채움. 매니페스트가
  없거나 검증에 실패하면 지금과 동일하게 동작(정보 없이 편입) — 별도 "폴백 모드"를 새로 만들
  필요 없이 이미 그게 오늘의 동작이다.
- 모델 1개 단위 (여러 모델을 하나로 묶는 번들은 범위 밖 — issue #296이 명시적으로 다음으로 미룸).

## 비목표

- GGUF 헤더를 다시 쓰거나 커스텀 키를 추가하지 않는다 (헤더를 건드리면 sha256이 바뀌어
  버리므로 원본 파일 검증이 불가능해진다).
- 여러 모델을 하나의 아카이브(tar 등)로 묶는 기능은 하지 않는다.
- LM Studio/Msty 등 서드파티 러너 디렉토리 스캔에는 매니페스트 조회를 추가하지 않는다 —
  그 디렉토리들에 omm이 쓴 매니페스트가 있을 이유가 없다. `omm import <directory>`의
  사용자 지정 디렉토리 스캔(`scan_directory()`)에만 적용한다.

## 매니페스트 형식

GGUF 파일과 같은 디렉토리에 `<filename>.omm-manifest.json`로 저장한다 (예:
`model.gguf` → `model.gguf.omm-manifest.json`). registry entry 중 이식 가능한 필드만
담고, 링크 상태처럼 로컬 머신 전용인 필드(`linked`, `custom_links`, `compatibility`)는
제외한다.

```json
{
  "schema_version": 1,
  "filename": "model.gguf",
  "sha256": "<64자리 hex>",
  "repo_id": "TheBloke/...",
  "source": "https://huggingface.co/...",
  "version": "abcdef1",
  "installed_at": "2026-09-10T12:00:00+00:00",
  "size_bytes": 123456789,
  "architecture": "llama",
  "parameter_count": 7000000000
}
```

`architecture`/`parameter_count`는 export 시점에 GGUF 헤더에서 best-effort로 읽는다
(`omm.gguf.read_gguf_metadata`). 읽기 실패해도 export 자체는 계속 진행하고 해당 필드만
비운다 — 참고용 정보일 뿐 검증 대상이 아니다.

`sha256`은 registry에 이미 기록된 값을 그대로 쓴다 (export 시점에 다시 해싱하지 않음 —
`omm install`/기존 다른 경로에서 이미 검증된 값이므로 재계산은 낭비).

## export 쪽 구현 (`src/omm/cli.py`)

`export_model` 커맨드가 `linker.export_file()`로 파일을 내보낸 뒤, registry entry에서
위 필드를 뽑아 `scan_import.write_manifest(exported_path, fields)`를 호출한다.
`write_manifest`는 `omm.atomic.atomic_write_text`로 JSON을 원자적으로 쓴다.

## import 쪽 구현 (`src/omm/scan_import.py`)

- `ExternalGguf` 데이터클래스에 `manifest: dict | None = None` 필드 추가. 기본값이 있어
  기존 생성자 호출은 전부 그대로 동작한다.
- `MANIFEST_SUFFIX = ".omm-manifest.json"`, `manifest_path_for(gguf_path)`,
  `write_manifest(gguf_path, fields)`, `_load_verified_manifest(gguf_path, sha256)` 추가.
  `_load_verified_manifest`는: 사이드카 파일이 없거나, JSON 파싱 실패, `dict`가 아니거나,
  `manifest["sha256"] != 실제 sha256`이면 `None`을 반환한다 (조용히 무시 — 오늘 동작과
  동일하게 진행).
- `scan_directory(path)`에서만 `_scan_flat_dir("import", path)`가 반환한 각 항목에 대해
  `_load_verified_manifest`를 호출해 `manifest` 필드를 채운다. 다른 스캐너
  (`scan_lmstudio`/`scan_jan`/`scan_ollama` 등)는 건드리지 않는다.
- `adopt_group()`의 새로 편입하는 분기(현재 `repo_id=None`/`source="imported"`/
  `installed_at=datetime.now(...)`를 쓰는 else 블록, scan_import.py:515-529 부근)에서
  `group.locations` 중 검증된 `manifest`가 있는 첫 항목을 찾아 있으면 그 `repo_id`/
  `source`/`version`/`installed_at`로 대체한다. 매니페스트 필드는 신뢰 경계 밖(다른 머신)에서
  온 값이므로 각 필드의 타입을 검사(`repo_id`/`source`/`version`은 `str`, `installed_at`은
  ISO 8601로 파싱 가능한 `str`)하고, 통과 못 하면 그 필드만 기존 기본값으로 되돌린다 —
  registry.json에 이상한 타입이 들어가는 것을 막기 위함이다.

## 에러 처리 / 신뢰 경계

- 매니페스트의 `sha256` 불일치 → 매니페스트 전체 무시 (탬퍼링/손상된 파일에 대한 방어).
- 매니페스트가 있지만 개별 필드 타입이 이상함 → 그 필드만 무시, 나머지는 사용.
- `--force` 없이 목적지에 이미 다른 파일이 있으면 `omm export`가 거부하는 기존 동작은
  그대로 유지 (매니페스트 파일 자체의 충돌 처리는 GGUF 파일과 동일 규칙을 따르지 않고,
  단순히 매번 덮어쓴다 — 매니페스트는 omm이 소유권을 추적하는 링크가 아니라 순수 사이드카
  데이터 파일이기 때문).

## 테스트 계획

- `tests/test_cli_export.py`: export 후 매니페스트 파일 생성 확인, 내용 필드 확인,
  GGUF 헤더 읽기 실패 시에도 export 성공하는지.
- `tests/test_scan_import.py` (신규 또는 기존 파일에 추가): 유효한 매니페스트로 import 시
  registry에 repo_id/source/installed_at이 복원되는지, sha256 불일치 시 무시되는지,
  손상된 JSON/타입 이상 필드 시 안전하게 폴백하는지, 매니페스트 없을 때 기존 동작(변경 없음)
  유지되는지.

## 이미 구현된 부분

`omm unlink <model> --runner <engine|all>` (issue #297)은 이 스펙 작성 이전에 이미
구현·테스트 완료됨 (`tests/test_cli_unlink.py`, 15개 테스트 전체 통과, 전체 스위트 회귀 없음
확인). `omm export`의 기본 파일 복사 메커니즘(`linker.export_file`, 하드링크→실제 복사,
심링크 금지)도 이미 구현 완료 — 이 스펙은 그 위에 매니페스트 기록/복원 레이어를 추가한다.
