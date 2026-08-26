"""Trusted command knowledge and deterministic routing for ``omm ask``.

This module deliberately contains no model or shell integration.  It is the
small, auditable boundary between untrusted natural-language/LLM output and
the CLI: assistants may choose a ``command_id``, but command text, warnings,
and documentation links always come from the records below.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import re
from types import MappingProxyType
import unicodedata
from collections.abc import Iterable, Mapping


MAX_QUESTION_LENGTH = 500
MAX_CANDIDATES = 5


class SideEffect(str, Enum):
    """Observable work a command may perform, including optional flows."""

    INSPECT = "inspect"
    NETWORK = "network"
    DOWNLOAD = "download"
    DISK_WRITE = "disk-write"
    DELETE = "delete"
    UPLOAD = "upload"
    RUNTIME_LOAD = "runtime-load"
    SETTINGS = "settings"
    LINK = "link"


class RiskLevel(str, Enum):
    READ_ONLY = "read-only"
    CHANGES_STATE = "changes-state"
    DESTRUCTIVE = "destructive"
    EXTERNAL_UPLOAD = "external-upload"


class QuestionSafetyError(ValueError):
    """A question must not be passed to either a local or remote model."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CommandKnowledge:
    command_id: str
    command_template: str
    summary_ko: str
    summary_en: str
    side_effects: tuple[SideEffect, ...]
    risk: RiskLevel
    required_arguments: tuple[str, ...]
    docs_path: str
    intents_ko: tuple[str, ...]
    intents_en: tuple[str, ...]


@dataclass(frozen=True)
class CommandCandidate:
    command_id: str
    score: float
    confidence: float
    matched_intents: tuple[str, ...]


@dataclass(frozen=True)
class FallbackResult:
    normalized_question: str
    candidates: tuple[CommandCandidate, ...]
    confidence: float
    clarify: bool
    reason: str


def _record(
    command_id: str,
    command_template: str,
    summary_ko: str,
    summary_en: str,
    side_effects: tuple[SideEffect, ...],
    risk: RiskLevel,
    required_arguments: tuple[str, ...],
    intents_ko: tuple[str, ...],
    intents_en: tuple[str, ...],
) -> CommandKnowledge:
    return CommandKnowledge(
        command_id=command_id,
        command_template=command_template,
        summary_ko=summary_ko,
        summary_en=summary_en,
        side_effects=side_effects,
        risk=risk,
        required_arguments=required_arguments,
        docs_path=f"/commands/{command_id}",
        intents_ko=intents_ko,
        intents_en=intents_en,
    )


_I = SideEffect.INSPECT
_N = SideEffect.NETWORK
_D = SideEffect.DOWNLOAD
_W = SideEffect.DISK_WRITE
_X = SideEffect.DELETE
_U = SideEffect.UPLOAD
_R = SideEffect.RUNTIME_LOAD
_S = SideEffect.SETTINGS
_L = SideEffect.LINK


