"""URL verification — confirms a URL returns HTTP 200 before alert is sent."""

import asyncio
from typing import Optional

import aiohttp

from bot.logger_setup import get_logger

log = get_logger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    )
}


async def verify_url(url: str, session: Optional[aiohttp.ClientSession] = None) -> bool:
    """Return True only if the URL responds with HTTP 200."""
    close_after = session is None
    if session is None:
        session = aiohttp.ClientSession(
            headers=_HEADERS,
            timeout=aiohttp.ClientTimeout(total=10),
        )
    try:
        async with session.head(url, allow_redirects=True) as resp:
            if resp.status == 200:
                return True
            # Some servers reject HEAD; fall back to GET
            if resp.status in (405, 403):
                async with session.get(url, allow_redirects=True) as gresp:
                    return gresp.status == 200
            log.debug("URL check %s → %s", url, resp.status)
            return False
    except Exception as exc:
        log.debug("URL verify failed for %s: %s", url, exc)
        return False
    finally:
        if close_after:
            await session.close()
