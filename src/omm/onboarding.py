"""First-run setup for interactive OMM users.

The module keeps prompting separate from CLI routing so the decision rules,
validation, and single atomic configuration update can be tested without a
real terminal. No function in this module performs a network request.
"""

from __future__ import annotations

import shutil
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

from omm import config, linker

UploadPolicy = Literal["ask", "never", "always"]
UpdateChannel = Literal["stable", "beta"]
UiMode = Literal["compact", "guided"]
OnboardingAction = Literal["configure", "skip"]


@dataclass(frozen=True)
class InvocationContext:
    stdin_is_tty: bool
    stdout_is_tty: bool
    command: str | None
    skip_onboarding: bool = False
    is_completion: bool = False


@dataclass(frozen=True)
class OnboardingState:
    onboarding_version: int
    default_engine: str | None
    telemetry_send_policy: UploadPolicy
    update_channel: UpdateChannel
    ui_mode: UiMode


@dataclass(frozen=True)
class StorageInfo:
    path: Path
    free_bytes: int | None


_SKIP_COMMANDS = {"help", "setup", "_bg-version-check"}


def should_run_onboarding(
    current: Mapping[str, Any], context: InvocationContext
) -> bool:
    """Return whether this invocation may open the first-run wizard."""
    if (
        context.skip_onboarding
        or context.is_completion
        or not context.stdin_is_tty
        or not context.stdout_is_tty
        or context.command in _SKIP_COMMANDS
    ):
        return False
    version = current.get("onboarding_version", 0)
    if isinstance(version, bool) or not isinstance(version, int):
        return False
    return version < config.CURRENT_ONBOARDING_VERSION


def inspect_storage(path: Path) -> StorageInfo:
    """Read free space from the nearest existing parent of ``path``."""
    probe = path.expanduser()
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        free_bytes = shutil.disk_usage(probe).free
    except OSError:
        free_bytes = None
    return StorageInfo(path=path.expanduser(), free_bytes=free_bytes)


def detect_supported_engines() -> tuple[str, ...]:
    """Detect supported default runtimes without starting either app."""
    detected = []
    if linker.is_ollama_installed():
        detected.append("ollama")
    if linker.is_lmstudio_installed():
        detected.append("lmstudio")
    return tuple(detected)


def context_lines(
    storage: StorageInfo,
    detected: Sequence[str],
    *,
    telemetry_endpoint: Any = None,
) -> tuple[str, ...]:
    labels = {"ollama": "Ollama", "lmstudio": "LM Studio"}
    free = (
        f"{storage.free_bytes / (1024 ** 3):.1f} GiB available"
        if storage.free_bytes is not None
        else "available space could not be read"
    )
    detected_text = ", ".join(labels.get(key, key) for key in detected) or "none"
    endpoint_text = str(telemetry_endpoint) if telemetry_endpoint else "not configured"
    return (
        "OMM keeps GGUF model files in one hub and links them to supported local AI apps.",
        "OMM does not start those apps or upload prompts and generated text.",
        f"Storage: {storage.path} ({free})",
        f"Detected without starting apps: {detected_text}",
        f"Benchmark upload destination: {endpoint_text}",
    )


def _ask_choice(
    message: str,
    choices: Sequence[tuple[str, str | None]],
    *,
    default: str | None = None,
) -> str | None:
    import questionary

    question = questionary.select(
        message,
        choices=[questionary.Choice(title, value=value) for title, value in choices],
        default=default,
    )
    try:
        return question.ask()
    except KeyboardInterrupt:
        return None


def choose_onboarding_action() -> OnboardingAction | None:
    answer = _ask_choice(
        "Set up OMM now?",
        (
            ("Set up now", "configure"),
            ("Skip for now (you can run `omm setup` later)", "skip"),
            ("Cancel", None),
        ),
        default="configure",
    )
    return answer if answer in {"configure", "skip"} else None


def _valid_default_engine(value: Any) -> str | None:
    return value if value in {"ollama", "lmstudio"} else None


def _valid_upload_policy(value: Any) -> UploadPolicy:
    return value if value in {"ask", "never", "always"} else "ask"


