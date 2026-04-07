"""
Playwright-based scraper for JS-heavy sites:
Walmart CA, Costco CA, Amazon CA, Indigo/Chapters, Pokemon Center CA.

Uses playwright-stealth to reduce detection.
Handles cookie consent popups automatically.
"""

import asyncio
import random
import re
from typing import List, Optional
from urllib.parse import urlparse, urlencode, parse_qs, urljoin

from scrapers.base import Product, parse_price, random_user_agent
from bot.logger_setup import get_logger

log = get_logger(__name__)

try:
    from playwright.async_api import async_playwright, Page, BrowserContext
    from playwright_stealth import Stealth as _Stealth
    _stealth = _Stealth()

    async def stealth_async(page):
        await _stealth.apply_stealth_async(page)

    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    log.warning("Playwright or playwright-stealth not installed. JS-heavy scrapers disabled.")


# ------------------------------------------------------------------ #
# Shared Playwright browser pool                                       #
# ------------------------------------------------------------------ #

_browser = None
_playwright_instance = None
_lock = asyncio.Lock()


async def get_browser():
    global _browser, _playwright_instance
    async with _lock:
        if _browser is None or not _browser.is_connected():
            if _playwright_instance:
                await _playwright_instance.stop()
            _playwright_instance = await async_playwright().start()
            _browser = await asyncio.wait_for(
                _playwright_instance.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-blink-features=AutomationControlled",
                        "--disable-infobars",
                        "--disable-dev-shm-usage",
                        "--disable-extensions",
                        "--window-size=1920,1080",
                        "--disable-http2",                  # avoid ERR_HTTP2_PROTOCOL_ERROR
                        "--ignore-certificate-errors",
                        "--disable-background-networking",
                    ],
                ),
                timeout=30.0,
            )
            log.info("Playwright browser launched")
    return _browser


async def close_browser():
    global _browser, _playwright_instance
    if _browser:
        await _browser.close()
        _browser = None
    if _playwright_instance:
        await _playwright_instance.stop()
        _playwright_instance = None


async def new_context(proxies: Optional[list] = None) -> BrowserContext:
    browser = await get_browser()
    proxy_settings = None
    if proxies:
        proxy_url = random.choice(proxies)
        proxy_settings = {"server": proxy_url}

    context = await browser.new_context(
        user_agent=random_user_agent(),
        locale="en-CA",
        timezone_id="America/Toronto",
        viewport={"width": 1920, "height": 1080},
        proxy=proxy_settings,
        extra_http_headers={
            "Accept-Language": "en-CA,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
        },
    )
    return context


async def _dismiss_consent(page: Page):
    """Try to click common cookie consent / overlay buttons."""
    selectors = [
        "button[id*='accept']", "button[id*='cookie']", "button[class*='accept']",
        "button[class*='consent']", "#onetrust-accept-btn-handler",
        ".cookie-accept", "[aria-label*='Accept']", "button:has-text('Accept')",
        "button:has-text('Accept All')", "button:has-text('I Accept')",
        "button:has-text('Agree')", "button:has-text('Got it')",
    ]
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=1000):
                await btn.click()
                await page.wait_for_timeout(500)
                return
        except Exception:
            continue


# ------------------------------------------------------------------ #
# Walmart Canada                                                       #
# ------------------------------------------------------------------ #

class WalmartScraper:
    def __init__(self, proxies=None):
        self.proxies = proxies or []

    async def scrape_url(self, url: str, site_key: str, site_name: str) -> List[Product]:
        if not PLAYWRIGHT_AVAILABLE:
            return []
        products = []
        context = await new_context(self.proxies)
        page = await context.new_page()
        try:
            await stealth_async(page)
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await _dismiss_consent(page)
            await page.wait_for_timeout(random.randint(2000, 4000))

            # Scroll to load lazy products
            for _ in range(3):
                await page.keyboard.press("End")
                await page.wait_for_timeout(1500)

            # Try Walmart's internal JSON API first
            # Products appear as JSON in <script type="application/json" id="__NEXT_DATA__">
            next_data = await page.evaluate("""
                () => {
                    const el = document.getElementById('__NEXT_DATA__');
                    return el ? el.textContent : null;
                }
            """)

            if next_data:
                import json
                try:
                    data = json.loads(next_data)
                    # Navigate the nested structure
                    items = _extract_walmart_products(data)
                    for item in items:
                        products.append(Product(
                            site_key=site_key,
                            site_name=site_name,
                            product_id=item.get("id", item.get("usItemId", "")),
                            title=item.get("name", item.get("title", "")),
                            url=_walmart_product_url(item),
                            price=_walmart_price(item),
                            in_stock=_walmart_in_stock(item),
                            raw=item,
                        ))
                except Exception as exc:
                    log.debug("Walmart JSON parse error: %s", exc)

            if not products:
                # Fallback: parse HTML
                html = await page.content()
                products = _parse_walmart_html(html, site_key, site_name, url)

        except Exception as exc:
            log.error("Walmart scrape error for %s: %s", url, exc)
        finally:
            await page.close()
            await context.close()

        log.debug("Walmart %s: found %d products", url, len(products))
        return products


