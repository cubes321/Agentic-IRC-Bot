"""`!define <word>` — confidently wrong dictionary definitions.

Pure logic, no IRC or LLM dependencies: parsing the command, a per-nick
rate limit, and tidying the model's reply into one postable line. The
handler in ircclient.py glues these to `AgentCore.one_shot`.
"""

from __future__ import annotations

import re
import time
from collections import defaultdict, deque
from typing import Callable

# 3 definitions a minute per nick: enough to play with, not enough to
# turn the channel into a dictionary flood.
DEFINE_RATE_LIMIT = 3
DEFINE_RATE_WINDOW_SEC = 60.0
MAX_WORD_LEN = 40
MAX_WORDS = 3
MAX_REPLY_LEN = 300

_WORD_RE = re.compile(r"^[\w'\-]+( [\w'\-]+){0,%d}$" % (MAX_WORDS - 1))
_QUOTES = "\"'`“”‘’"


def parse_word(cmd_text: str) -> str | None:
    """Extract the word from `!define <word>`. Returns None when missing
    or not word-like (too long, too many words, symbols, control chars)."""
    rest = " ".join(cmd_text[len("!define"):].split())
    if not rest or len(rest) > MAX_WORD_LEN:
        return None
    if not _WORD_RE.match(rest) or "_" in rest:
        return None
    return rest


def clean_reply(word: str, text: str | None) -> str | None:
    """First non-empty line, quotes and markdown stripped, headword ensured,
    length capped. None if nothing usable is left."""
    if not text:
        return None
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    line = line.strip(_QUOTES + "*_ ").strip()
    if not line:
        return None
    if not line.lower().startswith(word.lower()):
        line = f"{word}: {line}"
    if len(line) > MAX_REPLY_LEN:
        line = line[: MAX_REPLY_LEN - 1].rstrip() + "…"
    return line


class DefineLimiter:
    """In-memory sliding window per nick. Lost on restart, which is fine:
    this only guards against spam, not anything a user relies on."""

    def __init__(
        self,
        limit: int = DEFINE_RATE_LIMIT,
        window: float = DEFINE_RATE_WINDOW_SEC,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.limit = limit
        self.window = window
        self._clock = clock
        self._history: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, nick: str) -> bool:
        """Record a use and return True, or return False if over the limit."""
        now = self._clock()
        key = nick.lower()
        hist = self._history[key]
        while hist and now - hist[0] >= self.window:
            hist.popleft()
        if len(hist) >= self.limit:
            return False
        hist.append(now)
        return True
