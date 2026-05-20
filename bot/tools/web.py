"""Web search tool. Provider chain with one-shot fallback.

Backends:
- BraveBackend   (requires brave_api_key)
- DuckDuckGoBackend (no key; via duckduckgo-search)
- TavilyBackend  (requires tavily_api_key)

The chain is built from cfg.tools.search_provider then cfg.tools.search_fallback.
On a defined set of recoverable errors (rate-limit, auth, network, empty),
the chain advances once and returns. Other errors surface to the LLM as a tool
error so it can choose a different approach.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Protocol

import httpx

from . import Tool, ToolContext, register

log = logging.getLogger(__name__)


@dataclass
class Result:
    title: str
    url: str
    snippet: str


class BackendError(Exception):
    """Recoverable error: try the fallback."""


class SearchBackend(Protocol):
    name: str
    async def search(self, http: httpx.AsyncClient, query: str, n: int) -> list[Result]: ...


class BraveBackend:
    name = "brave"

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def search(self, http: httpx.AsyncClient, query: str, n: int) -> list[Result]:
        if not self.api_key:
            raise BackendError("Brave: no API key configured")
        try:
            r = await http.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": min(n, 20)},
                headers={"X-Subscription-Token": self.api_key, "Accept": "application/json"},
                timeout=10.0,
            )
        except (httpx.RequestError, asyncio.TimeoutError) as e:
            raise BackendError(f"Brave: network error: {e}") from e
        if r.status_code == 401 or r.status_code == 403:
            raise BackendError(f"Brave: auth error (HTTP {r.status_code})")
        if r.status_code == 429:
            raise BackendError("Brave: rate limited")
        if r.status_code >= 500:
            raise BackendError(f"Brave: server error (HTTP {r.status_code})")
        if r.status_code >= 400:
            raise RuntimeError(f"Brave: HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
        items = (data.get("web") or {}).get("results") or []
        out = [
            Result(title=i.get("title", ""), url=i.get("url", ""), snippet=i.get("description", ""))
            for i in items[:n]
        ]
        if not out:
            raise BackendError("Brave: empty results")
        return out


class DuckDuckGoBackend:
    name = "duckduckgo"
    # Hard cap on the sync DDG call so a hung scrape can't burn agent budget.
    CALL_TIMEOUT_SEC = 12.0

    @staticmethod
    def _import_ddgs():
        """Try the new package name first, fall back to the legacy name.
        Returns the DDGS class. Raises BackendError on missing dependency."""
        try:
            from ddgs import DDGS  # renamed package, late 2024+
            return DDGS, "ddgs"
        except ImportError:
            pass
        try:
            from duckduckgo_search import DDGS  # legacy package name
            return DDGS, "duckduckgo_search"
        except ImportError as e:
            raise BackendError(
                f"no DuckDuckGo library installed (tried 'ddgs' and "
                f"'duckduckgo_search'): {e}"
            ) from e

    async def search(self, http: httpx.AsyncClient, query: str, n: int) -> list[Result]:
        DDGS, pkg = self._import_ddgs()

        def _run() -> list[Result]:
            with DDGS() as ddgs:
                hits = list(ddgs.text(query, max_results=n))
            return [
                Result(
                    title=h.get("title", ""),
                    url=h.get("href", "") or h.get("url", ""),
                    snippet=h.get("body", "") or h.get("description", ""),
                )
                for h in hits
            ]

        try:
            results = await asyncio.wait_for(
                asyncio.to_thread(_run),
                timeout=self.CALL_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError as e:
            raise BackendError(
                f"DuckDuckGo ({pkg}): timed out after {self.CALL_TIMEOUT_SEC}s"
            ) from e
        except Exception as e:
            # Includes RatelimitException, DuckDuckGoSearchException,
            # network errors from the underlying scraper, etc. The exact
            # exception classes vary across library versions, so we catch
            # broadly and surface the type + message.
            raise BackendError(
                f"DuckDuckGo ({pkg}): {type(e).__name__}: {e}"
            ) from e
        if not results:
            raise BackendError(f"DuckDuckGo ({pkg}): empty results")
        return results


class TavilyBackend:
    name = "tavily"

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def search(self, http: httpx.AsyncClient, query: str, n: int) -> list[Result]:
        if not self.api_key:
            raise BackendError("Tavily: no API key configured")
        try:
            r = await http.post(
                "https://api.tavily.com/search",
                json={"api_key": self.api_key, "query": query, "max_results": n},
                timeout=15.0,
            )
        except (httpx.RequestError, asyncio.TimeoutError) as e:
            raise BackendError(f"Tavily: network error: {e}") from e
        if r.status_code == 401 or r.status_code == 403:
            raise BackendError(f"Tavily: auth error (HTTP {r.status_code})")
        if r.status_code == 429:
            raise BackendError("Tavily: rate limited")
        if r.status_code >= 400:
            raise RuntimeError(f"Tavily: HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
        items = data.get("results") or []
        out = [
            Result(title=i.get("title", ""), url=i.get("url", ""), snippet=i.get("content", ""))
            for i in items[:n]
        ]
        if not out:
            raise BackendError("Tavily: empty results")
        return out


def _build_backend(name: str, cfg) -> SearchBackend | None:
    if name == "brave":
        return BraveBackend(cfg.tools.brave_api_key)
    if name == "duckduckgo":
        return DuckDuckGoBackend()
    if name == "tavily":
        return TavilyBackend(cfg.tools.tavily_api_key)
    if name == "":
        return None
    log.warning("Unknown search backend %r, ignoring", name)
    return None


async def _search_call(ctx: ToolContext, args: dict) -> dict:
    query = args["query"]
    n = int(args.get("n", 5))
    n = max(1, min(n, 10))

    primary = _build_backend(ctx.cfg.tools.search_provider, ctx.cfg)
    fallback = _build_backend(ctx.cfg.tools.search_fallback, ctx.cfg)
    chain = [b for b in (primary, fallback) if b is not None]
    if not chain:
        return {"error": "no search backends configured"}

    errors: list[str] = []
    for backend in chain:
        try:
            results = await backend.search(ctx.http, query, n)
            log.info(
                "Search via %s for %r: %d result(s)", backend.name, query, len(results)
            )
            return {
                "backend": backend.name,
                "results": [
                    {"title": r.title, "url": r.url, "snippet": r.snippet} for r in results
                ],
            }
        except BackendError as e:
            log.warning("Search backend %s failed: %s", backend.name, e)
            errors.append(f"{backend.name}: {e}")
            continue
        except Exception as e:
            # Unexpected: log full traceback but treat as a chain-advancing error
            # so the LLM still gets a structured result rather than a raised
            # exception bubbling out of the tool.
            log.exception("Search backend %s raised unexpectedly", backend.name)
            errors.append(f"{backend.name}: unexpected {type(e).__name__}: {e}")
            continue
    return {
        "error": "all search backends failed",
        "details": errors,
    }


register(Tool(
    name="web_search",
    description="Search the web. Use for current events, facts you don't already know, or to find URLs to fetch.",
    schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query."},
            "n": {"type": "integer", "description": "Number of results (1-10).", "default": 5},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    requires=set(),
    call=_search_call,
))