def _extract_walmart_products(data: dict) -> list:
    """Recursively find product arrays in Walmart's __NEXT_DATA__ JSON."""
    results = []
    try:
        # Common paths in Walmart CA next data
        pages = data.get("props", {}).get("pageProps", {})
        # Search results
        search_data = pages.get("initialSearch", pages.get("searchPage", {}))
        items = (
            search_data.get("items", [])
            or search_data.get("products", [])
            or _deep_find(data, "items")
            or _deep_find(data, "products")
        )
        results.extend(items if isinstance(items, list) else [])
    except Exception:
        pass
    return results


def _deep_find(obj, key, depth=0):
    if depth > 10:
        return None
    if isinstance(obj, dict):
        if key in obj and isinstance(obj[key], list) and len(obj[key]) > 0:
            return obj[key]
        for v in obj.values():
            result = _deep_find(v, key, depth + 1)
            if result:
                return result
    elif isinstance(obj, list):
        for item in obj[:5]:
            result = _deep_find(item, key, depth + 1)
            if result:
                return result
    return None


def _walmart_product_url(item: dict) -> str:
    url = item.get("canonicalUrl", item.get("productUrl", ""))
    if url and not url.startswith("http"):
        url = "https://www.walmart.ca" + url
    return url or "https://www.walmart.ca"


def _walmart_price(item: dict) -> Optional[float]:
    for key in ("salePrice", "currentPrice", "price", "regularPrice"):
        val = item.get(key)
        if val:
            if isinstance(val, dict):
                val = val.get("price", val.get("amount", val.get("value")))
            if val:
                p = parse_price(str(val))
                if p:
                    return p
    return None


def _walmart_in_stock(item: dict) -> bool:
    availability = str(item.get("availabilityStatus", item.get("availability", ""))).lower()
    if "stock" in availability and "out" not in availability:
        return True
    if availability in ("in_stock", "available", "instock"):
        return True
    return bool(item.get("isAvailable", item.get("available", False)))


def _parse_walmart_html(html: str, site_key: str, site_name: str, base_url: str) -> List[Product]:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    products = []
    # Walmart product cards
    cards = soup.select('[data-item-id], [data-product-id], article[data-automation-id]')
    for card in cards:
        pid = card.get("data-item-id") or card.get("data-product-id") or ""
        title_el = card.select_one('[data-automation-id="product-title"] span, .product-name, h3')
        title = title_el.get_text(strip=True) if title_el else ""
        link_el = card.select_one("a[href]")
        href = link_el["href"] if link_el else ""
        if href and not href.startswith("http"):
            href = "https://www.walmart.ca" + href
        price_el = card.select_one('[itemprop="price"], .price-characteristic, [data-automation-id="product-price"]')
        price = parse_price(price_el.get_text(strip=True)) if price_el else None
        in_stock = "out-of-stock" not in html.lower() or bool(price)
        if title or pid:
            products.append(Product(
                site_key=site_key, site_name=site_name,
                product_id=pid or title[:40],
                title=title, url=href or base_url,
                price=price, in_stock=in_stock,
            ))
    return products


# ------------------------------------------------------------------ #
# Costco Canada                                                        #
# ------------------------------------------------------------------ #

class CostcoScraper:
    def __init__(self, proxies=None):
        self.proxies = proxies or []

    async def scrape_url(self, url: str, site_key: str, site_name: str) -> List[Product]:
        if not PLAYWRIGHT_AVAILABLE:
            return []
        products = []
        context = await new_context(self.proxies)
        page = await context.new_page()
        try:
            await stealth_async(page)
            await page.goto(url, wait_until="domcontentloaded", timeout=40000)
            await _dismiss_consent(page)
            await page.wait_for_timeout(random.randint(3000, 5000))

            for _ in range(2):
                await page.keyboard.press("End")
                await page.wait_for_timeout(1500)

            html = await page.content()
            products = _parse_costco_html(html, site_key, site_name)

        except Exception as exc:
            log.error("Costco scrape error for %s: %s", url, exc)
        finally:
            await page.close()
            await context.close()

        return products


