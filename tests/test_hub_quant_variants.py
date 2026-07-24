import pytest

from omm import hub
from omm.hub import AmbiguousModelError, ModelResolutionError, resolve_model
from omm.providers import huggingface


class _FakeResponse:
    def __init__(self, siblings, gguf_total=None):
        self._siblings = siblings
        self._gguf_total = gguf_total

    def raise_for_status(self):
        pass

    def json(self):
        payload = {"siblings": self._siblings}
        if self._gguf_total is not None:
            payload["gguf"] = {"total": self._gguf_total}
        return payload


def test_resolve_model_raises_ambiguous_error_with_repo_and_candidates(monkeypatch):
    monkeypatch.setattr(
        huggingface.requests,
        "get",
        lambda url, timeout: _FakeResponse(
            [
                {"rfilename": "llama-2-7b.Q4_K_M.gguf"},
                {"rfilename": "llama-2-7b.Q8_0.gguf"},
                {"rfilename": "README.md"},
            ]
        ),
    )

    with pytest.raises(AmbiguousModelError) as exc_info:
        resolve_model("TheBloke/Llama-2-7B-GGUF")

    err = exc_info.value
    assert err.repo_id == "TheBloke/Llama-2-7B-GGUF"
    assert err.candidates == ["llama-2-7b.Q4_K_M.gguf", "llama-2-7b.Q8_0.gguf"]


def test_rank_quant_variants_orders_fitting_candidates_by_quality_first():
    candidates = [
        "llama-2-7b.Q2_K.gguf",
        "llama-2-7b.Q4_K_M.gguf",
        "llama-2-7b.Q8_0.gguf",
    ]

    ranked = hub.rank_quant_variants(candidates, available_gb=6.0)

    assert [v.filename for v in ranked] == [
        "llama-2-7b.Q4_K_M.gguf",
        "llama-2-7b.Q2_K.gguf",
        "llama-2-7b.Q8_0.gguf",
    ]
    assert ranked[0].fits is True
    assert ranked[1].fits is True
    assert ranked[2].fits is False


def test_rank_quant_variants_marks_unparsable_filename_fit_as_unknown():
    ranked = hub.rank_quant_variants(["mystery-file.gguf"], available_gb=6.0)

    assert ranked[0].fits is None
    assert ranked[0].required_gb is None


def test_rank_quant_variants_falls_back_to_repo_level_param_count():
    # Regression: filenames like "ID_Legal_Assistant_Q8_0.gguf" carry a
    # quant tag but no param count, so per-filename parsing alone always
    # reports "fit unknown" - even though HF's own GGUF-header parse (the
    # "gguf.total" field from the repo API response) has the real count.
    ranked = hub.rank_quant_variants(
        ["ID_Legal_Assistant_Q8_0.gguf"], available_gb=6.0, param_count_b=8.19
    )

    assert ranked[0].fits is False
    assert ranked[0].required_gb == pytest.approx(8.19 * 8 / 8 * 1.2)


def test_resolve_model_skips_mmproj_when_repo_has_single_real_model(monkeypatch):
    # Regression: a repo whose sole *non-mmproj* .gguf is the real model, but
    # which also ships an mmproj file, must not let the mmproj file win the
    # "only one candidate" auto-select shortcut just because it's listed too.
    monkeypatch.setattr(
        huggingface.requests,
        "get",
        lambda url, timeout: _FakeResponse(
            [
                {"rfilename": "llava-v1.6-mistral-7b.Q4_K_M.gguf"},
                {"rfilename": "mmproj-model-f16.gguf"},
            ]
        ),
    )

    resolved = resolve_model("liuhaotian/llava-v1.6-mistral-7b-GGUF")

    assert resolved.filename == "llava-v1.6-mistral-7b.Q4_K_M.gguf"


def test_resolve_model_raises_when_repo_only_has_mmproj_files(monkeypatch):
    # Regression: a repo (or mirror) that only publishes the mmproj file
    # must not be silently installed as if it were a standalone model.
    monkeypatch.setattr(
        huggingface.requests,
        "get",
        lambda url, timeout: _FakeResponse([{"rfilename": "mmproj-model-f16.gguf"}]),
    )

    with pytest.raises(ModelResolutionError, match="multimodal projector"):
        resolve_model("some-org/mmproj-only-repo")