def _valid_update_channel(value: Any) -> UpdateChannel:
    return value if value in {"stable", "beta"} else "stable"


def _valid_ui_mode(value: Any) -> UiMode:
    return value if value in {"compact", "guided"} else "compact"


def collect_onboarding(
    current: Mapping[str, Any], *, detected: Sequence[str] | None = None
) -> OnboardingState | None:
    """Collect choices in memory. Returning ``None`` leaves config untouched."""
    detected = tuple(detected if detected is not None else detect_supported_engines())
    current_engine = _valid_default_engine(current.get("default_engine"))
    engine_choices: list[tuple[str, str | None]] = [("Automatic", "automatic")]
    if "ollama" in detected or current_engine == "ollama":
        engine_choices.append(("Ollama", "ollama"))
    if "lmstudio" in detected or current_engine == "lmstudio":
        engine_choices.append(("LM Studio", "lmstudio"))

    engine_answer = _ask_choice(
        "Default runtime:",
        engine_choices,
        default=current_engine or "automatic",
    )
    if engine_answer is None:
        return None
    if engine_answer not in {"automatic", "ollama", "lmstudio"}:
        return None
    engine = None if engine_answer == "automatic" else engine_answer

    upload_choices: list[tuple[str, str | None]] = [
        ("Ask every time (default)", "ask"),
        ("Never upload", "never"),
    ]
    if current.get("telemetry_endpoint"):
        upload_choices.append(("Always upload benchmark results", "always"))
    current_upload = _valid_upload_policy(current.get("telemetry_send_policy"))
    if current_upload == "always" and not current.get("telemetry_endpoint"):
        current_upload = "ask"
    upload = _ask_choice(
        "Benchmark upload policy:",
        upload_choices,
        default=current_upload,
    )
    if upload not in {"ask", "never", "always"}:
        return None

    channel = _ask_choice(
        "Update channel:",
        (("Stable", "stable"), ("Beta", "beta")),
        default=_valid_update_channel(current.get("update_channel")),
    )
    if channel not in {"stable", "beta"}:
        return None

    ui_mode = _ask_choice(
        "Terminal presentation:",
        (("Compact", "compact"), ("Guided", "guided")),
        default=_valid_ui_mode(current.get("ui_mode")),
    )
    if ui_mode not in {"compact", "guided"}:
        return None

    return OnboardingState(
        onboarding_version=config.CURRENT_ONBOARDING_VERSION,
        default_engine=engine,
        telemetry_send_policy=upload,
        update_channel=channel,
        ui_mode=ui_mode,
    )


def review_lines(state: OnboardingState, width: int = 80) -> tuple[str, ...]:
    engine = {None: "Automatic", "ollama": "Ollama", "lmstudio": "LM Studio"}[
        state.default_engine
    ]
    rows = (
        f"Default runtime: {engine}",
        f"Benchmark uploads: {state.telemetry_send_policy}",
        f"Update channel: {state.update_channel}",
        f"Terminal presentation: {state.ui_mode}",
    )
    wrapper = textwrap.TextWrapper(width=max(20, width), subsequent_indent="  ")
    return tuple(line for row in rows for line in wrapper.wrap(row))


def confirm_onboarding() -> bool:
    answer = _ask_choice(
        "Save these settings?",
        (("Save", "save"), ("Cancel without saving", None)),
        default="save",
    )
    return answer == "save"


def apply_onboarding(state: OnboardingState) -> dict[str, Any]:
    """Commit the complete wizard state in one locked atomic update."""
    return config.update_config(
        config_schema_version=config.CONFIG_SCHEMA_VERSION,
        onboarding_version=state.onboarding_version,
        default_engine=_valid_default_engine(state.default_engine),
        telemetry_send_policy=_valid_upload_policy(state.telemetry_send_policy),
        update_channel=_valid_update_channel(state.update_channel),
        ui_mode=_valid_ui_mode(state.ui_mode),
    )


def mark_onboarding_skipped() -> dict[str, Any]:
    """Record only the wizard decision; preserve every existing preference."""
    return config.update_config(
        config_schema_version=config.CONFIG_SCHEMA_VERSION,
        onboarding_version=config.CURRENT_ONBOARDING_VERSION,
    )