def _parse_costco_html(html: str, site_key: str, site_name: str) -> List[Product]:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    products = []

    # Costco product tiles
    tiles = soup.select(".product-list-item, .ProductTile, [automation-id='productTile'], .product")
    for tile in tiles:
        link_el = tile.select_one("a[href]")
        href = link_el["href"] if link_el else ""
        if href and not href.startswith("http"):
            href = "https://www.costco.ca" + href

        pid = ""
        if href:
            m = re.search(r"/p/([^/?#]+)", href)
            pid = m.group(1) if m else href.split("/")[-1]

        title_el = tile.select_one(".product-name, .automation-product-name, h3, h4")
        title = title_el.get_text(strip=True) if title_el else ""

        price_el = tile.select_one(".price, .automation-final-price, [automation-id='finalPrice']")
        price = parse_price(price_el.get_text(strip=True)) if price_el else None

        out = tile.select_one(".out-of-stock, [class*='soldOut'], [class*='outOfStock']")
        in_stock = out is None and bool(price)

        if title or pid:
            products.append(Product(
                site_key=site_key, site_name=site_name,
                product_id=pid or title[:40],
                title=title, url=href or "https://www.costco.ca",
                price=price, in_stock=in_stock,
            ))
    return products


# ------------------------------------------------------------------ #
# Amazon Canada                                                        #
# ------------------------------------------------------------------ #

class AmazonScraper:
    def __init__(self, proxies=None):
        self.proxies = proxies or []

    async def scrape_url(self, url: str, site_key: str, site_name: str) -> List[Product]:
        if not PLAYWRIGHT_AVAILABLE:
            return []
        products = []
        context = await new_context(self.proxies)
        page = await context.new_page()
        try:
            await stealth_async(page)
            # Amazon-specific: block images to speed up load
            await page.route("**/*.{png,jpg,gif,webp,svg}", lambda r: r.abort())
            await page.goto(url, wait_until="domcontentloaded", timeout=35000)
            await _dismiss_consent(page)
            await page.wait_for_timeout(random.randint(2000, 4000))

            # Check for CAPTCHA
            content = await page.content()
            if "captcha" in content.lower() or "robot check" in content.lower():
                log.warning("Amazon CAPTCHA detected for %s", url)
                return []

            html = await page.content()
            products = _parse_amazon_html(html, site_key, site_name)

        except Exception as exc:
            log.error("Amazon scrape error for %s: %s", url, exc)
        finally:
            await page.close()
            await context.close()

        return products


def _parse_amazon_html(html: str, site_key: str, site_name: str) -> List[Product]:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    products = []

    # Amazon search result items
    for item in soup.select('[data-asin]:not([data-asin=""])'):
        asin = item.get("data-asin", "")
        if not asin:
            continue

        title_el = item.select_one("h2 a span, h2 span, .a-size-medium, .a-size-base-plus")
        title = title_el.get_text(strip=True) if title_el else ""

        link_el = item.select_one("h2 a, .a-link-normal[href*='/dp/']")
        href = link_el.get("href", "") if link_el else ""
        if href and not href.startswith("http"):
            href = "https://www.amazon.ca" + href

        # Price — whole + fraction
        price = None
        whole = item.select_one(".a-price-whole")
        frac = item.select_one(".a-price-fraction")
        if whole:
            price_str = whole.get_text(strip=True).replace(",", "")
            if frac:
                price_str += "." + frac.get_text(strip=True)
            price = parse_price(price_str)
        if price is None:
            price_el = item.select_one(".a-offscreen")
            if price_el:
                price = parse_price(price_el.get_text(strip=True))

        # Stock: if price present and no "Currently unavailable", assume in stock
        unavailable = item.select_one("[class*='unavailable'], .a-color-error")
        in_stock = price is not None and unavailable is None

        if title and asin:
            products.append(Product(
                site_key=site_key, site_name=site_name,
                product_id=asin, title=title,
                url=href or f"https://www.amazon.ca/dp/{asin}",
                price=price, in_stock=in_stock,
            ))
    return products


