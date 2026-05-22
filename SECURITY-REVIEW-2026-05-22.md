# Security review — Agentic IRC Bot

**Reviewer:** fresh session (no build-time context)
**Commit reviewed:** `d85caff`
**Date:** 2026-05-22
**Scope:** all of `bot/`, the SQLite schema in `bot/db.py`, the config schema in `bot/config.py`, and the tool registry in `bot/tools/`. Out-of-scope: pydle internals, LM Studio, yt-dlp internals, httpx behaviour beyond defaults.

This review verified each defense the build-session handoff named, walked each of the 13 hunches it raised, and hunted for issue classes the handoff did not flag. The verdict on every hunch is given; the body of the report orders findings by severity rather than by hunch number.

---

## Trust model the code actually implements

Before findings, the verified-by-code trust model. (Mostly matches the handoff; differences noted inline.)

- **Operator** — account in `cfg.operator.accounts`. Verified at:
  - `bot/auth.py:35` `is_operator` — case-sensitive exact match on `account`.
  - `bot/ircclient.py:452` `_cmd_quit` — operator-gated.
  - `bot/tasks.py:280` `cancel_task` — operator OR issuer.
  - `bot/ircclient.py:698` `_should_engage_dm` — operator passes every non-`ignore` mode.
- **Channel op (`+o`)** — tracked from `on_mode_change` (`bot/ircclient.py:187-217`) by reading `pydle`'s parsed mode dict. Used only for `task_issuers="ops"` (`bot/policy.py:60`). Not used for any other authorization gate.
- **Regular user** — recognized by services account (Q) or unidentified. Mention in a public channel always passes engagement. DM passes only if `[dm].mode` allows.

**Identity is account-based throughout authorization.** Verified: every auth call site reads `actor.account` or `is_operator(account)`, never the raw nick. The only nick-only fallback is task cancellation when neither side has an account (`bot/tasks.py:274-278`) — documented and bounded.

---

## Findings, by severity

### CRITICAL

#### C1. SSRF — `fetch_url` and `look_at_image` accept any HTTP(S) URL with no host allowlist

**Files:** `bot/tools/url.py:43-71`, `bot/tools/vision.py:46-100`
**Reachable by:** any user who can mention the bot in any channel (default), or any operator in DMs (default).
**Hunches addressed:** #1 — **CONFIRMED**.

`_fetch_url` validates only the URL scheme (`http://`, `https://`), then calls `await ctx.http.get(url, follow_redirects=True, ...)`. There is no allowlist of hosts, no block on RFC 1918 ranges (10/8, 172.16/12, 192.168/16), no block on link-local (169.254/16), no block on loopback (127/8, ::1), and no DNS-rebinding protection. `follow_redirects=True` means an attacker-controlled host can redirect to an internal address after the URL check passes.

`_fetch_image` (vision tool) has the same shape — same scheme-only check, same `follow_redirects=True`, same lack of host gating.

**Practical impact on the deployed host:**

- LM Studio is on `http://localhost:1234/v1` (config default). A user can ask the bot `fetch http://127.0.0.1:1234/v1/models` and the bot will return the loaded model list — useful reconnaissance for crafting tool-call confusion attacks.
- If this bot ever runs on a cloud VM, `http://169.254.169.254/latest/meta-data/iam/security-credentials/` (AWS) or `http://metadata.google.internal/` (GCP) becomes a credential-exfiltration path. The bot will read up to 4000 chars of the page body and return it to the LLM, which can then post it to the channel.
- Any other localhost service (postgres on 5432, redis on 6379, an exposed admin panel) is probeable. The error-vs-success response shape is a useful side channel even without parsable content.
- Content-type allowlist on `fetch_url` (only `text/*` and `application/xhtml+xml` are returned) limits exfiltration of binary services but not text-shaped internal APIs.

`youtube_info` is a **third SSRF path** via the same vector — the tool description says "YouTube" but the code does `yt_dlp.extract_info(url, download=False)` against any HTTP(S) URL (`bot/tools/youtube.py:39-43`). yt-dlp's generic extractor will GET arbitrary URLs trying to find video data, with redirect-following inside yt-dlp itself. Same SSRF semantics.

