"""Verified package identities shared by install and lifecycle management."""
from contextlib import contextmanager
from dataclasses import dataclass


@dataclass(frozen=True)
class EnginePackage:
    label: str
    manual_url: str
    brew_cask: str | None = None
    winget_id: str | None = None
    flatpak_id: str | None = None


PACKAGES = {
    "ollama": EnginePackage("Ollama", "https://ollama.com/download", "ollama-app", "Ollama.Ollama"),
    "lmstudio": EnginePackage("LM Studio", "https://lmstudio.ai/download", "lm-studio", "ElementLabs.LMStudio"),
    "jan": EnginePackage("Jan", "https://jan.ai/download", "jan", "Jan.Jan", "ai.jan.Jan"),
    "anythingllm": EnginePackage("AnythingLLM", "https://docs.anythingllm.com/installation-desktop/overview", "anythingllm"),
    "mstystudio": EnginePackage("Msty", "https://msty.ai/products/studio/", "mstystudio"),
    "koboldcpp": EnginePackage("KoboldCpp", "https://github.com/LostRuins/koboldcpp/releases"),
    "textgenwebui": EnginePackage("text-generation-webui", "https://github.com/oobabooga/text-generation-webui/releases"),
}


@contextmanager
def operation_lock(key: str):
    from omm import config
    from omm.atomic import locked

    if key not in PACKAGES:
        raise ValueError(f"unknown engine: {key}")
    with locked(config.OMM_HOME / "locks" / f"engine-{key}", timeout=0):
        yield
