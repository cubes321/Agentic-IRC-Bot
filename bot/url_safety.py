"""SSRF protection for tool-driven HTTP fetches.

Background: tools that take a user-supplied URL (fetch_url, look_at_image,
youtube_info) historically used `httpx ... follow_redirects=True` with no
host validation. An LLM-mediated request from any channel user could
therefore target `127.0.0.1`, RFC 1918 ranges, link-local, or cloud
metadata endpoints (`169.254.169.254`) and exfiltrate their contents
back into the channel. The 2026-05-22 security review classified this as
CRITICAL (finding C1).

This module provides the host-validation primitives:

  - is_safe_url(url) -> (ok, reason)
      Verifies the URL's scheme is http/https AND every IP the host
      resolves to is globally routable (per stdlib `ipaddress.is_global`).
      Async because DNS resolution is async.

  - safe_fetch(http, url, ...) -> Response
      Equivalent to `http.request(...)` but follows redirects manually,
      re-validating the URL on every hop. The underlying http client
      gets `follow_redirects=False` regardless of what the caller passes.

  - safe_stream(http, url, ...) -> async context manager
      Same redirect dance, yielding a streamed response. Required for
      large-body tools (vision's 5MB image cap) where buffered fetch
      would defeat memory bounds.

Residual risk we accept:
  - DNS rebinding (host resolves benignly during is_safe_url, then to
    127.0.0.1 during the actual connect): mitigated only by keeping the
    window small (resolve immediately before fetch, no caching). A real
    rebinding attack requires attacker DNS control with very short TTLs;
    unlikely against a casual user, not in v1 scope to fix.
  - HTTP_PROXY env var pointing the http client at a proxy that itself
    resolves internal hostnames: the IP check happens on our side, the
    connect happens through the proxy. Document, don't defend.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import socket
from typing import Any, AsyncIterator
from urllib.parse import urljoin, urlparse

import httpx

log = logging.getLogger(__name__)


class UnsafeURLError(Exception):
    """Raised when a URL targets a non-globally-routable address (or fails
    the safety check for any other reason). Tools catch this and surface
    a user-facing error rather than a stack trace."""


def _classify_ip(ip_str: str) -> tuple[bool, str]:
    """Return (is_safe, reason). The reason is the most specific category
    we can identify — useful for diagnostics and audit logging. We don't
    just check `is_global` because an explicit reason ('loopback',
    'private network', etc.) is more informative for operators reading
    error messages."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False, f"not a valid IP literal: {ip_str!r}"
    # Order matters: most specific reasons first so the error message
    # picks the strongest categorical match.
    if ip.is_unspecified:
        return False, f"unspecified address ({ip_str})"
    if ip.is_loopback:
        return False, f"loopback address ({ip_str})"
    if ip.is_link_local:
        return False, f"link-local address ({ip_str})"
    if ip.is_multicast:
        return False, f"multicast address ({ip_str})"
    if ip.is_reserved:
        return False, f"reserved range ({ip_str})"
    if ip.is_private:
        # `is_private` covers RFC 1918, RFC 4193, and other "not globally
        # routable" ranges. Comes after the explicit loopback/link-local
        # checks so we report the more specific reason when applicable.
        return False, f"private network address ({ip_str})"
    if not ip.is_global:
        # Catch-all for anything else stdlib flags as non-global.
        return False, f"non-globally-routable address ({ip_str})"
    return True, ""


async def is_safe_url(url: str) -> tuple[bool, str]:
    """Return (ok, reason). Validates the URL's scheme and the host's
    resolved IPs. Every IP the host resolves to must pass `_classify_ip`;
    a single bad IP fails the whole check. This guards against split-DNS
    and multi-A-record attacks where one record points internally."""
    try:
        parsed = urlparse(url)
    except Exception as e:
        return False, f"could not parse URL: {e}"

    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        return False, f"unsupported scheme: {scheme!r} (only http/https allowed)"

    host = parsed.hostname
    if not host:
        return False, "URL has no hostname"

    # If the host is already an IP literal, skip DNS entirely. The
    # `ipaddress` module handles both v4 and v6 (including bracketed
    # forms via .hostname which strips brackets for us).
    try:
        # Reject IP literals based on their classification directly.
        ip = ipaddress.ip_address(host)
        return _classify_ip(str(ip))
    except ValueError:
        # Not an IP literal — fall through to DNS resolution.
        pass

    # Async DNS resolution via the running loop's getaddrinfo.
    try:
        loop = asyncio.get_running_loop()
        # type=SOCK_STREAM filters to TCP records; we don't care about UDP-only A/AAAA entries.
        addrinfos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        return False, f"DNS resolution of {host!r} failed: {e}"
    except Exception as e:
        log.exception("getaddrinfo failure for %r", host)
        return False, f"DNS lookup error for {host!r}: {e}"

    if not addrinfos:
        return False, f"DNS resolution of {host!r} returned no addresses"

    # Reject if ANY resolved address is unsafe. Defense vs. attackers who
    # control a hostname pointing some A records public and some internal.
    for info in addrinfos:
        # addrinfo tuple: (family, type, proto, canonname, sockaddr)
        # sockaddr is (host, port) for v4 or (host, port, flow, scope) for v6.
        sockaddr = info[4]
        ip_str = sockaddr[0]
        ok, reason = _classify_ip(ip_str)
        if not ok:
            return False, f"host {host!r} resolves to unsafe address: {reason}"

    return True, ""