**Mitigation:**
1. Add a shared `is_safe_url(url)` helper in a new module (or in `bot/tools/url.py`) that:
   - Resolves the host to an IP (use `socket.getaddrinfo` async-wrapped) **after** parsing — repeat the check on every redirect, because the SDK won't.
   - Rejects loopback, link-local, multicast, RFC 1918, RFC 4193, and any address in a configured deny-list.
   - Optionally accepts an operator-supplied allow-list.
2. Replace `follow_redirects=True` with a manual redirect chain that calls `is_safe_url` on each hop. httpx supports this via `http.send(req, follow_redirects=False)` plus a loop.
3. Apply the same wrapper to `youtube_info` — either reject non-YouTube hosts up front or run the URL through the safety check before handing to yt-dlp.

**Severity rationale:** Critical, not High, because (a) the attack requires only a single tool call by an unauthenticated channel user via mention, (b) the LM Studio reconnaissance succeeds today with no further effort, and (c) the cloud-metadata path would be a credential exfiltration if the bot ever moves off the home host. The TOML config (`storage.db_path`, etc.) is irrelevant — the live attack surface is the network itself.

---

### HIGH

#### H1. Prompt injection from fetched content composes with tool catalog into action triggers

**Files:** `bot/tools/url.py:62-71` (returns up to 4000 chars of page text to LLM), `bot/agent.py:483-492` (tool result truncated to 8000 chars and appended verbatim), `bot/tools/irc_native.py` (private_msg, set_topic, me_action).
**Hunches addressed:** #2 — **CONFIRMED, with composition risk worse than hunch suggests**.

A malicious page returned by `fetch_url` lands directly in the message history as a tool result. The 4000-char body cap doesn't help — only a few hundred bytes of injection text are needed. Small local models (the design's target — Qwen 7B / Hermes-class) are particularly easy to prompt-inject because they were trained on chat data where instruction-following on textual content is the default.

The catalog filter is the only thing standing between "page injection" and "attacker controls bot behaviour for the duration of the turn":

- In a `chatty` channel with `allow_actions = ["msg"]` (sample-config.toml shows this exact combination at line 152), a successful injection can call `private_msg(target_nick="anyone", message="...")`. The rate limit (`bot/tools/irc_native.py:196-198`, 3 DMs / 60s / target) caps each target but does **not** cap unique targets per turn. A single task turn (step_cap 30) can DM up to 30 distinct nicks — DM-spray vector.
- In a channel with `allow_actions = ["topic"]`, a successful injection can change the channel topic if the bot has `+o`. (See H5.)
- The `forget` tool is in the catalog whenever `memory` capability is enabled. A successful injection can `recall(query="...")` then `forget(memory_id=N)` to wipe channel memory. The "ALWAYS call recall first" instruction in the tool description is a description-only soft control; the LLM is the only thing enforcing it.

