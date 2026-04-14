"""Lightweight TCGPlayer market price lookup — best effort, fails gracefully."""

import asyncio
import json
import re
import time
from typing import Optional

import aiohttp

from bot.logger_setup import get_logger

log = get_logger(__name__)

# In-memory cache: query_key -> (price_usd, fetched_timestamp)
_cache: dict = {}
_CACHE_TTL = 3600  # 1 hour


async def fetch_tcgplayer_price(title: str, usd_cad_rate: float = 1.39) -> Optional[float]:
    """
    Search TCGPlayer for a sealed product and return its market price in CAD.
    Returns None if lookup fails or product not found. Times out after 8 seconds.
    """
    key = title.lower()[:80]
    now = time.time()

    if key in _cache:
        price_usd, fetched = _cache[key]
        if now - fetched < _CACHE_TTL:
            return round(price_usd * usd_cad_rate, 2) if price_usd else None

    # Clean query
    query = key
    for word in ["canada", "canadian", "- pokemon", "pokemon tcg", "pokémon tcg", "sealed", "(ca)"]:
        query = query.replace(word, "").strip()
    query = re.sub(r"\s+", " ", query).strip()

    try:
        url = "https://www.tcgplayer.com/search/pokemon/product"
        params = {
            "q": query,
            "productLineName": "pokemon",
            "view": "grid",
            "inStockOnly": "false",
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
            async with session.get(url, params=params, headers=headers) as resp:
                if resp.status != 200:
                    _cache[key] = (None, now)
                    return None
                html = await resp.text()

        # TCGPlayer embeds search data in __NEXT_DATA__ JSON
        match = re.search(
            r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
            html, re.DOTALL,
        )
        if not match:
            _cache[key] = (None, now)
            return None

        data = json.loads(match.group(1))
        props = data.get("props", {}).get("pageProps", {})
        results = props.get("searchResults", props.get("listingResults", {}))
        products = results.get("results", [])

        if not products:
            _cache[key] = (None, now)
            return None

        first = products[0]
        market_price = (
            first.get("marketPrice")
            or first.get("lowestPrice")
            or first.get("lowestListingPrice")
        )
        if market_price:
            price_usd = float(market_price)
            _cache[key] = (price_usd, now)
            return round(price_usd * usd_cad_rate, 2)

    except asyncio.TimeoutError:
        log.debug("TCGPlayer price lookup timed out: %s", title)
    except Exception as exc:
        log.debug("TCGPlayer price lookup failed for %s: %s", title, exc)

    _cache[key] = (None, now)
    return None
