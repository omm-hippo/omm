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
        if not isinstance(body, dict) or set(body) - {"operation", "id", "engine", "confirmed", "request_id"}:
            raise ValueError("Unsupported operation fields")
        operation = body.get("operation")
        if operation not in {"install", "link", "uninstall", "verify"}:
            raise ValueError("Unsupported operation")
        key = body.get("id")
        if not isinstance(key, str) or len(key) != 64:
            raise ValueError("Select a model from this session")
        engine = body.get("engine") or None
        if engine is not None and engine not in {s.key for s in linker.ENGINES}:
            raise ValueError("Unknown local runner")
        if body.get("confirmed") is not True:
            raise ValueError("The operation must be explicitly confirmed")
        if operation == "install":
            with self.lock:
                selected = self.candidates.get(key)
            if not selected:
                raise ValueError("Refresh the model list and select the package again")
            return {"operation": operation, **selected, "engine": engine}
        selected = next((x for x in self.models() if x["id"] == key), None)
        if not selected:
            raise ValueError("This model is not managed by OMM")
        if operation in {"link", "verify"} and not selected["exists"]:
            raise ValueError("The managed model file is missing")
        if operation == "verify" and (engine not in {"ollama", "lmstudio"} or engine not in selected["engines"]):
            raise ValueError("Select a linked Ollama or LM Studio runtime")
        return {"operation": operation, "filename": selected["filename"], "engine": engine}
