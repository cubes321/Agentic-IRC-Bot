"""`!define`: parsing, reply cleanup, rate limit, and the one-shot LLM call."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from openai import APIConnectionError

from bot.agent import AgentCore
from bot.define import DefineLimiter, clean_reply, parse_word


# --- parse_word -------------------------------------------------------------

@pytest.mark.parametrize("cmd, expected", [
    ("!define oxymoron", "oxymoron"),
    ("!define   ice   cream  ", "ice cream"),
    ("!define rock'n'roll", "rock'n'roll"),
    ("!define self-aware", "self-aware"),
    ("!define café", "café"),
])
def test_parse_word_accepts(cmd, expected):
    assert parse_word(cmd) == expected


@pytest.mark.parametrize("cmd", [
    "!define",
    "!define    ",
    "!define " + "a" * 41,
    "!define one two three four",
    "!define <script>",
    "!define foo; rm -rf",
    "!define dave_",
    "!define a\x01b",
])
def test_parse_word_rejects(cmd):
    assert parse_word(cmd) is None


# --- clean_reply ------------------------------------------------------------

def test_clean_reply_takes_first_line():
    text = "\n\noxymoron (n.): a moron who breathes oxygen.\nHope that helps!"
    assert clean_reply("oxymoron", text) == "oxymoron (n.): a moron who breathes oxygen."


def test_clean_reply_strips_quotes_and_markdown():
    assert clean_reply("paradox", '**"paradox (n.): two doctors."**') == \
        "paradox (n.): two doctors."


def test_clean_reply_adds_missing_headword():
    assert clean_reply("pharmacist", "a helper on the farm.") == \
        "pharmacist: a helper on the farm."


def test_clean_reply_headword_case_insensitive():
    assert clean_reply("paradox", "Paradox (n.): two doctors.") == \
        "Paradox (n.): two doctors."


def test_clean_reply_truncates():
    out = clean_reply("x", "x (n.): " + "y" * 1000)
    assert len(out) == 300 and out.endswith("…")


@pytest.mark.parametrize("text", [None, "", "   \n  ", '""'])
def test_clean_reply_empty(text):
    assert clean_reply("x", text) is None


# --- DefineLimiter ----------------------------------------------------------

def test_limiter_blocks_fourth_and_recovers():
    now = [1000.0]
    lim = DefineLimiter(clock=lambda: now[0])
    assert all(lim.allow("dave") for _ in range(3))
    assert not lim.allow("Dave")          # nick match is case-insensitive
    assert lim.allow("alice")             # separate budget per nick
    now[0] += 60
    assert lim.allow("dave")


# --- AgentCore.one_shot -----------------------------------------------------

class _FakeCompletions:
    def __init__(self, content=None, exc=None):
        self.content, self.exc, self.calls = content, exc, []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc:
            raise self.exc
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))],
            usage=SimpleNamespace(prompt_tokens=12, completion_tokens=7),
        )


class _FakeDB:
    def __init__(self):
        self.usage = []

    async def log_usage(self, **kwargs):
        self.usage.append(kwargs)


def _agent(cfg, completions, db):
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return AgentCore(
        chat_client=client, vision_client=None, db=db, cfg=cfg, capabilities=set(),
    )


async def test_one_shot_strips_thinking_and_logs_usage(cfg):
    comp = _FakeCompletions("<think>hmm</think>oxymoron (n.): a moron who breathes oxygen.")
    db = _FakeDB()
    out = await _agent(cfg, comp, db).one_shot(
        "SYS", "oxymoron", purpose="define", channel="#test",
    )
    assert out == "oxymoron (n.): a moron who breathes oxygen."
    sent = comp.calls[0]
    assert sent["model"] == "test-model"
    assert sent["messages"] == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "oxymoron"},
    ]
    assert db.usage == [{
        "model": "test-model", "prompt_tokens": 12, "completion_tokens": 7,
        "purpose": "define", "channel": "#test",
    }]


async def test_one_shot_returns_none_on_api_failure(cfg):
    exc = APIConnectionError(request=httpx.Request("POST", "http://x"))
    out = await _agent(cfg, _FakeCompletions(exc=exc), _FakeDB()).one_shot(
        "SYS", "x", purpose="define", channel=None,
    )
    assert out is None


async def test_one_shot_returns_none_on_empty_reply(cfg):
    out = await _agent(cfg, _FakeCompletions("<think>only thoughts"), _FakeDB()).one_shot(
        "SYS", "x", purpose="define", channel=None,
    )
    assert out is None
