"""Text-format tool call detection and the budget-summary strip safety net."""

from __future__ import annotations

import pytest

from bot.agent import _looks_like_text_tool_call, _strip_text_tool_calls


LFM_CALL = "<|tool_call_start|>[fetch_url(url='https://x/y')]<|tool_call_end|>"


# --- _looks_like_text_tool_call ---------------------------------------------

@pytest.mark.parametrize("text", [
    LFM_CALL,
    f"Let me check that. {LFM_CALL}",
    "<tool_call>{\"name\": \"fetch_url\"}</tool_call>",
])
def test_detects_text_tool_calls(text):
    assert _looks_like_text_tool_call(text)


@pytest.mark.parametrize("text", [
    "",
    "Sure, the weather in Oslo is 4°C and raining.",
    "A tool_call is how models invoke functions | pipes | are | fine.",
    "Use <|im_start|> tokens? No idea what you mean.",
])
def test_ignores_ordinary_prose(text):
    assert not _looks_like_text_tool_call(text)


# --- _strip_text_tool_calls -------------------------------------------------

def test_strips_lfm_block_and_keeps_surrounding_text():
    text = f"Out of budget, here's what I found. {LFM_CALL} Ask again later."
    assert _strip_text_tool_calls(text) == (
        "Out of budget, here's what I found. "
        "[malformed tool call removed] Ask again later."
    )


def test_strips_multiline_lfm_block():
    text = (
        "Summary so far.\n"
        "<|tool_call_start|>[search(\n  query='x'\n)]<|tool_call_end|>"
    )
    assert _strip_text_tool_calls(text) == (
        "Summary so far.\n[malformed tool call removed]"
    )


def test_strips_hermes_block_still():
    text = 'Before <tool_call>{"name": "fetch_url"}</tool_call> after'
    assert _strip_text_tool_calls(text) == (
        "Before [malformed tool call removed] after"
    )


def test_strips_both_forms_in_one_reply():
    text = f'A <tool_call>{{"name": "x"}}</tool_call> B {LFM_CALL} C'
    assert _strip_text_tool_calls(text) == (
        "A [malformed tool call removed] B [malformed tool call removed] C"
    )


def test_strip_leaves_plain_text_alone():
    text = "Nothing to see | here | at all."
    assert _strip_text_tool_calls(text) == text
