"""Vision tool: fetch an image URL, run it through the vision model, return
a description.

Capability-gated: registered with `requires={"vision"}`, so the tool only
appears in the catalog when `[ai].vision_model` is set in the config. If
the vision model isn't actually loaded in LM Studio at call time, the tool
returns a structured `vision_disabled` result rather than raising, so the
LLM can tell the user gracefully ('I'd look at the image but the vision
model isn't available right now')."""

from __future__ import annotations

import asyncio
import base64
import logging

import httpx
from openai import APIError, APIConnectionError, NotFoundError, BadRequestError

from . import Tool, ToolContext, register

log = logging.getLogger(__name__)


# Image-fetching limits. The point of validating content-type upfront is
# refusing to base64-encode a 50MB PDF or HTML error page that just happens
# to live at an `.png` URL.
HTTP_TIMEOUT = 15.0
MAX_IMAGE_BYTES = 5 * 1024 * 1024   # 5 MB; ~big enough for any real chat image
USER_AGENT = "AgenticIRCBot/0.1 (image fetch for vision)"
ALLOWED_CONTENT_TYPES = {
    "image/jpeg", "image/jpg", "image/png", "image/gif", "image/webp",
}
# Some servers don't set the right content-type for .jpg URLs. Map common
# file extensions to a best-guess content-type as fallback. Conservative —
# we'd rather refuse a mislabeled file than try to feed it to the model.
EXT_TO_TYPE = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