# ---- safe_fetch (buffered) -------------------------------------------------

# Cap on manual redirect chain. httpx's default is 20; for a tool fetching
# external content there's no good reason to chase more than 5 hops, and a
# tight cap also bounds the time spent on a redirect loop attack.
MAX_REDIRECTS = 5


async def safe_fetch(
    http: httpx.AsyncClient,
    url: str,
    *,
    method: str = "GET",
    max_redirects: int = MAX_REDIRECTS,
    **kwargs: Any,
) -> httpx.Response:
    """Like `http.request(method, url, ...)` but follows redirects manually,
    re-running `is_safe_url` on every hop. Raises UnsafeURLError on a
    failed safety check at any hop. Any caller-supplied `follow_redirects`
    is ignored — this function controls redirect behaviour."""
    # Strip the kwarg if the caller passed it; we always disable httpx's
    # built-in redirect following.
    kwargs.pop("follow_redirects", None)
    current_url = url
    for hop in range(max_redirects + 1):
        ok, reason = await is_safe_url(current_url)
        if not ok:
            raise UnsafeURLError(
                f"refusing to fetch {current_url!r}: {reason}"
            )
        r = await http.request(method, current_url, follow_redirects=False, **kwargs)
        if not r.is_redirect:
            return r
        # Follow the redirect manually after re-checking on next iteration.
        loc = r.headers.get("Location")
        if not loc:
            # Redirect status but no Location header — return the
            # response unchanged. Tools can inspect r.status_code.
            return r
        # Compose absolute URL from possibly-relative Location.
        current_url = urljoin(current_url, loc)
    raise UnsafeURLError(
        f"too many redirects ({max_redirects}) starting from {url!r}"
    )


# ---- safe_stream (streaming) -----------------------------------------------


@contextlib.asynccontextmanager
async def safe_stream(
    http: httpx.AsyncClient,
    url: str,
    *,
    method: str = "GET",
    max_redirects: int = MAX_REDIRECTS,
    **kwargs: Any,
) -> AsyncIterator[httpx.Response]:
    """Async context manager yielding a streamed response. Same redirect
    safety as safe_fetch, but the final response body remains a stream
    so the caller can enforce its own size cap or content-type checks
    without buffering the whole body first.

    Implementation: open `http.stream(...)` with follow_redirects=False;
    if the response is a redirect, exit that context cleanly and reopen
    against the next URL. Loops until a non-redirect lands or we hit
    max_redirects."""
    kwargs.pop("follow_redirects", None)
    current_url = url
    last_response: httpx.Response | None = None
    for hop in range(max_redirects + 1):
        ok, reason = await is_safe_url(current_url)
        if not ok:
            raise UnsafeURLError(
                f"refusing to fetch {current_url!r}: {reason}"
            )
        cm = http.stream(method, current_url, follow_redirects=False, **kwargs)
        response = await cm.__aenter__()
        try:
            if not response.is_redirect:
                # Yield the response. The caller's `async with` block runs
                # while we hold the stream open; we exit it after they're done.
                yield response
                return
            # Redirect — pull Location, exit this stream cleanly, loop.
            loc = response.headers.get("Location")
            last_response = response
        except BaseException:
            # Exception in caller's with-body or in our own code: close
            # the stream and propagate.
            await cm.__aexit__(None, None, None)
            raise
        await cm.__aexit__(None, None, None)
        if not loc:
            # Mid-chain redirect with no Location: we have no next URL.
            raise UnsafeURLError(
                f"redirect from {current_url!r} had no Location header"
            )
        current_url = urljoin(current_url, loc)
    raise UnsafeURLError(
        f"too many redirects ({max_redirects}) starting from {url!r}"
    )