The same injection vector exists for `look_at_image` (the vision model's description is returned verbatim to the LLM as the tool result) and for `wikipedia`, `youtube_info`, `web_search` (each of which returns attacker-influenceable text). Wikipedia is editable; web-search snippets are attacker-tunable via SEO.

**Mitigation:**

1. **Defense in depth** at the tool-call site, not just the catalog. For Tier-5 tools especially (`private_msg`, `set_topic`), gate each *invocation* on the actor's identity, not just channel policy. For example: `private_msg` should refuse targets who are not currently in any channel the bot shares with the actor — that converts arbitrary-nick DM-spray into "DM someone we both know," which is far less abusable.
2. Wrap tool-result content in a clear delimiter and tell the model in the system prompt to ignore any instructions inside the delimiter. Not perfect (well-known prompt-injection-evasion territory) but raises the bar against unsophisticated payloads.
3. Consider a per-turn budget on Tier-5 calls separately from the step cap. E.g. "at most 1 `set_topic` per turn, at most 3 `private_msg` per turn." This is much easier to enforce in `bot/agent.py:_run_loop` than per-tool — the dispatcher knows the call count.
4. For `forget` specifically, require an explicit phrase in the user's trigger text ("forget", "delete", or similar) before allowing the tool — refuse the call otherwise. Confidence that the user asked is much higher than confidence in injection-resistance.

**Severity rationale:** High because actual exploitation requires both a malicious URL the LLM is convinced to fetch AND a channel with the right `allow_actions`. The latter is the security-relevant choice the operator already makes; the former is reachable trivially via mention.

#### H2. Memory extractor accepts attacker-supplied content into durable storage with no moderation

**Files:** `bot/memory.py:253-318` (extractor), `bot/prompts.py:89-124` (extractor prompt), `bot/memory.py:209-249` (recall path).
**Hunches addressed:** #3 — **CONFIRMED**.

Every 20 public-channel messages, the extractor runs over the recent transcript and writes any "facts" the LLM identifies into the `memories` table. The prompt has guidance about what to extract and what to skip — but no negative-valence ban, no content moderation, no signed-source attribution. A coordinated user (or one persistent user across many sessions) can post repeated phrases ("Bob is a known scammer", "Alice hates [group]") and have those land as durable channel memories. The dedup at `DEDUP_SIM_THRESHOLD = 0.92` (`bot/memory.py:47`) prevents the *same* fact from being inserted twice but doesn't stop near-paraphrases from accumulating.

Once stored, those memories surface:
- Whenever `recall` is called by the LLM (any tool turn).
- Whenever auto-recall is enabled (`bot/agent.py:220-241`) — top-3 by similarity are prepended to every reply turn's system context.
- In initiative-tick prompts (`bot/scheduler.py:531-538`) — top-3 by similarity included in the chime-in decision.

The `recall` floor of 0.30 (`bot/memory.py:48`) does not protect against this — a planted "fact" matches its own query at sim ≈ 1.0.

The handoff already articulated the "no negative-valence roles" constraint as a v2 design principle; the production code does **not** implement it. Today the extractor is unconstrained.

**Mitigation:**

1. In `MEMORY_EXTRACTOR_SYSTEM` (`bot/prompts.py:89`), add an explicit DO-NOT-EXTRACT clause for accusations, character attacks, claims of wrongdoing, and any third-party statement that isn't about the speaker themselves. The handoff's "deferred opinion / theory-of-mind" section already names the desired clause.
2. Add a post-extraction filter (pydantic validator or a small classifier call) that rejects facts containing slurs, accusations, or unverifiable claims. Even a basic substring deny-list of category-words is a start.
3. Consider an "extractor probation" mode: facts are written but flagged `pending=true`; recall ignores `pending` facts; an operator command (e.g. `!memory promote <id>`) graduates them. Heavy for v1; lighter alternative: rate-limit how many memories a single user-account can contribute per channel per day.

#### H3. Reminder spam — no per-user cap, no per-channel cap, table unbounded

**Files:** `bot/tools/memory_tools.py:234-252` (`set_reminder` tool), `bot/scheduler.py:354-410` (firing loop), `bot/db.py:51-56` (no retention column / cleanup).
**Hunches addressed:** #4 — **CONFIRMED**.

The `set_reminder` tool has no rate limiting and no upper bound on `when`. A single LLM call can write one row; a reply turn (step cap 6) can write up to 6 reminders; a task turn (step cap 30) can write up to 30 — but the loop detector (`bot/agent.py:473-481`) only fires on **exactly the same args twice in a row**, so an LLM nudged into setting many reminders with slightly different timings or messages bypasses the loop detector entirely.

At fire time, the scheduler polls every `reminder_poll_sec` (default 10) and posts each due row to `bot.irc_send(channel, ...)`. No per-channel rate limit on outbound posts in this path — the scheduler will fire all of them with `INTER_LINE_DELAY = 0.5s` between lines. 30 reminders firing at the same moment is a 15-second flood the channel cannot suppress without `+m` and an op manually intervening.

Combined with H1/H2, a hostile DM (under `[dm].mode = "all"` if set) or a hostile channel mention can convert one prompt-injected page into a sustained channel-flood vehicle that survives bot restarts (rows are persisted).

**Mitigation:**

1. Per-actor rate limit at `set_reminder` call: e.g. "at most 3 reminders/hour per `ctx.actor_account` (fall back to `ctx.actor_nick`), at most 20 outstanding total per channel." Implement in the tool, not the agent, so the LLM sees the rejection and can stop trying.
2. Per-channel cap at fire time: if more than N reminders are due at once, post a summary line and either coalesce or drop the rest. Logged to operator.
3. Add a retention or housekeeping step: `bot/db.py` has no `prune_reminders()`; one is straightforward (`DELETE FROM reminders WHERE fire_at < ? - retention_days`). The current firing loop deletes only after successful post; a reminder targeting a parted channel sticks forever.
4. Cap `when` at a sane horizon (e.g. 90 days). Defends against "reminder at year 2100" survival across many restarts.

#### H4. Q password is sent in cleartext by default; TLS verification is force-disabled

**Files:** `bot/main.py:330-332`, `bot/config.py:23-30` (default `port = 6667, tls = False`), `bot/ircclient.py:108-110`.
**Hunches addressed:** none — **not flagged by handoff; found during review**.

```python
# bot/main.py:326-332
await client.connect(
    hostname=cfg.server.host,
    port=cfg.server.port,
    tls=cfg.server.tls,
    tls_verify=False,  # many IRCds use self-signed; revisit per-server
)
```

- The default config (`bot/config.py:23-30`) is `port=6667, tls=False` — i.e. plaintext IRC. With those defaults, the Q AUTH password is sent in cleartext: `PRIVMSG Q@CServe.quakenet.org :AUTH <account> <password>` is the very next thing after `on_connect`.
- Even when `tls=True`, `tls_verify=False` is hardcoded — there is no per-server way to require cert verification. A network-position attacker can present a self-signed cert and intercept the Q password.

The Q password file is gitignored and read at startup only (`bot/config.py:221-226`) — good — but it has no value if it's sent in cleartext across any network path the attacker can see.

Quakenet runs Q on port 6667 too, but supports TLS on 6697. The default should be `port=6697, tls=True`, and `tls_verify` should be operator-configurable with a default of `True`.

**Mitigation:**

1. Change defaults: `port = 6697, tls = True`, add `tls_verify: bool = True` to `ServerCfg`.
2. Pass `cfg.server.tls_verify` to `client.connect(...)` instead of the hardcoded `False`.
3. Keep `tls_verify = False` as an opt-in for development against self-signed servers, but emit a `log.warning` at startup when it's used so the operator notices.

---

### MEDIUM

#### M1. `private_msg` doesn't validate target is reachable through the requester's relationship

**Files:** `bot/tools/irc_native.py:217-261`.
**Hunches addressed:** #5 — **CONFIRMED, exactly as hunch describes**.

Once `allow_actions` contains `"msg"`, the LLM may DM any nick on the network. The tool checks (a) the target isn't a channel, (b) the target isn't the bot itself, (c) the per-target rate limit of 3 / 60s. It does **not** check:

- The target is in any channel the actor is in.
- The target consented to receive DMs from the bot.
- The actor is the same person whose policy enabled the action.

The handoff calls this out and asks whether the rate limit is the only protection. It is. And it's per-target — spraying to many targets bypasses it entirely.

**Mitigation:** see H1, mitigation #1. A common-channel check (use `bot.channels[ch]['users']` or pydle's equivalent) is cheap and converts "DM anyone on the network" to "DM someone the actor and bot both share a channel with."

#### M2. `set_topic` lets any mention-capable user change the topic via the bot

**Files:** `bot/tools/irc_native.py:135-160`, `bot/tools/__init__.py:49-68` (`build_catalog`).
**Hunches addressed:** #11 — **CONFIRMED**.

When `allow_actions` includes `"topic"`, the `set_topic` tool is in the catalog for every reply turn in that channel. Any user who can mention the bot can ask it to change the topic; if the bot is `+o`, the change goes through.

The tool description (`bot/tools/irc_native.py:165-171`) warns about op requirements but enforces no actor check. Even an unidentified visitor in `#myhouse` (default `task_issuers = "all"` per sample-config) can rewrite the topic via the bot.

**Mitigation:**

1. Gate `set_topic` invocation on `ctx.actor_account` being either an operator or a channel-op of `ctx.channel`. That matches the implicit assumption: the bot's topic-change should require the requester to be someone the channel has already trusted.
2. Alternative: configure `allow_actions = ["topic"]` to mean "the LLM may decide topic changes" but require a per-channel additional `topic_changers: ["ops", "operator", "all"]` setting analogous to `task_issuers`. More surface, more flexibility.

#### M3. `forget` is destructive and channel-only-bounded

**Files:** `bot/tools/memory_tools.py:135-170`.
**Hunches addressed:** #10 — **CONFIRMED, scope correctly bounded per design**.

`_forget` refuses cross-channel deletion (good, verified at line 152-161). Within the channel, however, anyone with mention access can ask the bot to recall then forget memories. The step cap of 6 (reply) / 30 (task) caps how many can be deleted in one turn, but multiple turns by the same actor are unbounded.

There is also no audit log — `bot/db.py` does not record forgets. Once memories are deleted, they are gone.

**Mitigation:**

1. Soft-delete: add a `deleted_at TEXT` column to `memories`, `UPDATE` rather than `DELETE`. `recall` filters out `deleted_at IS NOT NULL`. Provides a forensic trail.
2. Require operator OR the memory's original `user_account` to forget — i.e. you can forget facts *about you* freely, but you can't forget channel-wide or third-party facts unless you are an op. Implementation: pass `ctx.actor_account` and the memory's `user_account` to the auth check.
3. Add an audit row to a new `audit` table whenever a Tier-5 IRC action or a `forget` succeeds. This is one extra INSERT per call, useful well beyond this finding.

#### M4. Hardcoded legacy API-key path

**Files:** `bot/main.py:46-57`.
**Hunches addressed:** #8 — partially related; **flagged as separate medium**.

```python
legacy = Path("e:/ai/openai_api_key.txt")
if legacy.exists():
    return legacy.read_text().strip()
```

The third fallback in `_resolve_api_key` reads from a hardcoded absolute Windows path tied to the original author's machine. Anyone else who runs the code with a populated config and a misconfigured env var won't hit this — but the hardcoded path is a code smell, a portability bug, and a soft pointer to a file outside the repo.

**Severity:** medium because the path won't exist on most systems (no actual harm), but every reader of the codebase will see the personal path, which is information leakage and a maintenance hazard.

**Mitigation:** Drop the legacy fallback, or move it to a config-level option (`[ai].api_key_file`). The OpenAI client treats a non-empty string as valid for LM Studio, so a sensible default ("lm-studio") is enough.

#### M5. Channel-op state can desync after edge events

**Files:** `bot/ircclient.py:187-217` (`on_mode_change`), `bot/auth.py:75-82`, dependency on pydle's `self.channels[channel]['modes']`.
**Hunches addressed:** #9 — **CONFIRMED as a low-probability but real risk**.

`set_channel_ops` is rebuilt from pydle's parsed mode dict on every `on_mode_change` event. If pydle misses a mode event (server-side bug, netsplit + rejoin race), the bot's local op set goes stale and `is_op_in_channel` returns a wrong answer.

Practical impact is limited: the only authorization that relies on `is_op_in_channel` is `task_issuers = "ops"` in `bot/policy.py:60`. A misclassified ex-op could spuriously issue a task; that task is still bounded by the same step/wall caps.

**Mitigation:** Periodically resync from a NAMES query in the scheduler (every N minutes per channel). Cheap, and recovers from any drift within one cycle.

---

### LOW

#### L1. Account-cache TTL of 5 minutes is the auth horizon

**Files:** `bot/auth.py:23,46-55`.
**Hunches addressed:** #7 — **CONFIRMED but low impact**.

Once an account is cached, the bot trusts that nick→account binding for up to 5 minutes. If a user de-auths from Q during that window, the bot still treats them as that account. Worst case: an ex-operator retains operator privileges for up to 5 min after `/msg Q LOGOUT` or after a `/quit + rejoin from a different account`.

`forget_user` (`bot/auth.py:68-71`) clears the cache on `on_quit` — so a clean disconnect-reconnect cycle resets the cache. `rename` (`bot/auth.py:60-66`) moves the cache entry — which is correct for normal nick changes (the same user changed their nick) but doesn't help with account-only state changes that don't change the nick.

**Mitigation:**

1. Listen for `account-notify` more aggressively — pydle's `on_account_change` already updates the cache (`bot/ircclient.py:160-162`), which closes the loop on servers that broadcast account changes (Quakenet does via the CAP). Verify this fires correctly in practice.
2. Drop the cache TTL to 60 seconds for operator-tier accounts (`is_operator` true) so the privilege horizon is smaller. Regular cached entries can keep the 5-min TTL.

#### L2. `message_log` retention is forever

**Files:** `bot/db.py:27-35` — no retention column, no pruning logic anywhere.
**Hunches addressed:** #12 — **CONFIRMED**.

The `message_log` table grows monotonically. Every public message in every joined channel is stored, indefinitely. Two consequences:

- Privacy: channel transcripts are durable in a way users don't expect from IRC. There is no `!forget my history` path.
- Resource: at the deployment's claimed channel count and activity level, the table may not crash anything for months, but there is no mechanism to keep it bounded.

**Mitigation:** Add an operator-configurable `[storage].message_log_retention_days` (default e.g. 90), and a periodic prune in the scheduler that runs once a day. Same housekeeping applies to `reminders` (see H3) and `token_usage`.

#### L3. Log file contains DM contents at INFO level

**Files:** `bot/ircclient.py:319` (`log.info("Engaging in %s for %s: %r", reply_target, source, trigger[:120])`), and similar at lines 254, 397.
**Hunches addressed:** none — **new finding**.

```python
log.info("Engaging in %s for %s: %r", reply_target, source, trigger[:120])
```

For DMs, `trigger` is the user's DM content (first 120 chars). With `log_path` configured (default `bot.log`, sample config writes one), the log file accumulates DM contents at INFO level. The handoff documents that the `message_log` DB table excludes DMs (verified at `bot/ircclient.py:237-241`: only `if not is_dm`). The log file does not respect that privacy boundary.

**Severity:** low because operator-only file access typically. But the asymmetry — DMs are kept out of the DB but written to the log file — is a foot-gun. An operator sharing log files for debugging would unintentionally disclose user DMs.

**Mitigation:**

1. Redact DM content at log time: `log.info("Engaging in %s for %s: <DM redacted>", ...)` when `is_dm`. Keep the source / target / decision; drop the content.
2. Or: gate the content portion behind DEBUG, so a routine INFO log doesn't capture it but a deliberate `-v` debugging session does.

#### L4. `_dm_history` rate-limit dict grows unboundedly

**Files:** `bot/tools/irc_native.py:196-214`.

Per-target history is pruned (entries inside a list); per-target dict **keys** are never removed. Over the bot's lifetime, every unique target nick ever DMed gets a key. Not a security risk (memory grows slowly), but a minor housekeeping issue.

**Mitigation:** After pruning the per-key list, drop the key if `fresh == []`. One-line change.

#### L5. `set_reminder.target_nick` is unvalidated text

**Files:** `bot/tools/memory_tools.py:234-252`.

`target_nick` is taken from the LLM args, stored in the JSON payload, and posted as `"reminder for <target>:"` at fire time. If the LLM picks `target_nick = "#geeks"`, the reminder says "reminder for #geeks: ...". No ping fires for that string because the post itself is a regular channel PRIVMSG. Cosmetic, not security-relevant.

**Mitigation:** Strip channel-prefix characters from `target_nick` at insert time. Trivial.

---

## Verdicts on every hunch

| # | Hunch | Verdict | Where |
|---|-------|---------|-------|
| 1 | URL fetch egress | **CONFIRMED** (Critical) | C1 |
| 2 | Prompt injection from fetched content | **CONFIRMED** (High) | H1 |
| 3 | Memory extractor as injection vector | **CONFIRMED** (High) | H2 |
| 4 | Reminder spam vector | **CONFIRMED** (High) | H3 |
| 5 | Tool authorization at call time vs catalog | **CONFIRMED** (Medium) | M1 |
| 6 | `!task` issuance in DMs | **DISMISSED** — `bot/ircclient.py:283,286,289` all gate on `not is_dm`. DM messages with `!task` text fall through to the engagement path; the agent has no tool that issues tasks (`task_runner` is not exposed as a tool). Verified safe. |
| 7 | Account-cache TTL race | **CONFIRMED** (Low) | L1 |
| 8 | Persistence path safety | **DISMISSED for path traversal** (config is operator-provided, no user input reaches `db_path`/`log_path`/`q_password_file`). **CONFIRMED for hardcoded legacy path** (Medium) | M4 |
| 9 | Channel-op state freshness | **CONFIRMED** (Medium) | M5 |
| 10 | `forget` destructive scope | **CONFIRMED** (Medium) | M3 |
| 11 | `set_topic` consent | **CONFIRMED** (Medium) | M2 |
| 12 | `message_log` retention | **CONFIRMED** (Low) | L2 |
| 13 | Search-provider key leakage | **MOSTLY DISMISSED**. Brave's API key is in the `X-Subscription-Token` header, not the URL — not echoed in the URL-based parts of error messages. Tavily's key is in the JSON body, also not in URL. The Brave error path at `bot/tools/web.py:69` does include `r.text[:200]` from a non-recoverable HTTP error — if Brave's response body echoes the API key (extremely unlikely; not documented behaviour), that text could end up in the LLM's tool-result context. Not a confirmed leak path; flag for verification by inspecting actual error responses, not a finding to action. |

---

## Verification of handoff's "Already-implemented defenses" table

Each defense from the handoff verified against actual code:

| Defense | Cited at | Verdict |
|---|---|---|
| DM policy gate | `_should_engage_dm`, `bot/config.py:DmCfg` | **Verified.** Mode dispatch is correct; operator override fires before allowlist; account-only identity. `bot/ircclient.py:677-706`. |
| Operator account check | `auth.is_operator` | **Verified.** Account-based, case-sensitive, requires non-empty account. `bot/auth.py:35-36`. (Cache TTL caveat: L1.) |
| Tool capability filtering | `tools.build_catalog` | **Verified.** Capability subset check + action filter applied per catalog build. `bot/tools/__init__.py:49-68`. |
| Tier-5 actions gated | `irc_native.py requires={"action:..."}` | **Verified at catalog level.** Per-call authorization missing — see H1, M1, M2. |
| `private_msg` rate limit | `_check_and_record_dm` | **Verified.** Lock-protected; per-target pruning works. Per-target only, not per-actor — limitation noted in M1. |
| Task auth | `policy.can_issue_tasks`, `tasks.cancel_task` | **Verified.** Account-based, operator beats everything, owner-or-operator on cancel. `bot/policy.py:53-63`, `bot/tasks.py:268-298`. |
| Cross-channel memory privacy | `memory.recall`, `_forget` | **Verified.** All queries `WHERE channel = ?`. `_forget` refuses cross-channel. `bot/memory.py:217-221`, `bot/tools/memory_tools.py:152-161`. |
| `log_search` channel-scoped | `_log_search` DM refusal + SQL `channel = ?` | **Verified.** Returns explicit error if `ctx.channel` is not a real channel name (RFC 2811 check). `bot/tools/log_search.py:26-50`. |
| SQL parameterization | every query | **Verified.** Every query in `bot/db.py` and tool modules uses `?` placeholders. `bot/tools/log_search.py:66-81` builds SQL dynamically but only with fixed fragments + parameterized values. No string concatenation of user input. |
| Image fetch validation | `vision._fetch_image` | **Verified.** Content-type allowlist + extension fallback + 5 MB streamed cap. `bot/tools/vision.py:46-100`. **But:** scheme-only URL check — see C1 (SSRF). |
| Calc safety | `bot/tools/calc_tool.py` | **Verified.** AST walker; only arithmetic ops, `abs/round/min/max`. No `eval`, no `exec`, no attribute access, no name lookup beyond the allow-list. `bot/tools/calc_tool.py:29-48`. |
| Text-tool-call leak defense | `agent._looks_like_text_tool_call` + repair | **Verified.** Detection + repair-and-continue in loop; safety-net strip in budget-exhausted summary. `bot/agent.py:80-108, 413-429, 515-520`. |
| `<think>` block stripping | `agent._strip_thinking` | **Verified.** Closed and unclosed `<think>` tags removed. `bot/agent.py:47-62`. |
| Q password file | `config.load_q_password` | **Verified** for in-memory + gitignore. **NOT verified** for cleartext-transit — see H4. |
| Step cap + wall budget | `agent._run_loop` | **Verified.** Hard step cap + wall-clock deadline. `bot/agent.py:370-380`. |
| Bot self-message guard | `on_message` self check | **Verified** with caveat: comparison is `source == self.nickname` (case-sensitive); ignores via `source.lower()`. `bot/ircclient.py:224-226`. Pydle normally normalizes casing, but a case-mismatched event could bypass the self-guard. Not a finding; low impact. |

---

## Additional notes / non-findings worth recording

- **No SQL injection** found. Every query verified to use parameterized placeholders.
- **No race conditions** found in `_inflight_engagements` set, `_dm_history` dict (lock-protected), or TaskRunner's `_running` dict (single event loop; verified no `await` between check-and-mutate pairs).
- **`yt_dlp` as a code-execution vector** — yt-dlp historically has had RCE-class bugs via crafted URLs hitting site-specific extractors. Mitigation: keep yt-dlp pinned and updated; consider adding `youtube_info` to the SSRF mitigation in C1 so non-YouTube hosts are rejected up front.
- **The agent's `last_sig` loop detector** matches only identical-name + identical-args. A small variation (one whitespace, one re-ordered key) bypasses it. The handoff acknowledges this in the design; not a security issue per se, but composes with H3 (reminder spam loop bypass).
- **`open_threads` table** is defined in the schema but never written or read in the current code (verified by grep). Dead schema. Not a security issue but worth removing or implementing — the schema-doc divergence is misleading to future readers.

---

## Suggested next steps, ordered by impact-to-effort

1. **(Critical, low effort)** Land the SSRF mitigation: a single `is_safe_url` helper used by `fetch_url`, `look_at_image`, and `youtube_info`. ~50 lines including manual-redirect loop. Closes C1.
2. **(High, low effort)** Flip `tls` and `tls_verify` defaults; warn on insecure connect. ~10 lines. Closes H4.
3. **(Medium, low effort)** Add a `common_channel(actor, target, bot)` check in `private_msg`. Closes much of M1 / a slice of H1.
4. **(High, medium effort)** Per-actor rate limit on `set_reminder` + per-channel cap at fire time. Closes H3.
5. **(High, medium effort)** Negative-valence ban in `MEMORY_EXTRACTOR_SYSTEM` prompt + post-extraction filter. Closes most of H2.
6. **(Medium, medium effort)** Soft-delete on `memories` + audit table for Tier-5 actions and `forget`. Closes M3 forensically and gives every future review more to work with.
7. **(Low, low effort)** Redact DM content in `log.info` calls. Closes L3.
8. **(Low, trivial)** Drop the hardcoded `e:/ai/openai_api_key.txt` fallback. Closes M4.

Each of the above is a separable change; none requires architecture rework.
