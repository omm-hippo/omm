"""Read-only views and bounded identifiers for the existing OMM core."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import threading

from omm import config, hardware, hub, linker, model_wiki, predictor, recommend_evidence, recommend_facts, recommend_status, registry, rules
from omm import recommend_metadata, recommend_selection, search


def identifier(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _count(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 100_000_000 else None


def recommendation_evidence(artifact: dict | None, info) -> dict:
    return recommend_evidence.describe(artifact, info)


class WebService:
    def __init__(self):
        self.candidates: dict[str, dict] = {}
        self.lock = threading.RLock()
        self.partial_files: dict[str, dict] = {}
        self.discovered: dict[str, list[dict]] = {}

    def wiki_packages(self, model_id: str, profile: str = "balanced") -> dict:
        if profile not in predictor.RECOMMEND_PROFILES:
            raise ValueError("Unknown memory profile")
        selected = next((x for x in model_wiki.catalog()["models"] if x["id"] == model_id), None)
        if not selected:
            raise ValueError("위키에서 모델을 선택해 주세요.")
        artifact = predictor.load_cached_model()
        candidates = list(artifact["candidates"]) if artifact else []
        if not artifact:
            for rule in rules.load_rules():
                coordinates = hub.CURATED_INDEX.get(rule.get("name"))
                if coordinates:
                    repo, filename = coordinates
                    candidates.append({**rule, "repo_id": repo, "filename": filename, "provider": "huggingface"})
        with self.lock:
            candidates.extend(self.discovered.get(model_id, []))
        matches = {}
        for candidate in candidates:
            description = candidate.get("wiki") or model_wiki.describe(candidate)
            if description and description["id"] == model_id:
                matches[search.exact_install_ref(candidate)] = (candidate, description)
        info = hardware.scan_hardware()
        rows = []
        budget = min(predictor.profile_memory_cap_gb(info, profile), hardware.calculate_memory_budget(info).install_budget_gb)
        for ref, (candidate, description) in list(matches.items())[:100]:
            memory = predictor.estimate_required_memory_gb(candidate)
            fit = predictor.candidate_fits_memory(info, candidate)
            fits = None if fit is None or memory is None else fit is True and memory <= budget
            cid = identifier(ref)
            with self.lock:
                self.candidates[cid] = {"ref": ref, "filename": candidate["filename"]}
            rows.append({"id": cid, "ref": ref, "filename": candidate["filename"], "name": selected["name"],
                         "quantization": recommend_selection.quantization_label(candidate), "fits": fits,
                         "memory_required_gb": memory, "size_bytes": candidate.get("size_bytes"),
                         "wiki": description})
        rows.sort(key=lambda x: (x["fits"] is not True, x["memory_required_gb"] or float("inf")))
        return {"model": selected, "packages": rows, "budget_gb": budget}

    def discover_packages(self, model_id: str, profile: str = "balanced") -> dict:
        from omm.web.discovery import discover
        selected = next((x for x in model_wiki.catalog()["models"] if x["id"] == model_id), None)
        if not selected:
            raise ValueError("위키에서 모델을 선택해 주세요.")
        candidates = discover(selected)
        with self.lock:
            self.discovered[model_id] = candidates
        return self.wiki_packages(model_id, profile)

    def files(self) -> dict:
        from omm.web import management
        value = management.files()
        with self.lock:
            self.partial_files = {x["id"]: x for x in value["partials"]}
        return value

    def model_settings(self, key: str) -> dict:
        from omm.web import management
        selected = next((x for x in self.models() if x["id"] == key and x["exists"]), None)
        if not selected:
            raise ValueError("설치된 모델을 선택해 주세요.")
        return management.model_settings(selected["filename"], registry.load_registry()[selected["filename"]])

    def chat_request(self, body: dict) -> dict:
        if not isinstance(body, dict) or set(body) - {"id", "engine", "confirmed", "request_id", "chat_id"}:
            raise ValueError("지원하지 않는 채팅 입력이에요.")
        return self.request({k: v for k, v in {**body, "operation": "verify"}.items() if k != "chat_id"})

    def machine(self) -> dict:
        info = hardware.scan_hardware()
        return {
            **asdict(info),
            "model_budget_gb": hardware.calculate_memory_budget(info).model_budget_gb,
            "engines": [{"key": s.key, "name": s.label, "installed": linker.is_engine_installed(s.key)} for s in linker.ENGINES],
        }

    def models(self) -> list[dict]:
        rows = []
        for filename, entry in registry.load_registry().items():
            if not isinstance(entry, dict):
                continue
            try:
                hub.validate_model_filename(filename)
                path = config.MODELS_DIR / filename
                if not path.resolve().is_relative_to(config.MODELS_DIR.resolve()):
                    continue
                exists = path.is_file()
                size = path.stat().st_size if exists else 0
            except (OSError, ValueError, hub.ModelResolutionError):
                continue
            linked = entry.get("linked", {})
            rows.append({"id": identifier(filename), "filename": filename, "size_bytes": size,
                         "exists": exists, "engines": [k for k, v in linked.items() if v is True] if isinstance(linked, dict) else [],
                         "compatibility": entry.get("compatibility", {})})
        return rows

    def refresh_catalog(self) -> dict:
        settings = config.load_config()
        previous = predictor.load_cached_model()
        artifact = predictor.fetch_and_cache_model(settings["model_url"], settings.get("catalog_manifest_url"),
                                                  settings.get("catalog_public_key"))
        return {"updated": artifact != previous}

    def recommendations(self, profile: str = "balanced", query: str = "") -> dict:
        if profile not in predictor.RECOMMEND_PROFILES:
            raise ValueError("Unknown memory profile")
        if not isinstance(query, str) or len(query) > 160:
            raise ValueError("Search is limited to 160 characters")
        info = hardware.scan_hardware()
        artifact = predictor.load_cached_model()
        evidence = recommendation_evidence(artifact, info)
        if artifact:
            artifact = recommend_facts.apply(artifact)
            candidates = artifact["candidates"]
        else:
            candidates = []
            for rule in rules.load_rules():
                coordinates = hub.CURATED_INDEX.get(rule.get("name"))
                if coordinates:
                    repo, filename = coordinates
                    candidates.append({**rule, "repo_id": repo, "filename": filename, "provider": "huggingface"})
        if query.strip():
            terms = query.casefold().split()
            candidates = [c for c in candidates if all(t in (str(c.get("name", "")) + " " + c["repo_id"] + " " + c["filename"]).casefold() for t in terms)]
        budget = min(predictor.profile_memory_cap_gb(info, profile), hardware.calculate_memory_budget(info).install_budget_gb)
        if artifact:
            ranked = predictor.rank_candidates({**artifact, "candidates": candidates}, info)
        else:
            ranked = [(c, None) for c in candidates]
        ranked = [(c, speed) for c, speed in ranked if predictor.candidate_fits_memory(info, c) is True
                  and (predictor.estimate_required_memory_gb(c) or float("inf")) <= budget]
        ranked.sort(key=lambda pair: predictor.estimate_required_memory_gb(pair[0]) or 0, reverse=True)
        eligible = len(ranked)
        ranked = recommend_selection.shortlist(ranked)
        statuses = recommend_status.detect_installation_statuses([c for c, _ in ranked])
        rows = []
        with self.lock:
            for (candidate, speed), status in zip(ranked, statuses):
                ref = search.exact_install_ref(candidate)
                cid = identifier(ref)
                self.candidates[cid] = {"ref": ref, "filename": candidate["filename"]}
                labels = recommend_metadata.classify(candidate)
                rows.append({"id": cid, "name": recommend_selection.model_label(candidate["repo_id"]),
                             "ref": ref, "filename": candidate["filename"],
                             "quantization": recommend_selection.quantization_label(candidate),
                             "memory_required_gb": predictor.estimate_required_memory_gb(candidate),
                             "predicted_tokens_per_second": speed, "use_case": labels.use_case,
                             "model_type": labels.model_type, "use_case_source": labels.use_case_source,
                             "installed": status.installed, "installed_engines": list(status.engines),
                             "managed_by_omm": status.managed_by_omm, "installation_match": status.match_kind,
                             "variant_warning": recommend_selection.variant_warning(candidate),
                             "wiki": model_wiki.describe(candidate),
                             "evidence": {**recommend_evidence.describe(artifact, info, candidate), "memory_basis": predictor.memory_estimate_basis(candidate)}})
            if len(self.candidates) > 4096:
                self.candidates = {row["id"]: self.candidates[row["id"]] for row in rows}
        signed = bool(artifact) and predictor.cached_model_signature_is_valid(config.load_config().get("catalog_public_key"))
        source = ("signed_cache" if signed else "local_cache") if artifact else "bundled_rules"
        return {"models": rows, "profile": profile, "budget_gb": budget, "eligible_count": eligible,
                "catalog_source": source, "evidence": evidence,
                "catalog": {"trained_at": artifact.get("trained_at") if artifact else None,
                            "real_configuration_count": _count(artifact.get("real_row_count")) if artifact else None,
                            "holdout_rows": _count(artifact.get("evaluation", {}).get("holdout_rows"))
                                            if artifact and isinstance(artifact.get("evaluation"), dict) else None}}

    def request(self, body: dict) -> dict:
        if not isinstance(body, dict) or set(body) - {"operation", "id", "ids", "engine", "confirmed", "request_id"}:
            raise ValueError("Unsupported operation fields")
        operation = body.get("operation")
        if operation not in {"install", "link", "uninstall", "verify", "compare", "profile_save", "profile_restore", "engine_install", "import_scan", "import", "cleanup"}:
            raise ValueError("Unsupported operation")
        if body.get("confirmed") is not True:
            raise ValueError("The operation must be explicitly confirmed")
        engine = body.get("engine") or None
        if engine is not None and engine not in {s.key for s in linker.ENGINES}:
            raise ValueError("Unknown local runner")
        if operation in {"import_scan", "cleanup", "engine_install", "compare"}:
            if body.get("id") is not None:
                raise ValueError("이 작업에는 단일 모델 ID를 사용하지 않아요.")
            if operation == "engine_install":
                if engine is None or body.get("ids") is not None:
                    raise ValueError("설치할 실행 앱을 선택해 주세요.")
                return {"operation": operation, "filename": engine, "engine": engine}
            if operation == "import_scan":
                if engine is not None or body.get("ids") is not None:
                    raise ValueError("파일 검색에는 추가 입력을 사용하지 않아요.")
                return {"operation": operation, "filename": "외부 모델 검색"}
            ids = body.get("ids")
            maximum = 4 if operation == "compare" else 50
            if not isinstance(ids, list) or not 1 <= len(ids) <= maximum or any(not isinstance(x, str) for x in ids) or len(set(ids)) != len(ids):
                raise ValueError(f"1–{maximum}개의 서로 다른 대상을 선택해 주세요.")
            if operation == "cleanup":
                with self.lock:
                    rows = [self.partial_files.get(key) for key in ids]
                if engine is not None or any(x is None for x in rows):
                    raise ValueError("파일 목록을 다시 확인해 주세요.")
                return {"operation": operation, "filename": "불완전 다운로드 정리", "files": rows}
            if engine not in {"ollama", "lmstudio"}:
                raise ValueError("비교할 실행 앱을 선택해 주세요.")
            managed = {x["id"]: x for x in self.models()}
            rows = [managed.get(key) for key in ids]
            if any(x is None or not x["exists"] or engine not in x["engines"] for x in rows):
                raise ValueError("선택한 실행 앱에 연결된 모델을 선택해 주세요.")
            return {"operation": operation, "filename": "모델 성능 비교", "filenames": [x["filename"] for x in rows], "engine": engine}
        key = body.get("id")
        if not isinstance(key, str) or len(key) != 64:
            raise ValueError("Select a model from this session")
        if body.get("ids") is not None:
            raise ValueError("이 작업에는 단일 대상을 선택해 주세요.")
        if operation == "import":
            from omm.web.maintenance import read_import_preview
            selected = next((x for x in read_import_preview() if x["id"] == key), None)
            if not selected or engine is not None:
                raise ValueError("외부 모델 검색 결과에서 다시 선택해 주세요.")
            return {"operation": operation, "filename": selected["name"], "group": selected}
        if operation == "install":
            with self.lock:
                selected = self.candidates.get(key)
            if not selected:
                raise ValueError("Refresh the model list and select the package again")
            return {"operation": operation, **selected, "engine": engine}
        selected = next((x for x in self.models() if x["id"] == key), None)
        if not selected:
            raise ValueError("This model is not managed by OMM")
        if operation in {"link", "verify", "profile_save", "profile_restore"} and not selected["exists"]:
            raise ValueError("The managed model file is missing")
        if operation in {"verify", "profile_save", "profile_restore"} and (engine not in {"ollama", "lmstudio"} or engine not in selected["engines"]):
            raise ValueError("Select a linked Ollama or LM Studio runtime")
        return {"operation": operation, "filename": selected["filename"], "engine": engine}