def test_resolve_model_excludes_mmproj_from_ambiguous_candidates(monkeypatch):
    # Regression: when multiple real quants exist alongside an mmproj file,
    # the mmproj file must not appear in the quant picker at all - it isn't
    # a quant of the model.
    monkeypatch.setattr(
        huggingface.requests,
        "get",
        lambda url, timeout: _FakeResponse(
            [
                {"rfilename": "llava-v1.6-mistral-7b.Q4_K_M.gguf"},
                {"rfilename": "llava-v1.6-mistral-7b.Q8_0.gguf"},
                {"rfilename": "mmproj-model-f16.gguf"},
            ]
        ),
    )

    with pytest.raises(AmbiguousModelError) as exc_info:
        resolve_model("liuhaotian/llava-v1.6-mistral-7b-GGUF")

    assert exc_info.value.candidates == [
        "llava-v1.6-mistral-7b.Q4_K_M.gguf",
        "llava-v1.6-mistral-7b.Q8_0.gguf",
    ]


def test_resolve_model_ambiguous_error_carries_repo_level_param_count(monkeypatch):
    monkeypatch.setattr(
        huggingface.requests,
        "get",
        lambda url, timeout: _FakeResponse(
            [
                {"rfilename": "ID_Legal_Assistant_Q4_K_M.gguf"},
                {"rfilename": "ID_Legal_Assistant_Q8_0.gguf"},
            ],
            gguf_total=8_190_735_360,
        ),
    )

    with pytest.raises(AmbiguousModelError) as exc_info:
        resolve_model("Azzindani/Deepseek_ID_Legal_Preview_GGUF")

    assert exc_info.value.param_count_b == pytest.approx(8.19073536)


def test_best_filenames_by_tier_picks_fastest_per_quant_tier():
    variants = [
        hub.QuantVariant("q4-a.gguf", quant_bits=4.0, required_gb=4.0, fits=True),
        hub.QuantVariant("q4-b.gguf", quant_bits=4.0, required_gb=4.0, fits=True),
        hub.QuantVariant("q5-a.gguf", quant_bits=5.0, required_gb=5.0, fits=True),
        hub.QuantVariant("q5-b.gguf", quant_bits=5.0, required_gb=5.0, fits=True),
    ]
    predicted_speed = {
        "q4-a.gguf": 10.0,
        "q4-b.gguf": 12.0,
        "q5-a.gguf": 8.0,
        "q5-b.gguf": 6.0,
    }

    best = hub.best_filenames_by_tier(variants, predicted_speed)

    assert best == {"q4-b.gguf", "q5-a.gguf"}


def test_best_filenames_by_tier_ties_resolve_to_first_in_list_order():
    variants = [
        hub.QuantVariant("first.gguf", quant_bits=4.0, required_gb=4.0, fits=True),
        hub.QuantVariant("second.gguf", quant_bits=4.0, required_gb=4.0, fits=True),
    ]
    predicted_speed = {"first.gguf": 10.0, "second.gguf": 10.0}

    best = hub.best_filenames_by_tier(variants, predicted_speed)

    assert best == {"first.gguf"}


def test_best_filenames_by_tier_ignores_variants_missing_from_speed_map():
    variants = [
        hub.QuantVariant("known.gguf", quant_bits=4.0, required_gb=4.0, fits=True),
        hub.QuantVariant("unknown.gguf", quant_bits=4.0, required_gb=4.0, fits=None),
    ]
    predicted_speed = {"known.gguf": 10.0}

    best = hub.best_filenames_by_tier(variants, predicted_speed)

    assert best == {"known.gguf"}


def test_best_filenames_by_tier_empty_speed_map_returns_empty_set():
    variants = [hub.QuantVariant("a.gguf", quant_bits=4.0, required_gb=4.0, fits=True)]

    assert hub.best_filenames_by_tier(variants, {}) == set()
