"""Wikipedia lookup via the REST API. Tries the summary endpoint first; on
404 falls back to a search → top result → summary chain so the tool 'just
works' for fuzzy queries that aren't exact page titles."""

from __future__ import annotations

import asyncio
import logging

import httpx

from . import Tool, ToolContext, register

log = logging.getLogger(__name__)


# Per-call HTTP timeout for Wikipedia. 10s is generous — the summary endpoint
# is fast (~200ms median), and the search-then-summary chain still fits well
# under our default per-tool wall budget.
HTTP_TIMEOUT = 10.0
USER_AGENT = (
    "AgenticIRCBot/0.1 (https://github.com/example/agentic-irc-bot; "
    "via openai sdk + httpx)"
)
# Wikipedia's API guidelines ask for a descriptive User-Agent. The 'via'
# segment isn't policy but helps the WMF distinguish friendly bots from
# scrapers if they ever need to throttle.

# Most-extract text in the response is already short (~1-3 sentences) but
# pages on technical topics can run longer. Cap defensively so the LLM's
# context budget isn't bombed by an unexpectedly verbose page.
MAX_EXTRACT_CHARS = 1200


async def _summary(http: httpx.AsyncClient, lang: str, title: str) -> dict | None:
    """Try to fetch the REST 'page summary' for an exact title.

    Returns the parsed JSON on success, None if not found (404). Other
    HTTP / network errors raise, so the caller decides whether to retry,
    fall back, or surface as a tool error."""
    # URL-encode the title manually rather than relying on httpx (path
    # segments need / kept literal, but spaces must become %20 not +).
    from urllib.parse import quote
    url = f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{quote(title, safe='')}"
    r = await http.get(
        url,
        timeout=HTTP_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


async def _search_top_title(http: httpx.AsyncClient, lang: str, query: str) -> str | None:
    """Return the top search result's page title, or None if no hits.
    Used as a fallback when a direct title lookup 404s."""
    url = f"https://{lang}.wikipedia.org/w/api.php"
    params = {
        "action": "query",
        "list": "search",
        "srsearch": query,
        "srlimit": 1,
        "format": "json",
        "formatversion": 2,
    }
    r = await http.get(
        url,
        params=params,
        timeout=HTTP_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    r.raise_for_status()
    data = r.json()
    hits = data.get("query", {}).get("search", [])
    if not hits:
        return None
    return hits[0].get("title")


def _format_result(data: dict, used_fallback: bool, original_query: str) -> dict:
    """Shape a summary JSON response into the tool's return contract."""
    extract = (data.get("extract") or "").strip()
    if len(extract) > MAX_EXTRACT_CHARS:
        extract = extract[: MAX_EXTRACT_CHARS - 1] + "…"

    result = {
        "title": data.get("title") or "",
        "description": data.get("description") or "",   # one-line subtitle
        "extract": extract,
        "url": (
            (data.get("content_urls") or {})
            .get("desktop", {})
            .get("page")
            or ""
        ),
        "page_type": data.get("type") or "standard",
    }
    if used_fallback:
        result["note"] = (
            f"No exact page for {original_query!r}; showing top search result."
        )
    if result["page_type"] == "disambiguation":
        # Disambiguation pages have a meta-extract listing options. Surface
        # this so the LLM can ask the user which sense they meant, rather
        # than guessing.
        result["note"] = (
            "This is a Wikipedia disambiguation page — the query has multiple "
            "meanings. Ask the user to be more specific."
        )
    return result


async def _wikipedia(ctx: ToolContext, args: dict) -> dict:
    query = (args.get("query") or "").strip()
    if not query:
        return {"error": "query is empty"}
    if len(query) > 200:
        return {"error": "query too long (max 200 chars)"}
    lang = (args.get("language") or "en").strip().lower() or "en"
    # Restrict to ISO-639-1-ish two/three-letter codes to prevent the model
    # from accidentally injecting path segments via language= shenanigans.
    if not lang.replace("-", "").isalpha() or len(lang) > 8:
        return {"error": f"invalid language code: {lang!r}"}

    try:
        # First attempt: exact-title summary. Fast path for "Python (programming language)"-style queries.
        data = await _summary(ctx.http, lang, query)
        if data is None:
            # 404 → search for the term and take the top hit, then fetch its summary.
            top = await _search_top_title(ctx.http, lang, query)
            if top is None:
                return {"error": f"no Wikipedia results for {query!r}"}
            data = await _summary(ctx.http, lang, top)
            if data is None:
                return {
                    "error": (
                        f"search found {top!r} but its page summary 404'd "
                        "(unusual; try a different query)"
                    ),
                }
            return _format_result(data, used_fallback=True, original_query=query)
        return _format_result(data, used_fallback=False, original_query=query)
    except (httpx.RequestError, asyncio.TimeoutError) as e:
        return {"error": f"network error: {e}"}
    except httpx.HTTPStatusError as e:
        return {"error": f"HTTP {e.response.status_code} from Wikipedia"}
    except Exception as e:
        log.exception("wikipedia: unexpected error")
        return {"error": f"unexpected error: {e}"}


register(Tool(
    name="wikipedia",
    description=(
        "Look up a topic on Wikipedia and return a short summary. Tries the "
        "exact title first; on 404 falls back to search + top result. Returns "
        "the page title, one-line description, summary extract, and URL. "
        "On disambiguation pages, returns a note asking for clarification "
        "rather than guessing. Use for factual lookups about people, places, "
        "concepts, history — anything Wikipedia covers."
    ),
    schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Page title or search term. 'Albert Einstein', "
                    "'Python (programming language)', or a fuzzy query like "
                    "'einsteins theory of relativity' (search fallback handles it)."
                ),
            },
            "language": {
                "type": "string",
                "description": (
                    "Wikipedia language code (default 'en'). Examples: "
                    "'fr', 'de', 'ja', 'simple' (Simple English)."
                ),
                "default": "en",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    requires=set(),
    call=_wikipedia,
))
