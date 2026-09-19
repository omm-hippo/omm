"""`omm upgrade` 의 제안 탐색 로직 (순수). 네트워크·하드웨어 조회는 전부 인자로 주입한다 - cli.py 는 얇은 호출자로만 남는다.

두 갈래를 다룬다:
  (A) 같은 HuggingFace/ModelScope repo 안에서, 메모리 예산 안에 들면서 지금보다 quant_bits 가
      더 높은 형제 파일이 있는지 - `find_quant_upgrade`.
  (B) `published/candidates.json` 에 수동으로 적어 둔 `supersedes` 계보를 따라, 지금 설치된
      모델을 대체하는 후속 큐레이션 모델이 있는지 - `find_successor`.

예외 정책: `list_repo_files`/`file_size` 가 던지는 예외는 이 모듈에서 잡지 않는다. 호출자
(cli.py) 가 모델 단위로 잡아 "확인 못 함" 한 줄을 남기고 계속 진행한다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from omm.featurize import is_mmproj_filename, is_shard_filename, parse_quant_bits

KIND_SUCCESSOR = "successor"   # (B)
KIND_QUANT = "quant"           # (A)

_PROVIDER_PREFIXES = {
    "huggingface": "hf",
    "modelscope": "ms",
}

# Pulls the visible quant token (e.g. "Q4_K_M", "IQ2_XS") out of a filename
# for display. Deliberately not derived from parse_quant_bits's numeric
# result - showing the raw token from the filename reads better to users
# than reconstructing it from a bit count.
_QUANT_LABEL_RE = re.compile(r"I?Q[1-8][^.\-_]*(_[A-Z0-9]+)*")


@dataclass(frozen=True)
class Suggestion:
    installed_filename: str      # 지금 설치된 파일명(레지스트리 키)
    kind: str                    # KIND_SUCCESSOR | KIND_QUANT
    provider: str                # "huggingface" | "modelscope"
    repo_id: str
    filename: str                # 제안하는 파일명
    reason: str                  # 표에 그대로 찍는 한 줄 근거
    ref: str                     # `hub.resolve_model` 에 넘길 문자열 ("hf:owner/repo:file.gguf")
    predicted_tps: float | None = None
    quant_bits: float | None = None
    size_bytes: int | None = None


def provider_prefix(provider: str) -> str:
    """"huggingface" -> "hf", "modelscope" -> "ms" (hub._PREFIXES 의 역방향)."""
    return _PROVIDER_PREFIXES.get(provider, provider)


def quant_label(filename: str) -> str | None:
    """파일명에서 사람이 읽을 quant 토큰을 뽑는다 (예: "Q4_K_M"). 못 뽑으면 None -
    호출자가 `f"{bits:g}-bit"` 로 대체해야 한다."""
    match = _QUANT_LABEL_RE.search(filename.upper())
    if match is None:
        return None
    return match.group(0)


def find_successor(
    candidates: list[dict], *, repo_id: str | None, filename: str, provider: str,
    already_installed: frozenset[tuple[str, str, str]] = frozenset(),
) -> dict | None:
    """(B). candidates 에서 (provider, repo_id, filename) 이 정확히 일치하는 항목을 찾고,
    그 항목의 name 을 자신의 supersedes 에 담고 있는 **첫 번째** 다른 항목을 돌려준다.
    없으면 None (조용한 스킵). `already_installed`((provider, repo_id, filename.casefold())
    튜플 집합)에 있는 후속작은 건너뛴다 - omm으로 이미 설치돼 있으면 더 이상 "제안"이 아니다."""
    current = None
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        candidate_provider = candidate.get("provider") or "huggingface"
        if (
            candidate.get("repo_id") == repo_id
            and candidate.get("filename") == filename
            and candidate_provider == provider
        ):
            current = candidate
            break
    if current is None:
        return None

    current_name = current.get("name")
    if not isinstance(current_name, str) or not current_name:
        return None

    for candidate in candidates:
        if not isinstance(candidate, dict) or candidate is current:
            continue
        supersedes = candidate.get("supersedes")
        if not isinstance(supersedes, list) or current_name not in supersedes:
            continue
        successor_repo_id = candidate.get("repo_id")
        successor_filename = candidate.get("filename")
        if not isinstance(successor_repo_id, str) or not isinstance(successor_filename, str):
            # 방어적: 아티팩트가 서명돼 있어도 필드 타입이 이상하면 건너뛴다.
            continue
        successor_provider = candidate.get("provider") or "huggingface"
        if (
            successor_repo_id == repo_id
            and successor_filename == filename
            and successor_provider == provider
        ):
            # 자기 자신 계승 방지.
            continue
        if (successor_provider, successor_repo_id, successor_filename.casefold()) in already_installed:
            # 이미 omm으로 설치돼 있는 파일을 다시 "제안"하지 않는다 - 다음 후보를 계속 찾는다.
            continue
        return candidate

    return None


def find_quant_upgrade(
    *,
    provider: str,
    repo_id: str,
    installed_filename: str,
    list_repo_files: Callable[[str, str], tuple[list[str], float | None]],
    file_size: Callable[[str, str, str], int | None],
    predict_tps: Callable[[dict], float | None],
    fits_budget: Callable[[dict], bool],
    already_installed: frozenset[str] = frozenset(),
) -> Suggestion | None:
    """(A). 같은 repo 안에서 지금보다 quant_bits 가 높고 예산 안에 드는 최선의 형제 파일.
    `already_installed`(같은 (provider, repo_id) 안에서 이미 omm으로 설치된 파일명의
    casefold 집합)에 있는 파일은 후보에서 제외한다 - 다른 target을 통해 이미 제안·설치된
    형제 quant를 다시 추천하지 않기 위해서다."""
    installed_bits = parse_quant_bits(installed_filename)
    if installed_bits is None:
        return None

    filenames, _ = list_repo_files(provider, repo_id)

    seen: set[str] = set()
    bit_filtered: list[tuple[str, float]] = []
    for candidate_filename in filenames:
        dedupe_key = candidate_filename.casefold()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        if candidate_filename == installed_filename:
            continue
        if dedupe_key in already_installed:
            continue
        if is_mmproj_filename(candidate_filename) or is_shard_filename(candidate_filename):
            continue
        bits = parse_quant_bits(candidate_filename)
        if bits is None:
            continue
        if bits <= installed_bits:
            continue
        bit_filtered.append((candidate_filename, bits))

    scored: list[tuple[str, float, int | None, float | None]] = []
    for candidate_filename, bits in bit_filtered:
        size_bytes = file_size(provider, repo_id, candidate_filename)
        candidate = {
            "repo_id": repo_id,
            "filename": candidate_filename,
            "provider": provider,
            "size_bytes": size_bytes,
        }
        if not fits_budget(candidate):
            continue
        predicted_tps = predict_tps(candidate)
        scored.append((candidate_filename, bits, size_bytes, predicted_tps))

    if not scored:
        return None

    def _sort_key(item: tuple[str, float, int | None, float | None]) -> tuple[float, float, float]:
        _filename, bits, size_bytes, predicted_tps = item
        return (
            -bits,
            -(predicted_tps if predicted_tps is not None else float("-inf")),
            size_bytes if size_bytes is not None else float("inf"),
        )

    scored.sort(key=_sort_key)
    chosen_filename, chosen_bits, chosen_size_bytes, chosen_predicted_tps = scored[0]

    installed_label = quant_label(installed_filename) or f"{installed_bits:g}-bit"
    new_label = quant_label(chosen_filename) or f"{chosen_bits:g}-bit"
    reason = f"{installed_label} → {new_label}, fits the memory budget"

    return Suggestion(
        installed_filename=installed_filename,
        kind=KIND_QUANT,
        provider=provider,
        repo_id=repo_id,
        filename=chosen_filename,
        reason=reason,
        ref=f"{provider_prefix(provider)}:{repo_id}:{chosen_filename}",
        predicted_tps=chosen_predicted_tps,
        quant_bits=chosen_bits,
        size_bytes=chosen_size_bytes,
    )
