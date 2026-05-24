# Roadmap

The bot's v1 design is complete: tool-using agent loop, per-channel
personality and mode, long-term semantic memory, initiative ticks,
multi-step background tasks, IRC-native actions, graceful shutdown
with in-character goodbyes. All v1 work is shipped on `main`; the
[`CHANGELOG.md`](CHANGELOG.md) tracks the slice-by-slice history.

This document captures **possible future directions** that are
deliberately out-of-scope for v1. None of these are actively being
built right now — they're a place to think out loud about what
might come next. If any of them is interesting and you'd like to
contribute, open an issue first; some of these have load-bearing
design decisions worth talking through before code.

The items are roughly grouped: small additions near the top, larger
architectural items below.

---

## Small additions

### Long-form writing → pastebin upload

When a tool / task produces a long answer, upload it to a pastebin
service and post the URL to the channel instead of trying to fit
everything on the wire.

**Concrete near-term use case:** task results currently truncate
to 10 lines on the wire to avoid IRC excess-flood disconnects (see
`MAX_TASK_RESULT_WIRE_LINES` in `bot/tasks.py`). The truncation
notice points to the `tasks.result` DB row — which is fine for the
operator but invisible to channel users. A pastebin upload on
truncation would let the notice carry a real link:
`[task #N] ... (full result: https://pastebin.example/abc123)`.

**Design choices to make at implementation time:**
- Which service: gist / dpaste / hastebin / a self-hosted instance?
- TTL: pastes auto-expire after how long? 24h / 7d / never?
- Per-channel opt-in: some channels won't want their content pasted
  to a third-party service. Default off; channel config flag to
  enable.
- What to upload: just the result, or result + step log + tool
  call history (useful for debugging task behaviour)?

### Image generation tool

A Tier-5 tool that calls a configured image-generation backend
(SD / DALL-E / fal.ai / local ComfyUI). Posts the resulting image
URL to the channel. Likely gated by per-channel `allow_actions` the
same way Tier-5 IRC actions are.

### Long-running background jobs

RSS monitors, daily channel digests, scheduled reports. Requires
a real cron-style scheduler with pause / resume / list / cancel —
more elaborate than the current reminder loop, which is one-shot
"fire at time T."

### Tier 6: file read/write, shell

Sandboxed code execution (subprocess / E2B / Pyodide). Significant
attack surface; would land after the v1 security work is well-tested
in production and we have a clear sandbox story.

---

## `me_action` spontaneous use (known limitation)

The `me_action` tool exists and works correctly when explicitly
invoked. The problem is purely behavioural: small local models
(Qwen 3-class) have a deep IRC training prior that "actions are
wrapped in `*asterisks*` in chat text" and they default to that
pattern instead of calling the tool, even with explicit
anti-pattern guidance in both the system prompt and the tool
description.

**Mitigations already shipped** (slice 2d):
- `REPLY_SYSTEM` rule against asterisk-wrapped actions.
- `me_action` tool description warns against `/me` prefix and
  surrounding asterisks in arguments.
- `_normalise_action_text()` defensively strips a leading `/me ` and
  whole-string `*...*` from arguments — catches model fumbles.

**Two paths to revisit:**
1. **Model upgrade**: re-test action handling when adopting a
   different chat model. The infrastructure is in place; behaviour
   is purely model-dependent.
2. **Output-side rewriter**: post-process replies to detect
   `*single-line-action*` patterns and convert to a CTCP ACTION at
   send time. Rejected during v1 design (invisible magic, surprises
   model + operator), but ~15 lines if reliability ever beats
   transparency.

---

## Richer per-channel personalities ("acting like a butler")

Today `ChannelCfg.persona` is a single-string fragment (default:
`"a helpful, terse IRC chatbot."`) interpolated into four system
prompts. The *hook* is there — what's lacking is a way to author
rich, character-shaped personas conveniently and consistently.

**Design choices to make later:**

- **One string vs. a structured persona.** A richer persona could
  be a paragraph + tone notes + vocabulary preferences + signoff
  style + things-the-character-won't-do. Structured as TOML:
  ```toml
  [channels."#parlor".persona]
  brief = "a butler in the household, formal and discreet"
  tone = "formal, third-person, addresses users as 'sir' or 'madam'"
  vocabulary = "British English; 'indeed', 'very good', 'I shall'; never slang"
  greeting = "Good evening. How may I be of service?"
  signoff = "At your service."
  taboos = "never gossip about absent users; never disclose channel logs"
  ```
  All fields optional; backward-compatible — a plain string in
  `persona` still works.

- **Persona library / presets**: a `bot/personas/` directory shipping
  TOML files for common archetypes (butler, terse-helper, pirate,
  librarian, dispatcher). Channels reference by name:
  `persona_preset = "butler"`. Operators override individual fields
  per channel without rewriting the whole character. Lowers the
  "write a good persona" barrier.

- **Runtime persona switching**: operator command (`!persona <name>`
  or `!persona reset`) flips the active persona without restart.

- **Persona-aware tool gating**: a butler probably shouldn't
  `me_action("dabs")`. Personas could declare a `discourages_actions`
  set added to the system-prompt guidance.

