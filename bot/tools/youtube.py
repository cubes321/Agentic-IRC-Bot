"""YouTube metadata via yt-dlp. Ported from the legacy script's
get_youtube_video_info(); same options, same output shape."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

from . import Tool, ToolContext, register

log = logging.getLogger(__name__)


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
