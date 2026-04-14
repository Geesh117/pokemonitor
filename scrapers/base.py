"""Base scraper with user-agent rotation, random delays, and proxy support."""

import asyncio
import random
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

import aiohttp
from fake_useragent import UserAgent

from bot.logger_setup import get_logger

log = get_logger(__name__)

# Suppress fake_useragent's verbose warnings
import logging as _logging
_logging.getLogger("fake_useragent").setLevel(_logging.ERROR)

try:
    _UA = UserAgent(browsers=["chrome", "firefox", "edge"], fallback="Mozilla/5.0")
    # Pre-warm the cache to avoid per-call warnings
    _ = _UA.chrome
except Exception:
    _UA = None


def random_user_agent() -> str:
    if _UA:
        try:
            return _UA.random
        except Exception:
            pass
    # Fallback pool
    pool = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_3) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    ]
    return random.choice(pool)


@dataclass
class Product:
    """Normalised product representation returned by every scraper."""
    site_key: str
    site_name: str
    product_id: str          # Unique ID within the site (handle, SKU, ASIN, …)
    title: str
    url: str
    price: Optional[float]   # CAD, None if unknown
    in_stock: bool
    image_url: Optional[str] = None
    quantity: Optional[int] = None   # Units in stock (None = unknown)
    raw: dict = field(default_factory=dict)


def parse_price(raw: str) -> Optional[float]:
    """Extract a float price from a messy string like '$129.99' or '129,99'."""
    if not raw:
        return None
    cleaned = re.sub(r"[^\d.,]", "", str(raw)).replace(",", ".")
    # Handle cases like "129.99.00" — take the first valid float
    parts = cleaned.split(".")
    if len(parts) > 2:
        cleaned = parts[0] + "." + "".join(parts[1:])
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


class BaseScraper:
    """
    Provides an aiohttp session with rotating user-agents, optional proxies,
    and random inter-request delays.
    """

    def __init__(
        self,
        proxies: Optional[list] = None,
        delay_min: float = 3.0,
        delay_max: float = 8.0,
    ):
        self.proxies = proxies or []
        self.delay_min = delay_min
        self.delay_max = delay_max
        self._session: Optional[aiohttp.ClientSession] = None

    def _pick_proxy(self) -> Optional[str]:
        return random.choice(self.proxies) if self.proxies else None

    def _headers(self) -> dict:
        return {
            "User-Agent": random_user_agent(),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-CA,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",  # Brotli package required for br
            "Connection": "keep-alive",
            "DNT": "1",
        }

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(ssl=False, limit=10)
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=30),
            )
        return self._session

    async def _get(
        self,
        url: str,
        params: Optional[dict] = None,
        extra_headers: Optional[dict] = None,
        json: bool = False,
    ):
        """Perform an async GET, returning (status_code, text_or_json)."""
        await asyncio.sleep(random.uniform(self.delay_min, self.delay_max))
        headers = self._headers()
        if extra_headers:
            headers.update(extra_headers)
        proxy = self._pick_proxy()
        session = await self._get_session()
        try:
            async with session.get(
                url, headers=headers, params=params, proxy=proxy, allow_redirects=True
            ) as resp:
                if json:
                    data = await resp.json(content_type=None)
                    return resp.status, data
                text = await resp.text()
                return resp.status, text
        except Exception as exc:
            log.debug("GET %s failed: %s", url, exc)
            return 0, None

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
