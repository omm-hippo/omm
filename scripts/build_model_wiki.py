"""Build the bundled wiki offline from reviewed publisher facts, never weights.

No inference from similarly named repositories and no runtime network refresh.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
PUBLISHERS = {"Qwen": ("Qwen", "큐웬 퀀"), "meta-llama": ("Meta", "라마"),
              "google": ("Google", "젬마"), "microsoft": ("Microsoft", "파이"),
              "mistralai": ("Mistral AI", "미스트랄"), "deepseek-ai": ("DeepSeek", "딥시크"),
              "LGAI-EXAONE": ("LG AI Research", "엑사원 한국어"),
              "ibm-granite": ("IBM", "그래나이트"), "HuggingFaceTB": ("Hugging Face", "스몰엘엠"),
              "openai": ("OpenAI", "지피티 오에스에스")}


def bilingual(ko, en):
    return {"ko": ko, "en": en}


def entry(record):
    repo = record["repository"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("Invalid reviewed repository")
    org, name = repo.split("/")
    publisher, korean = PUBLISHERS[org]
    cfg = record["configuration"]
    context = next((cfg[k] for k in ("max_position_embeddings", "max_sequence_length", "seq_length", "n_positions")
                    if type(cfg.get(k)) is int and 0 < cfg[k] < 10_000_000), None)
    tags = {t.casefold() for t in record.get("tags", []) if isinstance(t, str)}
    languages = [x for x in record.get("languages", []) if isinstance(x, str)]
    hints = str(cfg.get("model_type", record.get("architectureHint", ""))).casefold()
    moe = ("moe" in hints or "mixtral" in hints or
           any(type(cfg.get(k)) is int and cfg[k] > 1 for k in ("num_local_experts", "num_experts", "n_routed_experts")))
    architecture = record.get("architecture", "moe" if moe else "dense" if record.get("configurationAvailable") else "unknown")
    labels = re.findall(r"(?i)\d+x\d+(?:\.\d+)?B|\d+(?:\.\d+)?[BM](?=[-_.]|$)", name)
    size = labels[0].upper() if labels else "미확인"
    tasks = list(record.get("reviewedTasks", ["general"]))
    if {"code", "coding", "codeqwen"} & tags:
        tasks.append("coding")
    if len(languages) > 1:
        tasks.append("multilingual")
    if record.get("vision"):
        tasks.append("vision")
    params = record.get("parametersB")
    if params is not None and params <= 4:
        tasks.append("compact")
    if architecture != "moe" and re.fullmatch(r"\d+(?:\.\d+)?[BM]", size):
        named_scale = float(size[:-1]) / (1000 if size.endswith("M") else 1)
        if named_scale <= 4:
            tasks.append("compact")
    # Model-name scale is a label, not a measured parameter count or memory
    # requirement. In particular, a MoE's active scale is not total weights.
    license_label = record.get("license") or "미확인 / Unverified"
    scope = "metadata" if record.get("revision") else "publisher_documentation"
    source_id = "metadata" if scope == "metadata" else "publisher"
    facts_ko = [f"{publisher}의 {name} 체크포인트입니다."]
    facts_en = [f"{name}, published by {publisher}."]
    if size != "미확인":
        facts_ko.append(f"이름의 규모 표기는 {size}입니다.")
        facts_en.append(f"The repository name labels its scale as {size}.")
    if context:
        facts_ko.append(f"공개 설정의 문맥 길이는 {context:,}토큰입니다.")
        facts_en.append(f"The published configuration sets a {context:,}-token context.")
    strengths = []
    if context:
        strengths.append({"text": bilingual(f"공개 설정의 문맥 한도: {context:,}토큰. 실행 앱의 실제 설정은 별도로 확인해야 합니다.",
                                             f"Published configuration context: {context:,} tokens; verify the effective runner settings separately."),
                          "basis": "publisher", "sourceId": "config"})
    if languages:
        strengths.append({"text": bilingual("공식 메타데이터의 언어 표기: " + ", ".join(languages) + ". 언어별 품질 실측은 없습니다.",
                                             "Publisher-listed languages: " + ", ".join(languages) + "; no per-language OMM quality measurement."),
                          "basis": "publisher", "sourceId": source_id})
    if record.get("toolFormatDeclared"):
        strengths.append({"text": bilingual("공식 채팅 템플릿에 도구 호출 형식이 포함됩니다. 실제 도구 작업 성공 여부는 미검증입니다.",
                                             "The official chat template declares tool-call formatting; actual tool workflows remain unverified."),
                          "basis": "publisher", "sourceId": source_id})
    if not strengths:
        strengths.append({"text": bilingual("개발사 원문을 연결한 체크포인트 정보입니다. 확인되지 않은 기능은 미확인으로 표시합니다.",
                                             "This entry links publisher checkpoint information; unconfirmed capabilities remain unknown."),
                          "basis": "editorial"})
    cautions = [{"text": bilingual("GGUF 변환본·양자화·실행 앱에 따라 입력 지원과 도구 파서가 달라질 수 있습니다. 설치 가능 여부나 품질 순위를 보증하지 않습니다.",
                                    "GGUF conversion, quantization and runner can change input/tool handling. This is not an installation or quality guarantee."),
                 "basis": "editorial"}]
    if not context:
        cautions.append({"text": bilingual("이 자료에서 정확한 문맥 한도를 확인하지 못했습니다. 다른 크기나 버전의 값을 복사하지 않았습니다.",
                                            "Exact context was not confirmed in the reviewed source; values from another size/version are not substituted."), "basis": "editorial"})
    if "custom_code" in tags:
        cautions.append({"text": bilingual("허브에 custom_code 태그가 있습니다. 이 위키는 원격 Python 코드를 실행하거나 trust_remote_code를 켜지 않습니다.",
                                            "The hub marks custom_code. This wiki never executes remote Python or enables trust_remote_code."), "basis": "publisher", "sourceId": source_id})
    if architecture == "moe":
        cautions.append({"text": bilingual("MoE의 활성 파라미터만으로 메모리 용량을 판단하면 안 됩니다. 전체 가중치와 문맥 버퍼를 함께 확인하세요.",
                                            "A MoE's active parameter count is not its total memory requirement; account for all weights and context buffers."), "basis": "editorial"})
    return {"id": re.sub(r"[^a-z0-9]+", "-", repo.casefold()).strip("-"), "name": name,
            "publisher": publisher, "repository": repo, "aliases": [repo, publisher, *korean.split()],
            "tasks": sorted(set(tasks)), "architecture": architecture, "sizeLabel": size,
            "parameterCountB": params, "activeParametersB": record.get("activeParametersB"),
            "context": {"tokens": context, "basis": "publisher_configuration" if context else "unknown",
                        "extendedTokens": None, "sourceId": "config" if context else source_id},
            "inputs": ["text", "image"] if record.get("vision") else ["text"], "inputCoverage": "confirmed_only",
            "toolCalling": record.get("toolCalling"), "toolFormatDeclared": record.get("toolFormatDeclared", False),
            "license": {"label": license_label, "url": record["sources"][0]["url"]},
            "languages": languages, "summary": bilingual(" ".join(facts_ko), " ".join(facts_en)),
            "chooseWhen": bilingual("이 체크포인트의 규모·문맥·입력·라이선스를 비교해 선택하세요. 도구 지원과 실행 가능 여부는 실제 실행 앱에서 확인하세요.",
                                     "Compare this checkpoint's scale, context, inputs and license; verify tools and compatibility in the actual runner."),
            "strengths": strengths, "cautions": cautions,
            "runtimeNote": bilingual("문맥 수치는 개발사 설정값이며 OMM 실측이 아닙니다. 전체 가중치·양자화·메모리 여유와 정확한 실행 앱 지원을 확인하세요.",
                                      "Context is publisher configuration, not an OMM measurement. Verify total weights, quantization, memory headroom and runner support."),
            "sources": record["sources"], "reviewedAt": record["reviewedAt"], "ommEvaluation": None,
            "descriptionBasis": scope}


def build(curated, reviewed):
    result = deepcopy(curated)
    models = result["models"]
    known = {m["repository"].casefold() for m in models}
    for record in reviewed["records"]:
        if record["repository"].casefold() not in known:
            models.append(entry(record))
            known.add(record["repository"].casefold())
    ids = [m["id"] for m in models]
    if len(ids) != len(set(ids)):
        raise ValueError("Model IDs must be unique")
    result.update(contentVersion="2026-10-23.1", reviewedAt=reviewed["reviewedAt"],
                  upstreamScope="original_curated_entries_only",
                  generatedBy="scripts/build_model_wiki.py", additionalSources="scripts/data/model-wiki-reviewed-20261023.json")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "src/omm/model-wiki.json")
    args = parser.parse_args()
    curated = json.loads((ROOT / "scripts/data/model-wiki-curated-v1.json").read_text(encoding="utf-8"))
    reviewed = json.loads((ROOT / "scripts/data/model-wiki-reviewed-20261023.json").read_text(encoding="utf-8"))
    data = build(curated, reviewed)
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Built {len(data['models'])} exact-checkpoint wiki entries offline.")


if __name__ == "__main__":
    main()
