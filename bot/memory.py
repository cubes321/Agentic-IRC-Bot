"""Long-term memory: extractor (write) + cosine recall (read).

Storage:
- All memories live in the `memories` table with a numpy float32 BLOB column.
- Vectors are unit-normalised at insert time, so cosine similarity at query
  time is a single dot product per row.

Two paths:
- WRITE: every N messages in a channel, MemoryWriter calls a cheap LLM with the
  MEMORY_EXTRACTOR_SYSTEM prompt, parses the JSON list, embeds each fact, and
  inserts. Per-channel counter, fully async, errors are logged and the batch is
  dropped (never poisons the next one).
- READ: the `recall` tool calls MemoryStore.recall(channel, query, k=5),
  which embeds the query and returns the top-K rows by dot-product against
  the channel's embedding stack.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import numpy as np
from openai import AsyncOpenAI, APIError, APIConnectionError
from pydantic import BaseModel, Field, ValidationError

from . import prompts
from .db import Database, now_utc_iso

log = logging.getLogger(__name__)


class _NullCtx:
    async def __aenter__(self): return self
    async def __aexit__(self, *_): return False


def _maybe_sem(sem: asyncio.Semaphore | None):
    return sem if sem is not None else _NullCtx()


EXTRACTOR_BATCH_SIZE = 20
DEDUP_SIM_THRESHOLD = 0.92      # skip if any existing memory >= this
RECALL_FLOOR = 0.30             # drop results below this
DEFAULT_RECALL_K = 5


# ---- Negative-valence filter for stored memories (security review H2). ----
#
# The MEMORY_EXTRACTOR_SYSTEM prompt now explicitly bans extracting
# character claims, slurs, and third-party accusations. This filter is a
# defensive backstop: prompt is the primary control, this catches the
# most-egregious payloads if the LLM lets one through. Applied at add()
# so it covers BOTH the extractor path AND explicit `remember` tool
# calls — the memories table is the durable surface, not the entry point.
#
# Deliberately narrow: only matches obvious slurs and the "<X> is/was
# (a) <pejorative>" pattern. False negatives (subtle insults the regex
# misses) are accepted as the cost of avoiding false positives on
# legitimate content ("Bob hates pineapple", "Alice thinks Rust is
# overrated" — both fine and shouldn't be filtered).

# Pejorative nouns used in identity-attack form ("X is a scammer").
_PEJORATIVE_NOUNS = (
    "scammer", "liar", "fraud", "fraudster", "cheat", "cheater", "thief",
    "stalker", "predator", "pedophile", "paedophile", "creep",
    "racist", "sexist", "homophobe", "transphobe",
    "nazi", "fascist", "terrorist", "rapist", "abuser",
    "psycho", "sociopath", "narcissist",
)
# Pejorative adjectives used in identity-attack form ("X is dumb").
_PEJORATIVE_ADJ = (
    "stupid", "dumb", "retarded", "retard", "idiot", "moron", "imbecile",
    "asshole", "bitch", "bastard", "cunt", "pathetic", "worthless",
)
# Slur terms — direct hate-speech vocabulary. Deliberately small list;
# the prompt's "slurs/hate speech in any form" instruction is the
# primary defense. We're catching the most-blatant payloads only.
_SLUR_TERMS = (
    "nigger", "nigga", "faggot", "tranny", "kike", "spic",
    "chink", "wetback", "gook",
)

# Pattern: "<word> (is|was|are|were) (a|an)? <pejorative>"
# Catches: "alice is a scammer", "Bob was an idiot", "they are racists"
_IS_PEJORATIVE_RE = re.compile(
    r"\b\w+\s+(?:is|was|are|were)\s+(?:an?\s+)?(?:"
    + "|".join(_PEJORATIVE_NOUNS + _PEJORATIVE_ADJ)
    + r")s?\b",
    re.IGNORECASE,
)
_SLUR_RE = re.compile(
    r"\b(?:" + "|".join(_SLUR_TERMS) + r")s?\b",
    re.IGNORECASE,
)


def _is_memory_content_safe(content: str) -> tuple[bool, str]:
    """Return (safe, reason). False if the content matches obvious
    negative-valence patterns. The patterns are intentionally narrow:
    this is a backstop, not a moderation system. Prompt is primary."""
    if not content:
        return True, ""  # empty content fails elsewhere; not our concern
    if _SLUR_RE.search(content):
        return False, "contains slur or hate-speech term"
    if _IS_PEJORATIVE_RE.search(content):
        return False, "matches '<who> is (a) <pejorative>' pattern"
    return True, ""


class _ExtractedFact(BaseModel):
    kind: str = Field(pattern=r"^(fact|preference|event|topic)$")
    user_account: str | None = None
    content: str = Field(min_length=1, max_length=400)


def _to_unit(vec: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vec))
    if norm == 0.0:
        return vec
    return (vec / norm).astype(np.float32)


def _blob(vec: np.ndarray) -> bytes:
    return _to_unit(vec).tobytes()


def _from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


@dataclass
class MemoryHit:
    id: int
    kind: str
    user_account: str | None
    content: str
    created_at: str
    similarity: float


class MemoryStore:
    """Embeddings + similarity search over the `memories` table.

    All embedding calls go through a single AsyncOpenAI client pointed at
    LM Studio (or any OpenAI-compatible endpoint). The chat client is held
    separately for the extractor's structured-output call.
    """

    def __init__(
        self,
        db: Database,
        chat_client: AsyncOpenAI,
        chat_model: str,
        embed_client: AsyncOpenAI,
        embed_model: str,
        chat_semaphore: asyncio.Semaphore | None = None,
    ):
        self.db = db
        self.chat_client = chat_client
        self.chat_model = chat_model
        self.embed_client = embed_client
        self.embed_model = embed_model
        # Shared with AgentCore + Scheduler so all chat completions contend
        # for the same slot pool. Embeddings don't share — they hit a different
        # model on a separate device path.
        self._chat_sem = chat_semaphore

    # ---- embedding ----

    async def embed(self, text: str, purpose: str = "embed", channel: str | None = None) -> np.ndarray:
        return (await self.embed_batch([text], purpose=purpose, channel=channel))[0]

    async def embed_batch(
        self,
        texts: list[str],
        purpose: str = "embed",
        channel: str | None = None,
    ) -> list[np.ndarray]:
        if not texts:
            return []
        if not self.embed_model:
            raise RuntimeError("no embed_model configured")
        resp = await self.embed_client.embeddings.create(
            model=self.embed_model,
            input=texts,
        )
        # Token-usage logging.
        # Embedding calls have prompt_tokens but no completion_tokens. LM Studio's
        # embeddings endpoint frequently returns a usage object with prompt_tokens=0
        # (the field is technically present, just unpopulated). When that happens
        # we fall back to a character-length heuristic so local numbers still
        # reflect actual work done — important for cloud-cost projection. ~4
        # chars/token is correct enough for English text; non-English text or
        # special tokens may diverge but the order of magnitude is right.
        try:
            reported = 0
            usage = getattr(resp, "usage", None)
            if usage is not None:
                reported = getattr(usage, "prompt_tokens", 0) or 0
            prompt_tokens = reported or sum(max(1, len(t) // 4) for t in texts)
            await self.db.log_usage(
                model=self.embed_model,
                prompt_tokens=prompt_tokens,
                completion_tokens=0,
                purpose=purpose,
                channel=channel,
            )
        except Exception:
            log.exception("failed to log embed usage")
        return [np.asarray(item.embedding, dtype=np.float32) for item in resp.data]

    # ---- write ----

    async def add(
        self,
        channel: str,
        kind: str,
        content: str,
        user_account: str | None = None,
        dedup: bool = True,
    ) -> int | None:
        """Insert a memory. Returns the new row id, or None if deduped or
        filtered.

        Filtering: content goes through `_is_memory_content_safe` first
        (security review H2, 2026-05-22). Stops obvious slurs and
        "<X> is (a) <pejorative>" patterns from being durably stored.
        Applied here at add() rather than only in the extractor path so
        explicit `remember` tool calls are subject to the same gate."""
        safe, reason = _is_memory_content_safe(content)
        if not safe:
            # INFO level so the operator can see what was attempted but
            # spammers (extractor-via-injection) don't get an LLM-visible
            # error response that tells them what triggered the filter.
            log.info(
                "memory rejected in %s (%s): %r",
                channel, reason, content[:120],
            )
            return None

        try:
            vec = await self.embed(content, purpose="embed_write", channel=channel)
        except (APIError, APIConnectionError) as e:
            log.warning("embed failed for memory in %s: %s", channel, e)
            return None

        if dedup:
            existing = await self._channel_vectors(channel)
            if existing.size > 0:
                sims = existing @ _to_unit(vec)
                if float(sims.max()) >= DEDUP_SIM_THRESHOLD:
                    log.debug(
                        "skipping near-duplicate memory in %s (max sim=%.3f): %r",
                        channel, float(sims.max()), content[:80],
                    )
                    return None

        new_id = await self.db.insert_returning_id(
            "INSERT INTO memories (channel, user_account, kind, content, created_at, embedding) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (channel, user_account, kind, content, now_utc_iso(), _blob(vec)),
        )
        log.info("memory[%d] %s/%s/%s: %s", new_id, channel, kind, user_account or "-", content[:80])
        return new_id

    async def forget(self, memory_id: int) -> bool:
        row = await self.db.fetchone("SELECT id FROM memories WHERE id = ?", (memory_id,))
        if not row:
            return False
        await self.db.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        return True

    # ---- read ----

    async def _channel_vectors(self, channel: str) -> np.ndarray:
        """Return an (N, dim) matrix of unit-normalised embeddings for a channel."""
        rows = await self.db.fetchall(
            "SELECT embedding FROM memories WHERE channel = ? AND embedding IS NOT NULL",
            (channel,),
        )
        if not rows:
            return np.zeros((0,), dtype=np.float32)
        return np.vstack([_from_blob(r["embedding"]) for r in rows])

    async def recall(
        self,
        channel: str,
        query: str,
        k: int = DEFAULT_RECALL_K,
    ) -> list[MemoryHit]:
        if not query.strip():
            return []
        rows = await self.db.fetchall(
            "SELECT id, kind, user_account, content, created_at, embedding "
            "FROM memories WHERE channel = ? AND embedding IS NOT NULL",
            (channel,),
        )
        if not rows:
            return []
        try:
            qvec = await self.embed(query, purpose="embed_recall", channel=channel)
        except (APIError, APIConnectionError) as e:
            log.warning("embed failed for recall in %s: %s", channel, e)
            return []
        qvec_unit = _to_unit(qvec)
        mat = np.vstack([_from_blob(r["embedding"]) for r in rows])
        sims = mat @ qvec_unit
        order = np.argsort(-sims)
        out: list[MemoryHit] = []
        for idx in order[: max(k * 2, k)]:  # over-fetch then floor-filter
            sim = float(sims[idx])
            if sim < RECALL_FLOOR:
                break
            r = rows[idx]
            out.append(MemoryHit(
                id=r["id"],
                kind=r["kind"],
                user_account=r["user_account"],
                content=r["content"],
                created_at=r["created_at"],
                similarity=sim,
            ))
            if len(out) >= k:
                break
        return out

    # ---- extractor ----

    async def extract_and_store(self, channel: str, n_messages: int = EXTRACTOR_BATCH_SIZE) -> int:
        """Pull last N messages, ask the LLM for durable facts, store them.
        Returns the number of facts inserted (post-dedup)."""
        rows = await self.db.recent_messages(channel, n=n_messages)
        if not rows:
            return 0

        transcript_lines = [f"<{r['nick']}> {r['content']}" for r in rows]
        accounts_line = ", ".join(
            f"{r['nick']}={r['account']}" for r in rows if r["account"]
        ) or "(none known)"
        user_msg = (
            f"Channel: {channel}\n"
            f"Known accounts: {accounts_line}\n"
            f"Transcript (most recent last):\n" + "\n".join(transcript_lines)
        )
        log.info("extractor: running on %s with %d-message transcript", channel, len(rows))
        try:
            async with _maybe_sem(self._chat_sem):
                resp = await self.chat_client.chat.completions.create(
                    model=self.chat_model,
                    messages=[
                        {"role": "system", "content": prompts.MEMORY_EXTRACTOR_SYSTEM},
                        {"role": "user", "content": user_msg},
                    ],
                    temperature=0,
                    timeout=120.0,  # extractor has all the context, give it room
                )
        except (APIError, APIConnectionError, asyncio.TimeoutError) as e:
            log.warning("extractor LLM call failed in %s: %s", channel, e)
            return 0
        try:
            usage = getattr(resp, "usage", None)
            if usage is not None:
                await self.db.log_usage(
                    model=self.chat_model,
                    prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                    completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                    purpose="extract",
                    channel=channel,
                )
        except Exception:
            log.exception("failed to log extractor usage")
        log.debug("extractor: raw output for %s: %r", channel, (resp.choices[0].message.content or "")[:300])

        raw = (resp.choices[0].message.content or "").strip()
        facts = _parse_facts(raw)
        if not facts:
            return 0

        stored = 0
        for fact in facts:
            try:
                new_id = await self.add(
                    channel=channel,
                    kind=fact.kind,
                    content=fact.content,
                    user_account=fact.user_account,
                    dedup=True,
                )
                if new_id is not None:
                    stored += 1
            except Exception:
                log.exception("failed to store extracted fact in %s", channel)
        log.info("extractor: %s facts stored in %s (raw count=%d)", stored, channel, len(facts))
        return stored


def _parse_facts(raw: str) -> list[_ExtractedFact]:
    """Parse the extractor's JSON output defensively. Returns [] on any failure."""
    if not raw or raw == "[]":
        return []
    # Strip markdown fences if the model wrapped its output.
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE)
    # Find the first [ and last ] to tolerate stray prose.
    start, end = raw.find("["), raw.rfind("]")
    if start == -1 or end == -1 or end < start:
        log.debug("extractor: no JSON array found in output: %r", raw[:200])
        return []
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError as e:
        log.debug("extractor: JSON parse failed: %s", e)
        return []
    if not isinstance(data, list):
        return []
    out: list[_ExtractedFact] = []
    for item in data:
        try:
            out.append(_ExtractedFact.model_validate(item))
        except ValidationError as e:
            log.debug("extractor: dropped invalid fact: %s", e)
    return out