async def _fetch_image(http: httpx.AsyncClient, url: str) -> tuple[bytes, str] | dict:
    """Fetch and validate an image URL. Returns (bytes, content_type) on
    success, or a dict containing {'error': ...} on validation failure.

    Mixing return types lets the caller short-circuit cleanly without an
    exception ladder for what are really just user-input errors."""
    if not url.startswith(("http://", "https://")):
        return {"error": "image_url must start with http:// or https://"}
    try:
        # Streamed fetch so we can abort mid-download if the response is huge.
        async with http.stream(
            "GET", url,
            timeout=HTTP_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept": "image/*"},
        ) as r:
            if r.status_code >= 400:
                return {"error": f"HTTP {r.status_code} fetching image"}
            ctype = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
            # First try the declared content-type. If unhelpful, fall back to
            # the URL's file extension before refusing.
            if ctype not in ALLOWED_CONTENT_TYPES:
                from urllib.parse import urlparse
                path = urlparse(url).path.lower()
                ext_guess = next(
                    (t for ext, t in EXT_TO_TYPE.items() if path.endswith(ext)),
                    None,
                )
                if ext_guess is None:
                    return {
                        "error": (
                            f"unsupported content-type: {ctype!r}. "
                            "Supported: JPEG, PNG, GIF, WebP."
                        ),
                    }
                ctype = ext_guess

            # Read with a hard byte cap. We sum chunk sizes as we go and
            # abort early if we'd exceed MAX_IMAGE_BYTES — never load the
            # whole giant response into memory.
            chunks: list[bytes] = []
            total = 0
            async for chunk in r.aiter_bytes():
                total += len(chunk)
                if total > MAX_IMAGE_BYTES:
                    return {
                        "error": (
                            f"image too large (>{MAX_IMAGE_BYTES // (1024*1024)} MB). "
                            "Ask the user for a smaller image or a thumbnail URL."
                        ),
                    }
                chunks.append(chunk)
            return b"".join(chunks), ctype
    except (httpx.RequestError, asyncio.TimeoutError) as e:
        return {"error": f"network error fetching image: {e}"}


async def _look_at_image(ctx: ToolContext, args: dict) -> dict:
    image_url = (args.get("image_url") or "").strip()
    prompt = (args.get("prompt") or "").strip()
    if not image_url:
        return {"error": "image_url is empty"}
    if not prompt:
        # Default prompt rather than refusing — common case is "what is this?"
        prompt = "Describe what you see in this image."

    fetched = await _fetch_image(ctx.http, image_url)
    if isinstance(fetched, dict):
        # _fetch_image returned an error dict; pass it through.
        return fetched
    image_bytes, ctype = fetched

    # Build the OpenAI vision multimodal message format. LM Studio's vision
    # endpoint accepts this same shape. We pass the image as a base64 data
    # URL rather than the original URL so:
    #   1. The vision model server doesn't have to fetch the network itself
    #      (one less failure mode, and works behind firewalls).
    #   2. Content-type validation already ran on OUR side.
    b64 = base64.b64encode(image_bytes).decode("ascii")
    data_url = f"data:{ctype};base64,{b64}"

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }
    ]

    try:
        resp = await ctx.llm_vision.chat.completions.create(
            model=ctx.cfg.ai.vision_model,
            messages=messages,
            timeout=float(ctx.cfg.budgets.llm_call_timeout_sec),
        )
    except NotFoundError as e:
        # LM Studio raises NotFoundError when the vision model isn't loaded.
        # Surface as a structured 'disabled' result so the LLM can explain
        # rather than crashing the agent loop.
        log.warning("look_at_image: vision model not found: %s", e)
        return {
            "status": "vision_disabled",
            "reason": (
                f"vision model {ctx.cfg.ai.vision_model!r} is not loaded "
                "in LM Studio right now"
            ),
        }
    except BadRequestError as e:
        # Most common cause: the loaded model doesn't accept images (e.g. a
        # text-only model was set as vision_model by mistake).
        log.warning("look_at_image: bad request from vision model: %s", e)
        return {
            "status": "vision_disabled",
            "reason": (
                f"vision call rejected by model: {e}. The configured "
                "vision_model may not support images."
            ),
        }
    except (APIError, APIConnectionError, asyncio.TimeoutError) as e:
        log.exception("look_at_image: API/connection error")
        return {"error": f"vision call failed: {e}"}
    except Exception as e:
        log.exception("look_at_image: unexpected error")
        return {"error": f"unexpected error: {e}"}

    # Token usage logging — same pattern as the agent / tick. Uses a distinct
    # purpose so the on-exit summary attributes vision spend separately.
    try:
        usage = getattr(resp, "usage", None)
        if usage is not None:
            await ctx.db.log_usage(
                model=ctx.cfg.ai.vision_model,
                prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                purpose="vision",
                channel=ctx.channel,
            )
    except Exception:
        log.exception("look_at_image: usage logging failed")

    text = (resp.choices[0].message.content or "").strip()
    if not text:
        return {
            "status": "vision_disabled",
            "reason": "vision model returned empty content",
        }
    return {
        "image_url": image_url,
        "content_type": ctype,
        "size_bytes": len(image_bytes),
        "model": ctx.cfg.ai.vision_model,
        "description": text,
    }


register(Tool(
    name="look_at_image",
    description=(
        "Look at an image URL and describe what you see. Use when someone "
        "posts an image link and asks about it, or when you need to "
        "understand the contents of an image to answer a question. Supports "
        "JPEG, PNG, GIF, WebP up to 5 MB. Returns a description from the "
        "vision model. If the vision model is not loaded, returns "
        "status='vision_disabled' with a reason — explain to the user "
        "rather than retrying."
    ),
    schema={
        "type": "object",
        "properties": {
            "image_url": {
                "type": "string",
                "description": "Full URL of the image (http:// or https://).",
            },
            "prompt": {
                "type": "string",
                "description": (
                    "What to look at or describe. Default: 'Describe what you "
                    "see in this image.' Use a specific prompt for focused "
                    "questions like 'What model of car is this?' or "
                    "'Transcribe any text visible in the image.'"
                ),
            },
        },
        "required": ["image_url"],
        "additionalProperties": False,
    },
    requires={"vision"},
    call=_look_at_image,
))
