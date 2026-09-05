import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_pr_description.py"
_spec = importlib.util.spec_from_file_location("check_pr_description", _SCRIPT)
check = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = check
_spec.loader.exec_module(check)

GOOD_BODY = """## 한줄 요약
npm으로 설치한 omm이 임시 폴더의 git 저장소를 자기 소스로 착각하던 문제를 고칩니다.

## 배경
오늘 npm 배포 경로 코드리뷰에서 발견했습니다. 임시 폴더에 .git이 있으면 업데이트가 엉뚱한 폴더를 건드립니다.

## 무엇을 바꿨나
- 실행 파일이 압축 해제된 임시 폴더는 소스 체크아웃으로 보지 않습니다.
- npm 설치 여부를 먼저 확인합니다.

## 어떻게 확인했나
```
pytest tests/test_package_metadata.py -> 41 passed
```
그리고 실제 빌드한 omm.exe로 재현 후 수정 확인.

## Technical notes
`_package_checkout()` now returns None when `sys.frozen` is set.
"""


def test_good_body_passes():
    verdict = check.evaluate(GOOD_BODY, author="Matwaetle", head_ref="fix/npm-install-source")
    assert verdict.ok and not verdict.exempt
    assert "감사합니다" in verdict.message


def test_missing_heading_is_named():
    body = GOOD_BODY.replace("## 배경\n", "## Background\n")
    verdict = check.evaluate(body, author="minigu5", head_ref="feat/x")
    assert not verdict.ok
    assert "`## 배경` 제목이 없습니다." in verdict.problems


def test_english_only_body_fails_on_hangul_count():
    body = """## 한줄 요약
Fix the install-source detection for frozen builds.

## 배경
Found during the npm path review; a .git in TEMP shadowed the npm install.

## 무엇을 바꿨나
- Skip checkout detection when frozen.

## 어떻게 확인했나
pytest passed.
"""
    verdict = check.evaluate(body, author="fakeminjun7321", head_ref="fix/y")
    assert not verdict.ok
    assert any("한국어가 너무 적습니다" in p for p in verdict.problems)
    assert "붙여넣어 채울 틀" in verdict.message


def test_template_comments_do_not_count_as_content():
    body = """## 한줄 요약
<!-- 이 PR이 무엇을 하는지 한 문장으로. 개발자가 아닌 팀원도 이해할 수 있게. -->

## 배경
<!-- 왜 이 변경이 나왔나요? -->

## 무엇을 바꿨나
<!-- 바꾼 내용을 쉬운 말로. -->

## 어떻게 확인했나
<!-- 실행한 명령과 결과 -->
"""
    verdict = check.evaluate(body, author="Matwaetle", head_ref="feat/z")
    assert not verdict.ok
    assert sum("비어 있습니다" in p for p in verdict.problems) == 4


def test_wrong_order_is_reported():
    body = (
        GOOD_BODY.replace("## 배경", "## 배경-tmp")
        .replace("## 무엇을 바꿨나", "## 배경")
        .replace("## 배경-tmp", "## 무엇을 바꿨나")
    )
    verdict = check.evaluate(body, author="Matwaetle", head_ref="feat/order")
    assert not verdict.ok
    assert any("순서가 다릅니다" in p for p in verdict.problems)


def test_heading_with_extra_words_is_accepted():
    body = GOOD_BODY.replace("## 배경\n", "## 배경 (context)\n")
    assert check.evaluate(body, author="Matwaetle", head_ref="feat/x").ok


def test_headings_inside_code_fences_are_ignored():
    body = GOOD_BODY + "\n```markdown\n## 배경\nfake\n```\n"
    assert check.evaluate(body, author="Matwaetle", head_ref="feat/x").ok


@pytest.mark.parametrize(
    "author, head_ref",
    [
        ("github-actions[bot]", "retrain/20260903-074624"),
        ("omm-retrain-bot", "retrain/20260903-074624"),
        ("minigu5", "retrain/20260903-074624"),
        ("minigu5", "beta"),
    ],
)
def test_bot_and_sync_prs_are_exempt(author, head_ref):
    verdict = check.evaluate("", author=author, head_ref=head_ref)
    assert verdict.ok and verdict.exempt


def test_main_reads_environment_and_writes_summary(tmp_path, monkeypatch, capsys):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("PR_BODY", GOOD_BODY)
    monkeypatch.setenv("PR_AUTHOR", "Matwaetle")
    monkeypatch.setenv("PR_HEAD_REF", "feat/env")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert check.main() == 0
    assert "감사합니다" in capsys.readouterr().out
    assert summary.read_text(encoding="utf-8").startswith("## PR 설명 확인")

    monkeypatch.setenv("PR_BODY", "no headings at all")
    assert check.main() == 1
