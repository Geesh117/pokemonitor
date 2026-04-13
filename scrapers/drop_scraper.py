"""
Drop intelligence scrapers.

Sources:
  - StockTrack.ca — Costco / Walmart Canada real-time in-store & online stock
"""

import asyncio
import hashlib
import random
import re
from typing import List, Optional

from bs4 import BeautifulSoup

from scrapers.base import BaseScraper, Product
from bot.logger_setup import get_logger

log = get_logger(__name__)

# Canadian store regions within ~2 h of Milton, ON
GTA_LOCATIONS = [
    "toronto", "mississauga", "brampton", "oakville", "burlington",
    "hamilton", "vaughan", "richmond hill", "markham", "scarborough",
    "etobicoke", "newmarket", "aurora", "barrie", "guelph",
    "kitchener", "waterloo", "cambridge", "brantford", "pickering",
    "ajax", "whitby", "oshawa", "durham", "milton", "georgetown",
    "halton", "woodbridge", "thornhill", "concord", "king city",
    "orangeville", "ontario", "canada",
]

TCG_TERMS = ["pokemon", "pokémon", "one piece", "onepiece"]


def _make_product_id(name: str, retailer: str) -> str:
    raw = f"{name.lower().strip()}|{retailer.lower().strip()}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def _detect_location(text_lower: str) -> str:
    for loc in GTA_LOCATIONS:
        if loc in text_lower:
            return loc.title()
    return "Online"


def _detect_retailer(text_lower: str) -> str:
    if "walmart" in text_lower:
        return "Walmart Canada"
    if "amazon" in text_lower:
        return "Amazon Canada"
    if "best buy" in text_lower:
        return "Best Buy Canada"
    if "indigo" in text_lower or "chapters" in text_lower:
        return "Indigo/Chapters"
    return "Costco Canada"


class StockTrackScraper(BaseScraper):
    """
    Scrapes stocktrack.ca for Costco / Walmart Canada TCG product stock.

    Returns standard Product objects so the existing DB change-detection
    (is_new / stock_changed / price_changed) works transparently.
    """

    # Each tuple: (query label, search URL)
    BASE = "https://stocktrack.ca/cgi-bin/item.cgi?q={q}&st=1&lang=en"
    SEARCHES = [
        # Broad sweeps — catch anything Pokemon/One Piece at Costco/Walmart
        ("pokemon", BASE.format(q="pokemon+tcg")),
        ("pokemon booster box", BASE.format(q="pokemon+booster+box")),
        ("pokemon elite trainer", BASE.format(q="pokemon+elite+trainer")),
        ("pokemon ultra premium", BASE.format(q="pokemon+ultra+premium")),
        ("pokemon premium collection", BASE.format(q="pokemon+premium+collection")),
        ("pokemon special collection", BASE.format(q="pokemon+special+collection")),
        ("one piece card game", BASE.format(q="one+piece+card+game")),
    ]

    async def scrape_all(self, site_key: str, site_name: str) -> List[Product]:
        products: List[Product] = []
        seen_ids: set = set()

        for label, url in self.SEARCHES:
            batch = await self._scrape_search(url, label, site_key, site_name)
            for p in batch:
                if p.product_id not in seen_ids:
                    seen_ids.add(p.product_id)
                    products.append(p)
            await asyncio.sleep(random.uniform(5, 10))

        log.info("StockTrack: %d unique products across all queries", len(products))
        return products

    async def _scrape_search(
        self, url: str, label: str, site_key: str, site_name: str
    ) -> List[Product]:
        status, html = await self._get(url, extra_headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": "https://stocktrack.ca/",
            "Accept-Language": "en-CA,en;q=0.9",
        })

        if not html:
            log.warning("StockTrack '%s': empty response (status %s)", label, status)
            return []

        products: List[Product] = []
        try:
            soup = BeautifulSoup(html, "lxml")
            rows = soup.find_all("tr")

            if not rows:
                log.info(
                    "StockTrack '%s': no <tr> elements found. "
                    "HTML preview (first 2000 chars): %s",
                    label, html[:2000],
                )
                return []

            for row in rows:
                text = row.get_text(" ", strip=True)
                if len(text) < 10:
                    continue

                text_lower = text.lower()

                # Must be a TCG product
                if not any(g in text_lower for g in TCG_TERMS):
                    continue

                # Must have a price — rows without prices are headers / metadata
                price_match = re.search(r'\$\s*(\d+(?:\.\d{2})?)', text)
                if not price_match:
                    continue

                # Product name from first link, fallback to first cell
                link_el = row.find("a", href=True)
                if link_el:
                    product_name = link_el.get_text(strip=True)
                    raw_href = link_el["href"]
                    href = raw_href if raw_href.startswith("http") else f"https://stocktrack.ca{raw_href}"
                else:
                    tds = row.find_all("td")
                    product_name = tds[0].get_text(strip=True) if tds else text[:80]
                    href = url

                if not product_name or len(product_name) < 4:
                    continue

                try:
                    price = float(price_match.group(1))
                except (ValueError, IndexError):
                    price = None

                retailer = _detect_retailer(text_lower)
                location = _detect_location(text_lower)

                # In-stock detection
                positive_kws = ["in stock", "available", "add to cart", "buy now", "yes", "✓"]
                negative_kws = ["out of stock", "unavailable", "sold out", "no stock", "✗", "no"]
                in_stock = any(kw in text_lower for kw in positive_kws) and \
                           not any(kw in text_lower for kw in negative_kws)

                # Build descriptive title: "Product Name @ Costco Canada [Mississauga]"
                loc_tag = f" [{location}]" if location not in ("Online", "Ontario", "Canada") else ""
                full_title = f"{product_name} @ {retailer}{loc_tag}"
                pid = _make_product_id(product_name, retailer + location)

                products.append(Product(
                    site_key=site_key,
                    site_name=site_name,
                    product_id=pid,
                    title=full_title,
                    url=href,
                    price=price,
                    in_stock=in_stock,
                    raw={"retailer": retailer, "location": location, "raw_text": text[:400]},
                ))

            log.info("StockTrack '%s': parsed %d products", label, len(products))

        except Exception as exc:
            log.error("StockTrack parse error for '%s': %s", label, exc)

        return products


