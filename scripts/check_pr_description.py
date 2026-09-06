#!/usr/bin/env python3
"""Check that a pull-request description explains the change in plain Korean.

Every contributor here develops with an AI agent, and an AI-written English PR
body tells a teammate who was not in that session nothing about where the
change came from. CONTRIBUTING.md therefore requires four Korean sections, in
this order, before any English detail:

    ## 한줄 요약   ## 배경   ## 무엇을 바꿨나   ## 어떻게 확인했나

This script is the CI side of that rule (`.github/workflows/pr-description-check.yml`).
It reads the PR body and author/branch from environment variables so the
workflow never interpolates untrusted text into a shell command.

Exit 0 = OK or exempt (bot PRs). Exit 1 = the description needs work; the
message says exactly what is missing, in Korean, with a skeleton to paste.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass

REQUIRED_HEADINGS = ["한줄 요약", "배경", "무엇을 바꿨나", "어떻게 확인했나"]
# Sections whose combined Korean text must reach MIN_HANGUL syllables. The
# verification section may legitimately be mostly commands, so it is excluded.
NARRATIVE_HEADINGS = ["한줄 요약", "배경", "무엇을 바꿨나"]
MIN_HANGUL = 40

# PRs created by automation carry no human-readable context to enforce.
BOT_LOGIN_SUFFIX = "[bot]"
BOT_LOGINS = {"omm-retrain-bot"}
# train.yml opens `retrain/<timestamp>` PRs; the beta -> main sync PR has head `beta`.
EXEMPT_HEAD_BRANCHES = {"beta"}
EXEMPT_HEAD_PREFIXES = ("retrain/",)

_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_HANGUL_RE = re.compile(r"[가-힣]")

SKELETON = """## 한줄 요약
이 PR이 무엇을 하는지 한 문장으로.

## 배경
왜 이 변경이 나왔는지: 어떤 이슈·버그·대화·리뷰에서 시작됐는지 (맥락). 이슈 번호가 있으면 함께.

## 무엇을 바꿨나
바꾼 내용을 쉬운 말로. 함수·파일 이름은 꼭 필요할 때만.

## 어떻게 확인했나
실행한 명령과 결과. 못 해본 경로는 "미검증"으로.
"""


@dataclass(frozen=True)
class Verdict:
    ok: bool
    exempt: bool
    problems: list[str]

    @property
    def message(self) -> str:
        if self.exempt:
            return "PR 설명 확인: 자동 생성 PR이라 검사하지 않습니다."
        if self.ok:
            return "PR 설명 확인: 한국어 설명 네 항목이 모두 있습니다. 감사합니다."
        lines = ["PR 설명 확인 실패. 이 PR의 설명을 아래처럼 고쳐 주세요.", ""]
        lines += [f"- {problem}" for problem in self.problems]
        lines += [
            "",
            "PR 본문은 이 세션에 없던 팀원도 이해할 수 있게 한국어로 씁니다. 영어 기술 세부는",
            "네 항목 아래에 덧붙여도 됩니다. 규칙: CONTRIBUTING.md, 틀: .github/PULL_REQUEST_TEMPLATE.md",
            "",
            "붙여넣어 채울 틀:",
            "",
            SKELETON.rstrip(),
        ]
        return "\n".join(lines)


def is_exempt(author: str, head_ref: str) -> bool:
    author = (author or "").strip()
    head_ref = (head_ref or "").strip()
    if author.endswith(BOT_LOGIN_SUFFIX) or author in BOT_LOGINS:
        return True
    if head_ref in EXEMPT_HEAD_BRANCHES or head_ref.startswith(EXEMPT_HEAD_PREFIXES):
        return True
    return False


def strip_comments(text: str) -> str:
    return _COMMENT_RE.sub("", text or "")


def split_sections(body: str) -> dict[str, str]:
    """Map each markdown heading (text after the hashes) to its section body."""
    sections: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []
    in_fence = False
    for line in strip_comments(body).splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        match = None if in_fence else _HEADING_RE.match(line)
        if match:
            if current is not None:
                sections[current] = "\n".join(buffer).strip()
            current = match.group(1).strip()
            buffer = []
        elif current is not None:
            buffer.append(line)
    if current is not None:
        sections[current] = "\n".join(buffer).strip()
    return sections


def _find_heading(sections: dict[str, str], wanted: str) -> str | None:
    """Accept `## 배경`, `## 배경 (context)`, `## 2. 배경`: the key must appear in the heading."""
    for heading in sections:
        if wanted in heading:
            return heading
    return None


def hangul_count(text: str) -> int:
    return len(_HANGUL_RE.findall(text or ""))


def evaluate(body: str, author: str = "", head_ref: str = "") -> Verdict:
    if is_exempt(author, head_ref):
        return Verdict(ok=True, exempt=True, problems=[])
    sections = split_sections(body)
    problems: list[str] = []
    found: dict[str, str] = {}
    for wanted in REQUIRED_HEADINGS:
        heading = _find_heading(sections, wanted)
        if heading is None:
            problems.append(f"`## {wanted}` 제목이 없습니다.")
            continue
        found[wanted] = heading
        if not sections[heading]:
            problems.append(f"`## {wanted}` 아래가 비어 있습니다. 내용을 적어 주세요.")
    order = [found[w] for w in REQUIRED_HEADINGS if w in found]
    positions = [list(sections).index(h) for h in order]
    if positions != sorted(positions):
        problems.append(
            "네 항목의 순서가 다릅니다: 한줄 요약 → 배경 → 무엇을 바꿨나 → 어떻게 확인했나."
        )
    narrative = " ".join(sections.get(found.get(w, ""), "") for w in NARRATIVE_HEADINGS)
    count = hangul_count(narrative)
    if count < MIN_HANGUL:
        problems.append(
            f"한줄 요약·배경·무엇을 바꿨나에 한국어가 너무 적습니다 (한글 {count}자, 최소 {MIN_HANGUL}자). "
            "영어를 번역하라는 뜻이 아니라, 이 변경이 왜 나왔는지 쉬운 한국어로 설명해 주세요."
        )
    return Verdict(ok=not problems, exempt=False, problems=problems)


def main() -> int:
    # Korean output must survive a non-UTF-8 console (Windows cp949) when run locally.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8", errors="replace")
    body = os.environ.get("PR_BODY", "")
    author = os.environ.get("PR_AUTHOR", "")
    head_ref = os.environ.get("PR_HEAD_REF", "")
    verdict = evaluate(body, author, head_ref)
    print(verdict.message)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write("## PR 설명 확인\n\n")
            handle.write(verdict.message.replace("\n", "\n\n", 1) + "\n")
    return 0 if verdict.ok else 1


if __name__ == "__main__":
    sys.exit(main())