class MemoryWriter:
    """Per-channel message counter that fires the extractor every N messages.

    Owned by the bot; IRCBot.on_message calls `note_message(channel)` after
    persisting each public-channel message. The actual extractor work runs as
    a fire-and-forget asyncio task so message handling never blocks on it.
    """

    def __init__(self, store: MemoryStore, batch_size: int = EXTRACTOR_BATCH_SIZE):
        self.store = store
        self.batch_size = batch_size
        self._counters: dict[str, int] = {}
        self._inflight: dict[str, asyncio.Task] = {}

    def note_message(self, channel: str) -> None:
        c = self._counters.get(channel, 0) + 1
        self._counters[channel] = c
        log.debug("memory writer: %s counter=%d/%d", channel, c, self.batch_size)
        if c < self.batch_size:
            return
        # Reset counter; fire extractor in background.
        self._counters[channel] = 0
        existing = self._inflight.get(channel)
        if existing is not None and not existing.done():
            log.info("extractor: skipping fire for %s (previous run still in-flight)", channel)
            return
        log.info("extractor: firing for %s (counter reached %d)", channel, self.batch_size)
        task = asyncio.create_task(self._run(channel))
        self._inflight[channel] = task

    def counter(self, channel: str) -> int:
        """For diagnostics: current count for a channel."""
        return self._counters.get(channel, 0)

    async def _run(self, channel: str) -> None:
        try:
            await self.store.extract_and_store(channel)
        except Exception:
            log.exception("memory extractor crashed in %s", channel)