_RECORDS = (
    _record(
        "search",
        "omm search <TEXT>",
        "이름이나 키워드로 설치 가능한 모델을 찾습니다.",
        "Find installable models by name or keyword.",
        (_I, _N),
        RiskLevel.READ_ONLY,
        ("text",),
        ("모델 검색", "모델 찾기", "허깅페이스에서 찾기", "이름으로 찾기", "검색하고 싶", "찾아보고 싶", "찾고 싶"),
        ("search models", "find a model", "look for a model", "huggingface search", "model search"),
    ),
    _record(
        "install",
        "omm install <MODEL>",
        "모델을 중앙 허브에 내려받고 설치된 실행 프로그램에 연결합니다.",
        "Download a model to the central hub and link it to installed runners.",
        (_N, _D, _W, _L),
        RiskLevel.CHANGES_STATE,
        ("model",),
        ("모델 설치", "모델 다운로드", "모델 받기", "다운받고 싶", "설치하고 싶"),
        ("install a model", "download a model", "get a model", "add a model", "model install"),
    ),
    _record(
        "run",
        "omm run [MODEL]",
        "설치된 모델을 실행 프로그램에서 열어 대화를 시작합니다.",
        "Open an installed model in a runner and start a chat.",
        (_I, _R),
        RiskLevel.CHANGES_STATE,
        (),
        ("모델 실행", "채팅 시작", "대화 시작", "모델과 대화", "모델 써보기", "질문하고 싶"),
        ("run a model", "start a chat", "chat with a model", "open model", "use a model"),
    ),
    _record(
        "recommend",
        "omm recommend",
        "현재 하드웨어에 맞는 모델을 순위별로 추천합니다.",
        "Rank models that fit the current hardware.",
        (_I, _N, _D, _W, _L),
        RiskLevel.CHANGES_STATE,
        (),
        (
            "모델 추천",
            "내 컴퓨터에 맞는 모델",
            "사양에 맞는 모델",
            "내 맥 성능",
            "내 맥에 맞는 ai",
            "내 맥에 맞는 모델",
            "맥에 맞는 ai",
            "맥에 맞는 모델",
            "맥 성능에 맞",
            "맥 사양에 맞",
            "ai 추천",
            "코딩하기 좋은 ai",
            "코딩을 하기 좋은 ai",
            "코딩 모델 추천",
            "개발용 모델 추천",
            "뭘 설치해야",
            "어떤 모델이 좋아",
            "처음 모델",
        ),
        (
            "recommend a model",
            "model for my computer",
            "model for my hardware",
            "coding model for my mac",
            "best coding ai for my mac",
            "model for coding",
            "coding model recommendation",
            "which model should i install",
            "best model for me",
        ),
    ),
    _record(
        "verify",
        "omm verify <MODEL>",
        "설치된 모델이 실제로 로드되고 로컬 텍스트를 생성하는지 확인합니다.",
        "Prove that an installed model loads and generates local text.",
        (_I, _R),
        RiskLevel.CHANGES_STATE,
        ("model",),
        ("실제로 답", "실제 생성", "생성되는지", "작동하는지 확인", "정상 실행 확인", "모델 검증", "로드되는지"),
        ("verify a model", "prove generation", "actually generates", "check model works", "test model loading", "model verification"),
    ),
    _record(
        "doctor",
        "omm doctor",
        "OMM 설치와 실행 프로그램 연결 상태를 변경 없이 진단합니다.",
        "Diagnose the OMM installation and runner links without changing state.",
        (_I,),
        RiskLevel.READ_ONLY,
        (),
        ("오류 진단", "상태 진단", "문제 확인", "왜 안 돼", "진단해줘", "고장", "설치 상태 점검", "연결 문제"),
        ("diagnose", "troubleshoot", "why is it broken", "why does it not work", "health check", "check installation"),
    ),
    _record(
        "list",
        "omm list",
        "OMM으로 설치한 모델과 연결 상태를 나열합니다.",
        "List models installed through OMM and their link status.",
        (_I,),
        RiskLevel.READ_ONLY,
        (),
        ("설치된 모델 목록", "모델 목록", "뭐가 설치", "보유 모델", "설치 목록"),
        ("list models", "installed models", "what is installed", "show my models", "model list"),
    ),
    _record(
        "uninstall",
        "omm uninstall <MODEL|all>",
        "모델과 OMM이 만든 연결을 제거합니다.",
        "Remove a model and the links created by OMM.",
        (_W, _X),
        RiskLevel.DESTRUCTIVE,
        ("model",),
        ("모델 삭제", "모델 제거", "삭제하고 싶", "제거하고 싶", "지우고 싶", "설치 해제", "전부 지우", "용량 확보하려고 모델"),
        ("uninstall a model", "delete a model", "remove a model", "erase all models", "free model space"),
    ),
    _record(
        "cleanup",
        "omm cleanup",
        "중단된 다운로드와 등록되지 않은 설치 캐시 찌꺼기를 정리합니다.",
        "Delete interrupted downloads and unregistered install-cache leftovers.",
        (_I, _X),
        RiskLevel.DESTRUCTIVE,
        (),
        ("부분 다운로드 정리", "설치 캐시 정리", "다운로드 찌꺼기", "임시 파일 삭제", "불완전한 설치 정리"),
        ("clean partial downloads", "install cache cleanup", "delete download leftovers", "remove temporary downloads", "cleanup incomplete install"),
    ),
    _record(
        "contribute",
        "omm contribute",
        "모델을 반복 측정하고 추천 개선용 텔레메트리를 동의 후 업로드합니다.",
        "Repeatedly benchmark models and, with consent, upload recommendation telemetry.",
        (_I, _N, _D, _W, _R, _U, _X),
        RiskLevel.EXTERNAL_UPLOAD,
        (),
        ("데이터 기여", "추천 개선", "벤치마크 기여", "텔레메트리 업로드", "측정값 보내기", "프로젝트 기여"),
        ("contribute data", "improve recommendations", "contribution loop", "upload telemetry", "send benchmarks", "contribute benchmarks"),
    ),
    _record(
        "setup",
        "omm setup",
        "하드웨어 확인과 실행 프로그램 선택을 포함한 초기 설정을 다시 실행합니다.",
        "Re-run first-time setup, including hardware and runner selection.",
        (_I, _N, _D, _W, _S),
        RiskLevel.CHANGES_STATE,
        (),
        ("초기 설정", "설정 마법사", "처음 설정 다시", "온보딩 다시", "실행 프로그램 설정"),
        ("initial setup", "setup wizard", "run onboarding again", "first time setup", "configure runners"),
    ),
    _record(
        "scan",
        "omm scan",
        "RAM, GPU, 운영체제, 로컬 모델과 실행 프로그램 상태를 조사합니다.",
        "Inspect hardware, local models, and installed runners.",
        (_I,),
        RiskLevel.READ_ONLY,
        (),
        ("하드웨어 확인", "컴퓨터 사양", "램 확인", "그래픽카드 확인", "로컬 모델 스캔", "pc 정보"),
        ("scan hardware", "computer specs", "check ram", "check gpu", "scan local models", "pc information"),
    ),
    _record(
        "tune",
        "omm tune <MODEL>",
        "모델에 맞는 컨텍스트, GPU 오프로딩, 스레드와 배치 크기를 추천합니다.",
        "Recommend context, GPU offload, threads, and batch size for a model.",
        (_I,),
        RiskLevel.READ_ONLY,
        ("model",),
        ("실행 설정 추천", "gpu 오프로딩", "컨텍스트 길이", "스레드 추천", "배치 크기", "모델 튜닝"),
        ("tune a model", "runtime settings", "gpu offload", "context length", "thread recommendation", "batch size"),
    ),
    _record(
        "fit",
        "omm fit <MODEL>",
        "모델이 현재 PC 메모리에 맞는지 설치 전후에 확인합니다.",
        "Check whether a model fits the current PC memory, installed or not.",
        (_I, _N),
        RiskLevel.READ_ONLY,
        ("model",),
        ("메모리에 맞는지", "실행 가능한 크기", "사양에 들어가", "램에 맞아", "모델 적합성", "설치해도 돼"),
        ("does model fit", "memory fit", "can my pc run", "model size for ram", "hardware fit", "safe to install"),
    ),
    _record(
        "help",
        "omm help [COMMAND]",
        "명령어 목록이나 특정 명령어의 사용법을 보여줍니다.",
        "Show the command list or usage for one command.",
        (_I,),
        RiskLevel.READ_ONLY,
        (),
        ("명령어 도움말", "사용법 보기", "옵션 알려줘", "명령 목록", "도움말"),
        ("command help", "show usage", "show options", "command list", "how to use omm"),
    ),
    _record(
        "import",
        "omm import [PATH]",
        "다른 로컬 AI 앱의 GGUF를 OMM 허브로 가져와 중복 저장을 줄입니다.",
        "Adopt GGUF files from other local AI apps into the OMM hub.",
        (_I, _W, _X, _L),
        RiskLevel.CHANGES_STATE,
        (),
        ("기존 모델 가져오기", "gguf 가져오기", "다른 앱 모델 옮기기", "중복 모델 정리", "외부 모델 등록"),
        ("import existing models", "import gguf", "adopt another app model", "deduplicate models", "register external model"),
    ),
    _record(
        "info",
        "omm info <MODEL>",
        "설치된 모델의 이름, 버전, 크기, 연결 및 실행 명령을 보여줍니다.",
        "Show an installed model's version, size, links, and run commands.",
        (_I,),
        RiskLevel.READ_ONLY,
        ("model",),
        ("모델 상세 정보", "모델 크기 확인", "모델 버전 확인", "실행 명령 보기", "모델 정보"),
        ("model info", "model details", "model size", "model version", "show run command"),
    ),
    _record(
        "upgrade",
        "omm upgrade [MODEL|all]",
        "설치된 모델의 소스를 확인하고 바뀐 경우에만 다시 내려받습니다.",
        "Check installed model sources and re-download only changed models.",
        (_I, _N, _D, _W),
        RiskLevel.CHANGES_STATE,
        (),
        ("모델 업데이트", "모델 최신 버전", "모델 업그레이드", "설치 모델 새로고침"),
        ("upgrade a model", "update models", "latest model version", "refresh installed models"),
    ),
    _record(
        "link",
        "omm link [DIRECTORY]",
        "OMM 모델의 실행 프로그램 연결을 만들거나 검사하고 복구합니다.",
        "Create, verify, or repair runner links for OMM models.",
        (_I, _W, _L),
        RiskLevel.CHANGES_STATE,
        (),
        ("모델 연결", "링크 복구", "심볼릭 링크 만들기", "실행 프로그램에 연결", "연결 누락"),
        ("link models", "repair links", "create symlink", "link to runner", "missing runner link"),
    ),
    _record(
        "autoremove",
        "omm autoremove",
        "원본 모델이 사라진 뒤 실행 프로그램에 남은 깨진 링크를 제거합니다.",
        "Remove broken runner links whose source model no longer exists.",
        (_I, _X),
        RiskLevel.DESTRUCTIVE,
        (),
        (
            "깨진 링크 정리",
            "깨진 심볼릭 링크",
            "죽은 심볼릭 링크",
            "고아 링크 제거",
            "남은 링크 삭제",
        ),
        ("remove broken links", "dead symlink cleanup", "remove orphan links", "stale link cleanup"),
    ),
    _record(
        "benchmark",
        "omm benchmark <MODEL>...",
        "설치된 모델의 재현 가능한 품질과 생성 속도를 측정합니다.",
        "Measure reproducible quality and decode speed for installed models.",
        (_I, _R, _N, _U),
        RiskLevel.EXTERNAL_UPLOAD,
        ("model",),
        ("모델 벤치마크", "생성 속도 측정", "성능 측정", "품질 측정", "토큰 속도"),
        ("benchmark a model", "measure generation speed", "performance test", "quality test", "tokens per second"),
    ),
    _record(
        "update",
        "omm update",
        "OMM 자체를 선택한 채널의 최신 소스로 다시 설치하고 데이터를 갱신합니다.",
        "Reinstall OMM from the selected channel and refresh its data.",
        (_I, _N, _D, _W),
        RiskLevel.CHANGES_STATE,
        (),
        ("omm 업데이트", "omm 최신 버전", "프로그램 업데이트", "omm 자체 업그레이드"),
        ("update omm", "latest omm version", "update the program", "upgrade omm itself"),
    ),
    _record(
        "setting",
        "omm setting [SUBCOMMAND]",
        "텔레메트리, 업로드, 메모리 보호, 버전, 테마와 신뢰 설정을 보거나 바꿉니다.",
        "View or change telemetry, upload, memory, version, theme, and trust settings.",
        (_I, _S),
        RiskLevel.CHANGES_STATE,
        (),
        ("omm 설정", "설정 바꾸기", "텔레메트리 설정", "업로드 정책", "테마 바꾸기", "메모리 보호", "버전 채널"),
        ("omm settings", "change settings", "telemetry settings", "upload policy", "change theme", "memory guard", "version channel"),
    ),
    _record(
        "engine",
        "omm engine install [ENGINE]",
        "Ollama, LM Studio 같은 로컬 AI 실행 프로그램을 설치합니다.",
        "Install a local AI runner such as Ollama or LM Studio.",
        (_I, _N, _D, _W),
        RiskLevel.CHANGES_STATE,
        (),
        ("실행 프로그램 설치", "ollama 설치", "lm studio 설치", "엔진 설치", "러너 설치"),
        ("install a runner", "install ollama", "install lm studio", "install engine", "local ai runner"),
    ),
    _record(
        "ask",
        "omm ask <QUESTION>",
        "자연어 질문을 OMM 공식 명령어와 쉬운 설명으로 연결합니다.",
        "Map a natural-language question to trusted OMM commands and explanations.",
        (_I, _R),
        RiskLevel.CHANGES_STATE,
        ("question",),
        ("ai에게 질문", "자연어로 물어보기", "명령어 추천받기", "omm 도우미", "ask 사용"),
        ("ask ai", "natural language help", "recommend a command", "omm assistant", "use ask"),
    ),
)


