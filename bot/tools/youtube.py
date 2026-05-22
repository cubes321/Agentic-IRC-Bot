"""YouTube metadata via yt-dlp. Ported from the legacy script's
get_youtube_video_info(); same options, same output shape.

Host-restricted (security review finding C1, 2026-05-22): the tool
description says "YouTube" but the underlying yt-dlp generic extractor
will probe arbitrary URLs trying to find video data. Letting it accept
any URL turned yt-dlp into a third SSRF surface alongside fetch_url and
look_at_image. We now pre-validate the host against an allowlist of
YouTube domains before handing off to yt-dlp."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from urllib.parse import urlparse

from . import Tool, ToolContext, register

log = logging.getLogger(__name__)


# Allowlist of host suffixes accepted by youtube_info. We match by
# suffix (so `m.youtube.com` and `www.music.youtube.com` both pass)
# rather than exact host equality. Includes youtube-nocookie.com which
# is the privacy-mode CDN YouTube uses for embedded players — same
# content backend, distinct hostname, legitimate metadata source.
_YOUTUBE_HOST_SUFFIXES = (
    "youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
)


def _is_youtube_url(url: str) -> tuple[bool, str]:
    """Return (ok, reason). True if the URL's host is in the YouTube
    allowlist; False with a human-readable reason otherwise."""
    try:
        parsed = urlparse(url)
    except Exception as e:
        return False, f"could not parse URL: {e}"
    if parsed.scheme not in ("http", "https"):
        return False, f"unsupported scheme: {parsed.scheme!r}"
    host = (parsed.hostname or "").lower()
    if not host:
        return False, "URL has no hostname"
    for suffix in _YOUTUBE_HOST_SUFFIXES:
        # Exact match OR subdomain (host ends with .suffix). Plain
        # `endswith(suffix)` would falsely accept "evil-youtube.com",
        # so we require the boundary "." or full equality.
        if host == suffix or host.endswith("." + suffix):
            return True, ""
    return False, (
        f"host {host!r} is not a YouTube domain "
        f"(accepted suffixes: {', '.join(_YOUTUBE_HOST_SUFFIXES)})"
    )


def _fetch_sync(url: str) -> dict:
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        return {"error": f"yt-dlp: {e}"}
    if not info:
        return {"error": "no info returned"}
    duration = info.get("duration") or 0
    if duration:
        duration_str = str(dt.timedelta(seconds=int(duration)))
    else:
        duration_str = "unknown (possibly live)"
    return {
        "title": info.get("title", "unknown"),
        "uploader": info.get("uploader", "unknown"),
        "duration": duration_str,
        "view_count": info.get("view_count"),
        "upload_date": info.get("upload_date"),
    }


async def _youtube_info(ctx: ToolContext, args: dict) -> dict:
    url = args["url"]
    if not url.startswith(("http://", "https://")):
        return {"error": "url must start with http:// or https://"}
    ok, reason = _is_youtube_url(url)
    if not ok:
        return {"error": f"refusing to fetch: {reason}"}
    return await asyncio.to_thread(_fetch_sync, url)


register(Tool(
    name="youtube_info",
    description="Get metadata about a YouTube video (title, uploader, duration). Use this when a YouTube URL is mentioned.",
    schema={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Full YouTube URL."},
        },
        "required": ["url"],
        "additionalProperties": False,
    },
    requires=set(),
    call=_youtube_info,
))
