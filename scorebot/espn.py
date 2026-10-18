"""One polite way to call ESPN's public JSON (scores, injuries, box scores).

ESPN sits behind a CDN that answers 403 to requests that don't look like a
browser and to bursts of calls. So every ESPN request here:
  - sends normal browser headers,
  - waits a moment between calls,
  - retries once after a pause on 403/429,
  - backs off for a few minutes after repeated 403s instead of hammering.
"""
from __future__ import annotations

import asyncio
import logging
import time

import aiohttp

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.espn.com/",
    "Origin": "https://www.espn.com",
}
MIN_GAP_S = 0.6          # spacing between ESPN calls
BACKOFF_S = 300          # pause after repeated 403s

_lock = asyncio.Lock()
_last_call = 0.0
_blocked_until = 0.0


class EspnBlocked(Exception):
    pass


async def get_json(session: aiohttp.ClientSession, url: str, params: dict | None = None,
                   timeout: float = 20) -> dict:
    global _last_call, _blocked_until
    if time.time() < _blocked_until:
        raise EspnBlocked(f"ESPN backing off for {int(_blocked_until - time.time())}s after 403s")
    for attempt in (1, 2):
        async with _lock:
            wait = MIN_GAP_S - (time.time() - _last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            _last_call = time.time()
        async with session.get(url, params=params, headers=HEADERS,
                               timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            if r.status in (403, 429) and attempt == 1:
                await asyncio.sleep(3)
                continue
            if r.status in (403, 429):
                _blocked_until = time.time() + BACKOFF_S
                raise EspnBlocked(f"ESPN returned {r.status} for {url}")
            r.raise_for_status()
            return await r.json(content_type=None)
    raise EspnBlocked("unreachable")