COMMAND_KNOWLEDGE: Mapping[str, CommandKnowledge] = MappingProxyType(
    {record.command_id: record for record in _RECORDS}
)
COMMAND_IDS = frozenset(COMMAND_KNOWLEDGE)

if len(COMMAND_KNOWLEDGE) != len(_RECORDS):  # pragma: no cover - import-time invariant
    raise RuntimeError("duplicate assistant command_id")


_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}\b", re.IGNORECASE),
    re.compile(
        r"\b(?:api[_ -]?key|access[_ -]?token|password|secret)\s*[:=]\s*"
        r"['\"]?[A-Za-z0-9._~+/=-]{12,}",
        re.IGNORECASE,
    ),
)


def normalize_question(question: str) -> str:
    """Normalize user text while enforcing the prompt-size/control contract."""

    if not isinstance(question, str):
        raise QuestionSafetyError("invalid_type", "question must be text")
    normalized = unicodedata.normalize("NFKC", question)
    normalized = "".join(
        " " if char.isspace() else char
        for char in normalized
        if unicodedata.category(char) != "Cc" or char in "\t\n\r"
    )
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized:
        raise QuestionSafetyError("empty", "question must not be empty")
    if len(normalized) > MAX_QUESTION_LENGTH:
        raise QuestionSafetyError(
            "too_long", f"question must be {MAX_QUESTION_LENGTH} characters or fewer"
        )
    return normalized


