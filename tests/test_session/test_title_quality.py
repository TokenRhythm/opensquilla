"""Bounded refusal recognition without suppressing ordinary session topics."""

import pytest

from opensquilla.session.naming import _sanitize_title
from opensquilla.session.title_quality import is_refusal_title


@pytest.mark.parametrize(
    "value",
    [
        "I cannot generate a title for this request",
        "I cannot assist with that request",
        "I'm sorry, but I can't help with that request.",
        "I'm unable to provide assistance with this request.",
        "抱歉，我无法协助处理该请求。",
        "我无法协助",
        "**Title: ‘I CANNOT HELP WITH THIS REQUEST.’**",
        '\"I cannot assist with that request\".',
        "**I cannot assist with that request**.",
        "「抱歉，我无法协助处理该请求」。",
        "“I’m sorry.\nI can’t assist with that request.”",
        "```\nSorry,\nI am unable to provide assistance with your request.\n```",
        "「抱歉。\n我无法协助处理该请求。」",
        "I cannot assist with that request because it is outside my scope.",
        "I cannot help with this request. Please ask about another topic.",
        "I cannot assist with that request\nPlease ask about another topic.",
        "I'm sorry.\nI cannot assist with that request\nPlease ask about another topic.",
        "抱歉，我无法协助处理该请求\n请换一个话题。",
        "抱歉。\n我无法协助处理该请求\n请换一个话题。",
        "I'm unable to provide assistance with this reque",
    ],
)
def test_refusal_title(value):
    assert is_refusal_title(value)


@pytest.mark.parametrize(
    "value",
    [
        "I cannot log in",
        "Analyze refusal responses",
        "Fix unable to provide assistance detection",
        "I cannot assist with that request handler",
        "I cannot help with Python imports",
        "I'm unable to provide assistance with this",
        "I'm unable to provide assistance with this requ",
        "I'm unable to provide assistance with this reques",
        "Sorry page routing",
        "研究模型拒答行为",
        "排查无法登录的问题",
        "分析无法协助提示",
        "Refactor HTTP request handling",
        "New topic\nI cannot assist with that request",
        None,
        "",
        42,
        ["I cannot assist with that request"],
    ],
)
def test_ordinary_topics_and_unknown_short_prefixes_are_not_refusals(value):
    assert not is_refusal_title(value)


@pytest.mark.parametrize("max_chars", [8, 34, 48, 100, 0])
@pytest.mark.parametrize(
    "value",
    [
        "I'm unable to provide assistance with this request.",
        "I'm sorry.\nI cannot generate a title for this request.",
        "抱歉，我无法协助处理该请求。",
        '\"I cannot assist with that request\".',
        "**I cannot assist with that request**.",
        "「抱歉，我无法协助处理该请求」。",
        "I'm sorry.\nI cannot assist with that request\nPlease ask about another topic.",
        "抱歉，我无法协助处理该请求\n请换一个话题。",
    ],
)
def test_sanitize_checks_refusal_before_first_line_and_truncation(value, max_chars):
    assert _sanitize_title(value, max_chars) is None


@pytest.mark.parametrize("value", [None, 123, [], {"text": "Example title"}])
def test_sanitize_ignores_non_string_content(value):
    assert _sanitize_title(value, 48) is None


@pytest.mark.parametrize(
    "value",
    ["I cannot log in", "Analyze refusal responses", "排查无法登录的问题"],
)
def test_sanitize_keeps_valid_negative_topics(value):
    assert _sanitize_title(value, 48) == value