# ------------------------------------------------------------------ #
# Indigo / Chapters                                                    #
# ------------------------------------------------------------------ #

class IndigoScraper:
    def __init__(self, proxies=None):
        self.proxies = proxies or []

    async def scrape_url(self, url: str, site_key: str, site_name: str) -> List[Product]:
        if not PLAYWRIGHT_AVAILABLE:
            return []
        products = []
        context = await new_context(self.proxies)
        page = await context.new_page()
        try:
            await stealth_async(page)
            await page.goto(url, wait_until="domcontentloaded", timeout=40000)
            await _dismiss_consent(page)
            await page.wait_for_timeout(random.randint(3000, 5000))

            html = await page.content()
            products = _parse_indigo_html(html, site_key, site_name)

        except Exception as exc:
            log.error("Indigo scrape error for %s: %s", url, exc)
        finally:
            await page.close()
            await context.close()

        return products


def _parse_indigo_html(html: str, site_key: str, site_name: str) -> List[Product]:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    products = []

    for card in soup.select(".product-card, .search-result-item, [class*='ProductCard'], [data-product-id]"):
        pid = card.get("data-product-id", "")
        link_el = card.select_one("a[href]")
        href = link_el["href"] if link_el else ""
        if href and not href.startswith("http"):
            href = "https://www.chapters.indigo.ca" + href
        if not pid and href:
            m = re.search(r"/products/([^/?#]+)", href)
            pid = m.group(1) if m else href.split("/")[-1]

        title_el = card.select_one(".product-card__name, .product-title, h3, h4")
        title = title_el.get_text(strip=True) if title_el else ""

        price_el = card.select_one(".product-card__sale-price, .product-price, [class*='price']")
        price = parse_price(price_el.get_text(strip=True)) if price_el else None

        out = card.select_one("[class*='soldOut'], [class*='out-of-stock'], .product-card__availability--out")
        in_stock = out is None and bool(href)

        if title or pid:
            products.append(Product(
                site_key=site_key, site_name=site_name,
                product_id=pid or title[:40],
                title=title, url=href or "https://www.chapters.indigo.ca",
                price=price, in_stock=in_stock,
            ))
    return products


# ------------------------------------------------------------------ #
# Pokemon Center Canada                                                #
# ------------------------------------------------------------------ #

class PokemonCenterScraper:
    def __init__(self, proxies=None):
        self.proxies = proxies or []

    async def scrape_url(self, url: str, site_key: str, site_name: str) -> List[Product]:
        if not PLAYWRIGHT_AVAILABLE:
            return []
        products = []
        context = await new_context(self.proxies)
        page = await context.new_page()
        try:
            await stealth_async(page)
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await _dismiss_consent(page)
            await page.wait_for_timeout(random.randint(4000, 6000))

            for _ in range(3):
                await page.keyboard.press("End")
                await page.wait_for_timeout(1500)

            html = await page.content()
            products = _parse_pokemon_center_html(html, site_key, site_name)

        except Exception as exc:
            log.error("Pokemon Center scrape error for %s: %s", url, exc)
        finally:
            await page.close()
            await context.close()

        return products


def _parse_pokemon_center_html(html: str, site_key: str, site_name: str) -> List[Product]:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    products = []

    for card in soup.select(".product-tile, .ProductTile, [class*='product-grid'] > li, [data-product]"):
        link_el = card.select_one("a[href]")
        href = link_el["href"] if link_el else ""
        if href and not href.startswith("http"):
            href = "https://www.pokemoncenter.com" + href

        pid = ""
        if href:
            m = re.search(r"/product/([^/?#]+)", href)
            pid = m.group(1) if m else href.split("/")[-1]

        title_el = card.select_one(".product-tile__title, .product-name, h3, h4, [class*='title']")
        title = title_el.get_text(strip=True) if title_el else ""

        price_el = card.select_one(".product-tile__price, .price, [class*='price']")
        price = parse_price(price_el.get_text(strip=True)) if price_el else None

        out = card.select_one("[class*='soldOut'], [class*='outOfStock'], [class*='unavailable']")
        add_btn = card.select_one("button[class*='add-to-cart'], button[class*='addToCart']")
        in_stock = out is None and (add_btn is not None or bool(price))

        if title or pid:
            products.append(Product(
                site_key=site_key, site_name=site_name,
                product_id=pid or title[:40],
                title=title, url=href or "https://www.pokemoncenter.com/en-ca",
                price=price, in_stock=in_stock,
            ))
    return products