def detect_secret(question: str) -> bool:
    """Return True for credential-shaped values, not mere security terms."""

    return any(pattern.search(question) is not None for pattern in _SECRET_PATTERNS)


def sanitize_question(question: str) -> str:
    """Return model-safe normalized text or refuse likely credentials.

    Refusal is intentional: masking risks incomplete redaction and is needless
    for command routing.  Callers should explain that the user can retry after
    removing the credential-like value.
    """

    normalized = normalize_question(question)
    if detect_secret(normalized):
        raise QuestionSafetyError(
            "secret_detected",
            "question looks like it contains a credential; remove it before asking",
        )
    return normalized


_WORD_RE = re.compile(r"[0-9a-zA-Z가-힣]+")

_KNOWN_MODEL_TARGETS = (
    ("anthropic", "anthropic"),
    ("앤트로픽", "anthropic"),
    ("엔트로픽", "anthropic"),
    ("claude", "anthropic"),
    ("클로드", "anthropic"),
    ("open ai", "openai"),
    ("openai", "openai"),
    ("오픈 ai", "openai"),
    ("오픈에이아이", "openai"),
    ("deep seek", "deepseek"),
    ("deepseek", "deepseek"),
    ("tiny llama", "tinyllama"),
    ("tinyllama", "tinyllama"),
    ("code llama", "codellama"),
    ("codellama", "codellama"),
    ("qwen", "qwen"),
    ("큐웬", "qwen"),
    ("llama", "llama"),
    ("라마", "llama"),
    ("code gemma", "codegemma"),
    ("codegemma", "codegemma"),
    ("gemma", "gemma"),
    ("젬마", "gemma"),
    ("mistral", "mistral"),
    ("미스트랄", "mistral"),
    ("exaone", "exaone"),
    ("엑사원", "exaone"),
    ("mixtral", "mixtral"),
    ("믹스트랄", "mixtral"),
    ("star coder", "starcoder"),
    ("starcoder", "starcoder"),
    ("스타코더", "starcoder"),
    ("codestral", "codestral"),
    ("코드스트랄", "codestral"),
    ("command r", "command-r"),
    ("커맨드 r", "command-r"),
    ("falcon", "falcon"),
    ("팔콘", "falcon"),
    ("solar", "solar"),
    ("솔라", "solar"),
    ("yi", "yi"),
    ("phi", "phi"),
    ("파이", "phi"),
    ("gpt oss", "gpt-oss"),
    ("gpt-oss", "gpt-oss"),
    ("gpt", "gpt"),
)


# Deliberately small typo map.  These are common command-intent typos, not a
# general spell checker: broad fuzzy matching made short terminal questions
# route to destructive commands too easily.  Exact token substitutions keep
# the work bounded and deterministic.
_TOKEN_CORRECTIONS = MappingProxyType(
    {
        "모댈": "모델",
        "모델를": "모델을",
        "추쳔": "추천",
        "설지": "설치",
        "삭재": "삭제",
        "검섹": "검색",
        "업대이트": "업데이트",
        "벤치마크크": "벤치마크",
        "serach": "search",
        "seach": "search",
        "instal": "install",
        "donwload": "download",
        "dowload": "download",
        "recomend": "recommend",
        "recommed": "recommend",
        "unistall": "uninstall",
        "verfy": "verify",
    }
)
_TEXT_CORRECTIONS = MappingProxyType(
    {
        "모댈": "모델",
        "추쳔": "추천",
        "설지": "설치",
        "삭재": "삭제",
        "검섹": "검색",
    }
)


