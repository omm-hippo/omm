"""Document search messages: Korean for the web UI, English under `omm rag`."""
from __future__ import annotations

import threading

import pytest
from typer.testing import CliRunner

from omm import cli, lang
from omm.documents import DocumentStore


def test_default_is_korean_and_use_resets():
    assert lang.t("한국어", "English") == "한국어"
    token = lang.use(lang.ENGLISH)
    assert lang.t("한국어", "English") == "English"
    lang.reset(token)
    assert lang.current() == lang.KOREAN


def test_new_threads_start_korean_even_when_the_caller_is_english():
    # Web request threads must not pick up the CLI's language.
    token = lang.use(lang.ENGLISH)
    seen = []
    try:
        thread = threading.Thread(target=lambda: seen.append(lang.current()))
        thread.start()
        thread.join()
    finally:
        lang.reset(token)
    assert seen == [lang.KOREAN]


@pytest.mark.parametrize("header, expected", [(None, "ko"), ("", "ko"), ("ko-KR,ko;q=0.9", "ko"),
                                              ("en", "en"), ("en-US,en;q=0.9", "en"), ("fr", "ko")])
def test_from_header(header, expected):
    assert lang.from_header(header) == expected


def test_store_errors_are_korean_outside_the_cli(isolated_omm_home):
    with pytest.raises(ValueError, match="컬렉션을 찾지 못했어요"):
        DocumentStore().search_detailed("질문", None, collection="없는컬렉션")


def test_rag_cli_prints_store_errors_in_english_and_restores_korean(isolated_omm_home):
    result = CliRunner().invoke(cli.app, ["rag", "search", "question", "--collection", "missing"])
    assert result.exit_code == 1
    output = " ".join(result.output.split())
    assert "Collection 'missing' was not found" in output
    assert not any("가" <= ch <= "힣" for ch in output)
    assert lang.current() == lang.KOREAN
