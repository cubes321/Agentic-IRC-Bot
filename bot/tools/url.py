"""Fetch a URL and return readable text. Generalises the old !tldr command."""

from __future__ import annotations

import asyncio
import logging

import httpx

from . import Tool, ToolContext, register

log = logging.getLogger(__name__)

MAX_CHARS = 4000
USER_AGENT = "Mozilla/5.0 (compatible; AgenticIRCBot/0.1)"


def _extract_readable(html: str) -> tuple[str, str]:
    """Return (title, text). Falls back to BeautifulSoup if readability is missing."""
    try:
        from readability import Document
        doc = Document(html)
        title = (doc.short_title() or "").strip()
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(doc.summary(), "lxml")
            text = soup.get_text(separator=" ", strip=True)
        except Exception:
            text = doc.summary()
        return title, text
    except Exception:
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "lxml")
            title = (soup.title.string if soup.title else "") or ""
            text = soup.get_text(separator=" ", strip=True)
            return title.strip(), text
        except Exception as e:
            log.warning("HTML extraction failed: %s", e)
            return "", html[:MAX_CHARS]


async def _fetch_url(ctx: ToolContext, args: dict) -> dict:
    url = args["url"]
    if not url.startswith(("http://", "https://")):
        return {"error": "url must start with http:// or https://"}
    try:
        r = await ctx.http.get(
            url,
            timeout=15.0,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.5"},
        )
    except (httpx.RequestError, asyncio.TimeoutError) as e:
        return {"error": f"network error: {e}"}
    if r.status_code >= 400:
        return {"error": f"HTTP {r.status_code}", "url": url}
    ctype = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
    if ctype and not (ctype.startswith("text/") or ctype == "application/xhtml+xml"):
        return {"error": f"unsupported content-type: {ctype}", "url": url}

    html = r.text
    title, text = _extract_readable(html)
    text = text.strip()
    truncated = len(text) > MAX_CHARS
    return {
        "url": str(r.url),
        "title": title,
        "text": text[:MAX_CHARS],
        "truncated": truncated,
    }


register(Tool(
    name="fetch_url",
    description="Fetch a web page and return its readable text. Use to read the contents of a URL someone posted, or one you found via web_search.",
    schema={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Full URL including scheme."},
        },
        "required": ["url"],
        "additionalProperties": False,
    },
    requires=set(),
    call=_fetch_url,
))