def _search_form(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    for typo, correction in _TEXT_CORRECTIONS.items():
        normalized = normalized.replace(typo, correction)
    tokens = _WORD_RE.findall(normalized)
    return " ".join(_TOKEN_CORRECTIONS.get(token, token) for token in tokens)


def extract_known_model_target(question: str) -> str | None:
    """Return a safe canonical search term for a named model/provider.

    Only maintained aliases are returned, so callers may put this value in a
    displayed command without trusting arbitrary user or model text.
    """

    searchable = _search_form(question)
    for alias, canonical in _KNOWN_MODEL_TARGETS:
        normalized_alias = _search_form(alias)
        if re.search(
            rf"(?:^| ){re.escape(normalized_alias)}"
            rf"(?:은|는|이|가|을|를|의|로|으로|도)?(?: |$)",
            searchable,
        ):
            return canonical
    return None


def _compact(text: str) -> str:
    return text.replace(" ", "")


def _has_phrase(question: str, phrase: str) -> bool:
    """Match a maintained marker across natural Korean spacing variants.

    English one-word markers keep token boundaries (so ``run`` does not match
    ``runtime``).  Hangul and multi-word markers additionally use a compact
    form, which covers inputs such as ``모델목록`` and ``다운 로드`` without
    accepting arbitrary fuzzy text.
    """

    needle = _search_form(phrase)
    if not needle:
        return False
    if " " in needle:
        if needle in question:
            return True
        # Compact matching is for Korean spacing variation only. Applying it
        # to English made `show models` spuriously match `show model size`.
        return bool(re.search(r"[가-힣]", needle)) and _compact(needle) in _compact(question)
    if re.fullmatch(r"[a-z0-9]+", needle):
        return re.search(rf"(?:^| ){re.escape(needle)}(?: |$)", question) is not None
    return needle in question or needle in _compact(question)


def _has_any(question: str, phrases: Iterable[str]) -> bool:
    return any(_has_phrase(question, phrase) for phrase in phrases)


_EXPLICIT_MARKERS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "search": (
            "검색해",
            "검색하고",
            "검색할",
            "찾아줘",
            "찾고 싶",
            "찾을래",
            "찾아서",
            "search",
            "find",
            "look for",
        ),
        "install": (
            "설치해",
            "설치하고",
            "설치할",
            "깔아줘",
            "깔고 싶",
            "받아줘",
            "받고 싶",
            "받아도 돼",
            "내려받",
            "다운로드",
            "install",
            "download",
        ),
        "run": (
            "대화하고",
            "대화 시작",
            "채팅하고",
            "채팅 시작",
            "말 걸",
            "모델 켜",
            "말해보",
            "써보고",
            "실행해줘",
            "실행하고 싶",
            "chat with",
            "talk to",
            "start chat",
            "run model",
            "run the model",
        ),
        "verify": (
            "진짜 답",
            "실제로 답",
            "답하는지 테스트",
            "생성되는지",
            "생성 확인",
            "작동하는지 확인",
            "모델 검증",
            "응답이 나오",
            "답이 나오",
            "작동 시험",
            "verify",
            "actually generate",
            "generation test",
            "check model works",
        ),
        "doctor": (
            "오류 진단",
            "에러 진단",
            "문제 진단",
            "왜 안",
            "안 됨",
            "안돼",
            "안 돼",
            "연결 안",
            "고장",
            "먹통",
            "연결 실패",
            "troubleshoot",
            "diagnose",
            "why does",
            "why is",
            "broken",
            "fail",
            "error",
        ),
        "list": (
            "목록",
            "뭐 있어",
            "뭐가 설치",
            "보유 모델",
            "받아 둔 ai",
            "설치한 거 보여",
            "list models",
            "show models",
            "show installed",
            "what is installed",
            "what models do i have",
        ),
        "uninstall": (
            "삭제해",
            "삭제하고",
            "삭제할",
            "제거해",
            "제거하고",
            "지워줘",
            "지우고",
            "없애줘",
            "없애고",
            "uninstall",
            "delete model",
            "delete qwen",
            "delete it",
            "remove model",
            "erase model",
        ),
        "cleanup": (
            "다운로드 찌꺼기",
            "다운로드하다 만",
            "부분 다운로드",
            "불완전한 다운로드",
            "설치 캐시",
            "임시 다운로드",
            "다운받다 멈",
            "멈춘 다운로드",
            "partial download",
            "incomplete download",
            "download leftovers",
            "install cache",
        ),
        "contribute": (
            "추천 데이터에 기여",
            "데이터 기여",
            "데이터를 기여",
            "기여하고",
            "기여해",
            "추천 정확도 개선에 참여",
            "프로젝트에 참여",
            "측정값 보내",
            "벤치마크 기여",
            "텔레메트리 업로드",
            "contribute data",
            "contribute benchmark",
            "upload telemetry",
            "improve recommendation data",
        ),
        "setup": (
            "초기 설정",
            "처음 설정",
            "설정 마법사",
            "온보딩",
            "셋업",
            "initial setup",
            "first time setup",
            "setup wizard",
            "onboarding",
        ),
        "scan": (
            "컴퓨터 스펙",
            "pc 스펙",
            "맥 스펙",
            "컴퓨터 사양",
            "하드웨어 확인",
            "하드웨어 사양",
            "gpu 뭐",
            "그래픽카드 뭐",
            "ram 얼마",
            "시스템 정보",
            "램과 gpu",
            "ram and gpu",
            "hardware specs",
            "system specs",
            "scan hardware",
        ),
        "tune": (
            "컨텍스트 길이",
            "gpu 오프로딩",
            "스레드 추천",
            "배치 크기",
            "실행 설정 추천",
            "튜닝",
            "파라미터 최적화",
            "실행 최적화",
            "context length",
            "gpu offload",
            "thread count",
            "batch size",
            "tune model",
        ),
        "fit": (
            "램에서 돌아",
            "메모리에서 돌아",
            "내 사양에서 돌아",
            "실행 가능해",
            "돌릴 수",
            "올릴 수",
            "구동 가능",
            "감당할 수",
            "메모리에 맞",
            "does it fit",
            "will it fit",
            "can my pc run",
            "can my mac run",
            "fit in ram",
            "fit in memory",
        ),
        "help": (
            "사용법 알려",
            "사용법 보여",
            "도움말",
            "옵션 알려",
            "명령 설명",
            "command help",
            "show usage",
            "show options",
            "how do i use",
            "how to use omm",
        ),
        "import": (
            "gguf 가져",
            "gguf를 가져",
            "gguf 가져오",
            "gguf 옮",
            "기존 모델 가져",
            "외부 모델 등록",
            "gguf 등록",
            "다른 앱 모델",
            "import gguf",
            "import existing",
            "adopt model",
            "register external",
        ),
        "info": (
            "모델 용량",
            "모델 크기",
            "모델 버전",
            "모델 상세",
            "이 모델 정보",
            "파일 크기",
            "용량 얼마",
            "정보 알려",
            "몇 기가",
            "model size",
            "model version",
            "model details",
            "model information",
            "information for this model",
        ),
        "upgrade": (
            "모델 업데이트",
            "모델 업그레이드",
            "모델 최신 버전",
            "모델 소스 새로고침",
            "업그레이드",
            "모델 갱신",
            "qwen 갱신",
            "update model",
            "upgrade model",
            "upgrade",
            "refresh model",
            "latest model version",
        ),
        "link": (
            "링크 복구",
            "링크 만들",
            "모델 연결 복구",
            "실행 프로그램 연결 만들",
            "연결 다시",
            "연결 고쳐",
            "심볼릭 링크",
            "link models",
            "repair links",
            "create symlink",
            "missing runner link",
        ),
        "autoremove": (
            "깨진 링크",
            "깨진 심볼릭 링크",
            "죽은 링크",
            "죽은 심볼릭 링크",
            "고아 링크",
            "남은 링크 삭제",
            "원본 없는 연결",
            "원본 없는 링크",
            "broken links",
            "dead symlink",
            "orphan links",
            "stale links",
        ),
        "benchmark": (
            "속도 재",
            "속도 측정",
            "성능 측정",
            "토큰 속도",
            "초당 토큰",
            "속도 얼마",
            "벤치마크",
            "benchmark",
            "measure speed",
            "tokens per second",
            "performance test",
        ),
        "update": (
            "omm 업데이트",
            "omm 업그레이드",
            "omm 최신",
            "프로그램 업데이트",
            "omm 새 버전",
            "update omm",
            "upgrade omm",
            "latest omm",
            "update the program",
        ),
        "setting": (
            "텔레메트리 끄",
            "텔레메트리 켜",
            "업로드 정책",
            "테마 바꾸",
            "다크 테마",
            "라이트 테마",
            "메모리 보호",
            "채널 바꾸",
            "omm 설정",
            "telemetry off",
            "disable telemetry",
            "upload policy",
            "change theme",
            "memory guard",
            "version channel",
        ),
        "engine": (
            "ollama 설치",
            "lm studio 설치",
            "러너 설치",
            "엔진 설치",
            "엔진 깔",
            "install ollama",
            "install lm studio",
            "install runner",
            "install a local runner",
            "install engine",
            "koboldcpp 설치",
            "jan 설치",
            "textgenwebui 설치",
            "anythingllm 설치",
            "msty studio 설치",
            "install koboldcpp",
            "install jan",
        ),
        "ask": (
            "자연어로 물어",
            "ai에게 질문",
            "omm 도우미",
            "ask 사용",
            "말로 명령",
            "문장으로 명령",
            "뭘 쓸지 알려",
            "ask ai",
            "omm assistant",
            "natural language help",
        ),
    }
)