# ------------------------------------------------------------------ #
# NowinStock.net (Amazon CA / Best Buy CA aggregator)                 #
# ------------------------------------------------------------------ #

NOWINSTOCK_SEARCHES = [
    ("pokemon ca", "https://www.nowinstock.net/search/?q=pokemon+booster&country=ca"),
    ("one piece ca", "https://www.nowinstock.net/search/?q=one+piece+card+game&country=ca"),
]


class NowinStockScraper(BaseScraper):
    """
    Scrapes nowinstock.net for Amazon CA / Best Buy CA TCG product availability.
    Returns Product objects for the existing DB change-detection pipeline.
    """

    async def scrape_all(self, site_key: str, site_name: str) -> List[Product]:
        products: List[Product] = []
        seen_ids: set = set()

        for label, url in NOWINSTOCK_SEARCHES:
            batch = await self._scrape_page(url, label, site_key, site_name)
            for p in batch:
                if p.product_id not in seen_ids:
                    seen_ids.add(p.product_id)
                    products.append(p)
            await asyncio.sleep(random.uniform(3, 6))

        log.info("NowinStock: %d unique products", len(products))
        return products

    async def _scrape_page(self, url: str, label: str, site_key: str, site_name: str) -> List[Product]:
        status, html = await self._get(url, extra_headers={
            "Accept": "text/html,application/xhtml+xml",
            "Referer": "https://www.nowinstock.net/",
        })
        if not html:
            log.warning("NowinStock: empty response for '%s'", label)
            return []

        products: List[Product] = []
        try:
            soup = BeautifulSoup(html, "lxml")

            # NowinStock uses a table or list of tracked items
            rows = (
                soup.select("tr.product, tr.item, .product-row") or
                soup.select("table tr") or
                soup.select(".product-listing, .item-listing, .tracked-item")
            )

            if not rows:
                log.info("NowinStock '%s': no rows found. HTML[:1500]: %s", label, html[:1500])
                return []

            for row in rows:
                text = row.get_text(" ", strip=True)
                if len(text) < 5:
                    continue
                text_lower = text.lower()
                if not any(g in text_lower for g in TCG_TERMS):
                    continue

                link_el = row.find("a", href=True)
                href = link_el.get("href", "") if link_el else ""
                if href and not href.startswith("http"):
                    href = "https://www.nowinstock.net" + href
                product_name = link_el.get_text(strip=True) if link_el else text[:80]

                # Retailer from link or text
                retailer = "Unknown"
                for r_name in ["amazon", "best buy", "bestbuy", "walmart", "costco"]:
                    if r_name in text_lower:
                        retailer = r_name.title().replace("Bestbuy", "Best Buy")
                        break

                # In-stock indicator
                in_stock = any(kw in text_lower for kw in ["in stock", "available", "yes"]) and \
                           not any(kw in text_lower for kw in ["out of stock", "unavailable"])

                price_match = re.search(r'\$(\d+(?:\.\d{2})?)', text)
                price = float(price_match.group(1)) if price_match else None

                pid = _make_product_id(product_name, retailer)

                products.append(Product(
                    site_key=site_key,
                    site_name=site_name,
                    product_id=pid,
                    title=f"{product_name} @ {retailer}",
                    url=href or url,
                    price=price,
                    in_stock=in_stock,
                    raw={"retailer": retailer, "location": "Online"},
                ))

            log.info("NowinStock '%s': %d products", label, len(products))
        except Exception as exc:
            log.error("NowinStock parse error for '%s': %s", label, exc)

        return products