**Open questions:**
- Length budget — the system prompt has limited room before pushing
  out recent-buffer context. Document a soft cap (~800 chars total
  after rendering).
- Do tasks use the same persona, or a "task voice" override? A
  butler's task report probably still sounds like a butler.

**When to revisit:** when (a) the operator finds themselves writing
similar paragraph-long descriptions across channels, (b) other
people start adopting the bot and want presets, or (c) runtime
switching becomes a felt need.

---

## Chat sommelier (proactive topic / content curator)

A mode where the bot doesn't just react to mentions or chime in
randomly — it *curates*. Surfaces topics, links, recall-worthy
memories, follow-ups, or just-the-right-tone-shift suggestions,
based on what the channel has been discussing and what it knows
about the channel's interests. The "sommelier" framing matters: a
curator with taste, not a random recommendation engine.

**Existing infrastructure this would build on:**
- The **initiative tick** already lets the bot proactively post in
  chatty channels.
- **Auto-recall** already pulls top-K memories matching the recent
  buffer on every reply turn.
- The **memories table** has per-channel kinds (`fact / preference /
  event / topic`) — a sommelier would draw heavily from
  `preference` (what this channel likes) and `topic` (what they
  discuss).
- The **`open_threads` table** is defined in `bot/db.py` but never
  written — designed originally for "deliver this followup later"
  and would be the natural place to queue sommelier suggestions
  for non-disruptive delivery.

**Two distinct shapes the feature could take:**

- **(A) Passive sommelier — opt-in tick variant.** New per-channel
  mode `chatty_curated`. Tick logic gains a "should I suggest a
  topic / surface a related memory?" branch alongside the existing
  "should I chime in conversationally?" branch. Cadence is slow
  (every few hours) and informed by recent buffer freshness — same
  anti-necropost defences as today's tick.

- **(B) On-demand sommelier — explicit tool / chat command.**
  `!recommend` or `<bot>: what should we talk about?`. One-shot LLM
  call that reads recent buffer + recalled memories, proposes 1-3
  things. No persistent state; user-initiated; doesn't risk noise.

(B) is the cheaper / safer starting point. (A) is the more
interesting product but has all the failure modes the initiative-
tick work already hit.

**Design questions worth pinning down:**
- **What does "topic" mean?** A specific subtopic of an ongoing
  thread ("you were talking Frieren; the OST drops next week"), or
  a totally new direction ("the channel hasn't discussed gardening
  in months — care to?")? Probably the former — the latter feels
  manipulative.
- **Source taste.** Pure-LLM-hallucinated recommendations feel bad.
  Backing them with concrete data (recall a stored memory; cite a
  tool result like wikipedia/web_search; surface a
  previously-discussed-and-paused topic) gives recommendations
  legs. The on-demand variant should be tool-using.
- **`open_threads` revival.** The dead schema field could finally
  be populated: sommelier finds a memory worth surfacing → writes
  an `open_threads` row → scheduler delivers it when the channel
  is fresh. Repurposes existing infrastructure cleanly.
- **Persona interaction.** A butler's recommendations sound
  different from a terse-helper's. Lean on whatever the richer-
  persona system above looks like.

**Order:** doing the persona work *first* probably makes sense —
the sommelier is much more interesting with a real character voice,
and retrofitting persona-awareness onto an already-built sommelier
means rewriting it.

**When to revisit:** when either (a) the on-demand `!recommend`
variant becomes a clearly-felt missing button during operation, or
(b) the persona system lands and you find yourself wanting
characters that "have things to say" rather than just personality-
shaped reply voices.

---

## Opinion / theory-of-mind model

A richer per-user model so the bot can know who's who in a channel
and treat them with appropriate context, *without* building a
reputation system that compounds dangerously. Designed in some
detail (see the local plan file's design constraints), but
deliberately deferred to revisit after the bot has run with the
current implicit-impression-from-facts design for a sustained
period.

**Hard rules** (from the original design, to preserve when picking
this up):

- New memory kind `"role"` joining `fact / preference / event /
  topic`. Roles are durable assertions about a person's place in
  the channel ("Alice is the Rust expert"). Only the extractor
  writes them.
- **Hard ban on negative-valence roles in the extractor prompt.**
  "Alice is rude" must never become a role memory. The H2
  extractor moderation (slice 2d security work) already provides
  the infrastructure for this kind of filtering.
- **Decay-by-recency in `MemoryStore.recall`**: weight recent
  memories slightly higher than old ones. Old vague impressions
  lose to recent specific ones.
- **Transparency tool `whois_in_channel(user)`**: returns the
  bot's roles + recent events + facts + preferences for a user,
  structured. Lets users audit and request corrections.
- **Per-channel scope is mandatory**: roles and impressions are
  channel-bound. Cross-channel aggregation collapses the
  per-channel privacy model.
- **No numeric scores anywhere.** The whole point is qualitative
  and bounded, not quantitative and compounding.

**When to revisit:** after the bot has run with the current design
for long enough to have empirical evidence about whether explicit
role tracking is actually missing. The current "LLM forms a soft
impression each turn from recalled facts" design may be sufficient.