_DISCOVERY_MARKERS = (
    "추천",
    "찾",
    "검색",
    "뭐가 좋",
    "어떤 게 좋",
    "recommend",
    "recommendation",
    "suggest",
    "find",
    "search",
    "best",
    "which one",
)
_RECOMMEND_MARKERS = (
    "추천",
    "뭐 깔",
    "무엇을 깔",
    "어떤 모델",
    "뭐가 좋",
    "골라줘",
    "골라 줘",
    "알맞은 모델",
    "recommend",
    "suggest",
    "which model",
    "best model",
)
_HARDWARE_MARKERS = (
    "내 맥",
    "내맥",
    "my mac",
    "내 컴퓨터",
    "내컴퓨터",
    "my computer",
    "내 pc",
    "my pc",
    "사양",
    "성능에 맞",
    "스펙",
    "hardware",
    "windows",
    "윈도우",
    "linux",
    "리눅스",
)
_USE_CASE_MARKERS = (
    "코딩",
    "개발",
    "번역",
    "요약",
    "글쓰기",
    "수학",
    "추론",
    "coding",
    "programming",
    "translation",
    "summarization",
    "writing",
    "math",
    "reasoning",
)
_SPECIFIC_MODEL_MARKERS = (
    "이 모델",
    "해당 모델",
    "this model",
    "that model",
    "7b",
    "8b",
    "13b",
    "14b",
    "32b",
    "70b",
)
_QUESTION_EXPLANATION_MARKERS = (
    "뭐야",
    "무슨 뜻",
    "설명해",
    "사용법",
    "옵션",
    "what is",
    "what does",
    "explain",
    "usage",
    "options",
)
_SMALLTALK_EXACT = frozenset(
    {
        "안녕",
        "안녕하세요",
        "하이",
        "헬로",
        "고마워",
        "고맙습니다",
        "감사",
        "감사합니다",
        "hi",
        "hello",
        "hey",
        "thanks",
        "thank you",
    }
)
_PRODUCT_QUESTION_MARKERS = (
    "omm이 뭐",
    "omm은 뭐",
    "omm 뭐하는",
    "omm 기능",
    "what is omm",
    "what does omm do",
    "what can omm do",
)


def _explicit_marker_hits(question: str) -> dict[str, tuple[str, ...]]:
    hits: dict[str, tuple[str, ...]] = {}
    for command_id, markers in _EXPLICIT_MARKERS.items():
        matched = tuple(marker for marker in markers if _has_phrase(question, marker))
        if matched:
            hits[command_id] = matched
    return hits


def _mentioned_command_explanation(question: str) -> bool:
    if not _has_any(question, _QUESTION_EXPLANATION_MARKERS):
        return False
    if _has_phrase(question, "omm help"):
        return True
    for command_id in COMMAND_IDS:
        # Korean particles commonly attach to the ASCII command name
        # (``install이 뭐야``).  Keep the suffix allowlist narrow so a command
        # ID embedded in an unrelated word is not treated as documentation.
        if re.search(
            rf"(?:^| ){re.escape(command_id)}(?:가|이|은|는|을|를)?(?: |$)",
            question,
        ):
            return True
    return False


def _intent_score(question: str, intent: str) -> float:
    needle = _search_form(intent)
    if not needle:
        return 0.0
    question_tokens = set(question.split())
    intent_tokens = needle.split()
    if needle == question:
        return 12.0
    if _has_phrase(question, needle):
        # Longer phrases encode much more intent than generic single words.
        return 3.0 + min(len(needle), 30) / 6.0 + max(0, len(intent_tokens) - 1)
    if all(token in question_tokens for token in intent_tokens):
        return 2.0 + len(intent_tokens)
    overlap = len(question_tokens.intersection(intent_tokens))
    if len(intent_tokens) >= 3 and overlap >= 2 and overlap == len(intent_tokens) - 1:
        return 0.7 * overlap
    return 0.0


def rank_command_candidates(question: str, *, limit: int = 3) -> FallbackResult:
    """Rank trusted commands without invoking an LLM or executing anything."""

    if not 1 <= limit <= MAX_CANDIDATES:
        raise ValueError(f"limit must be between 1 and {MAX_CANDIDATES}")
    normalized = sanitize_question(question)
    search_question = _search_form(normalized)
    if search_question in _SMALLTALK_EXACT:
        return FallbackResult(
            normalized_question=normalized,
            candidates=(),
            confidence=0.0,
            clarify=True,
            reason="smalltalk",
        )
    if _has_any(search_question, _PRODUCT_QUESTION_MARKERS):
        return FallbackResult(
            normalized_question=normalized,
            candidates=(),
            confidence=0.0,
            clarify=True,
            reason="product_question",
        )

    named_target = extract_known_model_target(normalized)
    named_target_request = named_target is not None and any(
        _has_phrase(search_question, marker) for marker in _DISCOVERY_MARKERS
    )
    explicit_hits = _explicit_marker_hits(search_question)

    # Resolve phrases where a broad action word is part of a more specific
    # operation.  E.g. "install Ollama" installs a runner, while "install
    # Qwen in Ollama" installs a model; "benchmark contribution" is the
    # consented contribution flow, not a one-off benchmark.
    explicit_ids = set(explicit_hits)
    if _has_phrase(search_question, "omm") and _has_any(
        search_question,
        ("업데이트", "업그레이드", "최신", "새 버전", "update", "upgrade", "latest"),
    ):
        explicit_ids.add("update")
        explicit_hits.setdefault("update", ("omm-self-update",))
    if "engine" in explicit_ids and "install" in explicit_ids and named_target is None:
        explicit_ids.discard("install")
    if "ask" in explicit_ids and "search" in explicit_ids and _has_any(
        search_question, ("명령", "command")
    ):
        explicit_ids.discard("search")
    if "contribute" in explicit_ids and "benchmark" in explicit_ids:
        explicit_ids.discard("benchmark")
    if "cleanup" in explicit_ids:
        explicit_ids.discard("uninstall")
        explicit_ids.discard("install")
    if "autoremove" in explicit_ids:
        explicit_ids.discard("link")
        explicit_ids.discard("uninstall")
    if "autoremove" in explicit_ids or "link" in explicit_ids:
        explicit_ids.discard("doctor")
    if "benchmark" in explicit_ids:
        explicit_ids.discard("verify")
    if "update" in explicit_ids and _has_phrase(search_question, "omm"):
        explicit_ids.discard("upgrade")
    elif "upgrade" in explicit_ids:
        explicit_ids.discard("info")
        explicit_ids.discard("update")

    rule_scores: dict[str, tuple[float, list[str]]] = {}

    def add_rule(command_id: str, score: float, marker: str) -> None:
        current_score, markers = rule_scores.get(command_id, (0.0, []))
        rule_scores[command_id] = (max(current_score, score), [*markers, marker])

    for command_id in explicit_ids:
        markers = explicit_hits[command_id]
        add_rule(
            command_id,
            24.0 + min(4.0, max(len(_search_form(marker)) for marker in markers) / 8.0),
            f"explicit:{markers[0]}",
        )

    help_explanation = _mentioned_command_explanation(search_question)
    if help_explanation:
        add_rule("help", 38.0, "command-explanation")

    has_recommend_request = _has_any(search_question, _RECOMMEND_MARKERS)
    has_hardware_context = _has_any(search_question, _HARDWARE_MARKERS)
    has_use_case = _has_any(search_question, _USE_CASE_MARKERS)
    has_model_context = _has_any(search_question, ("모델", "ai", "llm", "model"))
    has_specific_model = named_target is not None or _has_any(
        search_question, _SPECIFIC_MODEL_MARKERS
    )

    if named_target_request or (
        named_target is not None and has_use_case and "install" not in explicit_ids
    ):
        # A family/provider is a catalogue-search constraint. `recommend`
        # evaluates hardware but cannot accept "qwen"/"anthropic" arguments.
        add_rule("search", 36.0, f"named-target:{named_target}")
    if (
        has_recommend_request
        and named_target is None
        and (has_model_context or has_hardware_context or has_use_case)
        and "contribute" not in explicit_ids
    ):
        score = 35.0 if has_hardware_context else 31.0
        if has_use_case:
            score += 1.0
        add_rule("recommend", score, "model-recommendation")

    fit_language = _has_any(search_question, _EXPLICIT_MARKERS["fit"])
    if fit_language and has_specific_model:
        add_rule("fit", 37.0, "specific-model-fit")
    if fit_language and has_recommend_request and has_hardware_context and not has_specific_model:
        # "Which model fits my Mac?" asks OMM to choose a model. `fit`
        # requires one already-selected model argument.
        add_rule("recommend", 39.0, "which-model-fits-hardware")

    ranked: list[tuple[float, str, tuple[str, ...]]] = []
    for record in _RECORDS:
        matches: list[tuple[float, str]] = []
        for intent in (*record.intents_ko, *record.intents_en):
            score = _intent_score(search_question, intent)
            if score > 0:
                matches.append((score, intent))
        rule = rule_scores.get(record.command_id)
        if rule is not None:
            rule_score, rule_markers = rule
            matches.extend((rule_score, marker) for marker in rule_markers)
        if not matches:
            continue
        matches.sort(key=lambda item: (-item[0], item[1]))
        # A second independent phrase can reinforce a result without letting
        # a record with many generic keywords dominate the list.
        total = matches[0][0] + sum(score * 0.15 for score, _ in matches[1:3])
        ranked.append((total, record.command_id, tuple(intent for _, intent in matches[:3])))

    ranked.sort(key=lambda item: (-item[0], item[1]))
    if not ranked:
        return FallbackResult(
            normalized_question=normalized,
            candidates=(),
            confidence=0.0,
            clarify=True,
            reason="no_intent_match",
        )

    top_score = ranked[0][0]
    second_score = ranked[1][0] if len(ranked) > 1 else 0.0
    margin = max(0.0, top_score - second_score)
    top_confidence = min(0.99, 0.35 + top_score / 20.0 + margin / 25.0)
    conflict_ids = {
        command_id
        for command_id in explicit_ids
        if command_id
        in {
            "search",
            "install",
            "run",
            "verify",
            "uninstall",
            "cleanup",
            "contribute",
            "setup",
            "scan",
            "tune",
            "fit",
            "import",
            "info",
            "upgrade",
            "link",
            "autoremove",
            "benchmark",
            "update",
            "setting",
            "engine",
        }
    }
    forced_conflict = len(conflict_ids) > 1 and not help_explanation
    ambiguous = forced_conflict or top_score < 3.5 or (
        second_score >= top_score * 0.88 and margin < 1.5
    )
    candidates = tuple(
        CommandCandidate(
            command_id=command_id,
            score=round(score, 3),
            confidence=round(min(0.99, score / max(top_score, 1.0) * top_confidence), 3),
            matched_intents=matched,
        )
        for score, command_id, matched in ranked[:limit]
    )
    return FallbackResult(
        normalized_question=normalized,
        candidates=candidates,
        confidence=round(top_confidence, 3),
        clarify=ambiguous,
        reason=("conflicting_intents" if forced_conflict else "ambiguous")
        if ambiguous
        else "matched",
    )


def render_command(command_id: str) -> str:
    """Render only the maintained template; never interpolate model output."""

    try:
        return COMMAND_KNOWLEDGE[command_id].command_template
    except KeyError as exc:
        raise ValueError(f"unknown command_id: {command_id}") from exc


def build_candidate_context(
    candidates: Iterable[str | CommandCandidate], *, locale: str = "ko"
) -> str:
    """Return compact JSON that can be embedded in a constrained AI prompt."""

    if locale not in {"ko", "en"}:
        raise ValueError("locale must be 'ko' or 'en'")
    command_ids = [
        candidate.command_id if isinstance(candidate, CommandCandidate) else candidate
        for candidate in candidates
    ]
    if not command_ids:
        raise ValueError("at least one candidate is required")
    if len(command_ids) > MAX_CANDIDATES:
        raise ValueError(f"at most {MAX_CANDIDATES} candidates are allowed")
    if len(set(command_ids)) != len(command_ids):
        raise ValueError("candidate command_ids must be unique")
    unknown = [command_id for command_id in command_ids if command_id not in COMMAND_KNOWLEDGE]
    if unknown:
        raise ValueError(f"unknown command_id: {unknown[0]}")

    payload = []
    for command_id in command_ids:
        record = COMMAND_KNOWLEDGE[command_id]
        payload.append(
            {
                "commandId": record.command_id,
                "command": record.command_template,
                "summary": record.summary_ko if locale == "ko" else record.summary_en,
                "risk": record.risk.value,
                "effects": [effect.value for effect in record.side_effects],
                "requiredArguments": list(record.required_arguments),
            }
        )
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def validate_command_choice(command_id: str, allowed_command_ids: Iterable[str]) -> bool:
    """Validate an AI-selected ID against both knowledge and offered choices."""

    allowed = tuple(allowed_command_ids)
    return command_id in COMMAND_KNOWLEDGE and command_id in allowed


__all__ = [
    "COMMAND_IDS",
    "COMMAND_KNOWLEDGE",
    "MAX_CANDIDATES",
    "MAX_QUESTION_LENGTH",
    "CommandCandidate",
    "CommandKnowledge",
    "FallbackResult",
    "QuestionSafetyError",
    "RiskLevel",
    "SideEffect",
    "build_candidate_context",
    "detect_secret",
    "extract_known_model_target",
    "normalize_question",
    "rank_command_candidates",
    "render_command",
    "sanitize_question",
    "validate_command_choice",
]
