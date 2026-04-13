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

            page_title = await page.title()
            page_url = page.url
            log.info("Walmart page loaded: title=%r url=%s", page_title, page_url)

            if next_data:
                import json
                try:
                    data = json.loads(next_data)
                    items = _extract_walmart_products(data)
                    log.info("Walmart __NEXT_DATA__ found, extracted %d items", len(items))
                    for item in items:
                        products.append(Product(
                            site_key=site_key,
                            site_name=site_name,
                            product_id=item.get("id", item.get("usItemId", "")),
                            title=item.get("name", item.get("title", "")),
                            url=_walmart_product_url(item),
                            price=_walmart_price(item),
                            in_stock=_walmart_in_stock(item),
                            image_url=item.get("imageInfo", {}).get("thumbnailUrl") or item.get("image", {}).get("url"),
                            raw=item,
                        ))
                except Exception as exc:
                    log.info("Walmart JSON parse error: %s", exc)
            else:
                log.info("Walmart: __NEXT_DATA__ not found on page")

            if not products:
                # Fallback: parse HTML
                html = await page.content()
                log.info("Walmart HTML fallback, page length=%d chars", len(html))
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
        img_el = card.select_one("img[src]")
        image_url = img_el.get("src") or img_el.get("data-src") if img_el else None
        in_stock = "out-of-stock" not in html.lower() or bool(price)
        if title or pid:
            products.append(Product(
                site_key=site_key, site_name=site_name,
                product_id=pid or title[:40],
                title=title, url=href or base_url,
                price=price, in_stock=in_stock,
                image_url=image_url,
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

        img_el = tile.select_one("img[src]")
        image_url = img_el.get("src") or img_el.get("data-src") if img_el else None
        out = tile.select_one(".out-of-stock, [class*='soldOut'], [class*='outOfStock']")
        in_stock = out is None and bool(price)

        if title or pid:
            products.append(Product(
                site_key=site_key, site_name=site_name,
                product_id=pid or title[:40],
                title=title, url=href or "https://www.costco.ca",
                price=price, in_stock=in_stock,
                image_url=image_url,
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
            await page.goto(url, wait_until="networkidle", timeout=35000)
            await _dismiss_consent(page)
            await page.wait_for_timeout(random.randint(4000, 6000))

            # Explicit selector wait
            try:
                await page.wait_for_selector('[data-asin]:not([data-asin=""])', timeout=10000)
            except Exception:
                log.info("Amazon: timed out waiting for [data-asin] selector on %s", url)

            # Check for CAPTCHA — URL, title, or content
            page_title = await page.title()
            page_url = page.url
            content = await page.content()
            if (
                "captcha" in page_url.lower()
                or any(kw in page_title for kw in ("Robot", "CAPTCHA", "verify", "robot check"))
                or "captcha" in content.lower()
                or "robot check" in content.lower()
            ):
                log.warning(
                    "Amazon CAPTCHA detected for %s | title=%r url=%s | HTML[:2000]=%s",
                    url, page_title, page_url, content[:2000],
                )
                return []

            html = content
            products = _parse_amazon_html(html, site_key, site_name)

            if not products:
                log.info(
                    "Amazon 0 products. title=%r url=%s HTML[:2000]=%s",
                    page_title, page_url, html[:2000],
                )

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

    def _parse_item(item):
        asin = item.get("data-asin", "")
        if not asin:
            return

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

        img_el = item.select_one("img.s-image, img[data-image-latency], img[src*='amazon']")
        image_url = img_el.get("src") if img_el else None

        # Stock: if price present and no "Currently unavailable", assume in stock
        unavailable = item.select_one("[class*='unavailable'], .a-color-error")
        in_stock = price is not None and unavailable is None

        if title and asin:
            products.append(Product(
                site_key=site_key, site_name=site_name,
                product_id=asin, title=title,
                url=href or f"https://www.amazon.ca/dp/{asin}",
                price=price, in_stock=in_stock,
                image_url=image_url,
            ))

    # Amazon search result items — primary selector
    for item in soup.select('[data-asin]:not([data-asin=""])'):
        _parse_item(item)

    # Fallback selectors if primary returned nothing
    if not products:
        for item in soup.select('.s-result-item[data-asin]'):
            _parse_item(item)
    if not products:
        for item in soup.select('.sg-col-inner .s-card-container'):
            asin = item.get("data-asin", "")
            if not asin:
                parent = item.find_parent(attrs={"data-asin": True})
                if parent:
                    item = parent
            _parse_item(item)

    if not products:
        body_classes = []
        for el in soup.select("body, main"):
            cls = el.get("class", [])
            if cls:
                body_classes.extend(cls)
        log.info(
            "Amazon HTML parse: 0 products. Selectors tried. Body classes: %s",
            body_classes[:10],
        )

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
            await page.goto(url, wait_until="networkidle", timeout=40000)
            await _dismiss_consent(page)
            await page.wait_for_timeout(random.randint(4000, 7000))

            try:
                await page.wait_for_selector('a[href*="/en-ca/"]', timeout=12000)
            except Exception:
                log.info("Indigo: timed out waiting for a[href*='/en-ca/'] on %s", url)

            html = await page.content()
            products = _parse_indigo_html(html, site_key, site_name)

            if not products:
                title = await page.title()
                log.info(
                    "Indigo 0 products. title=%r url=%s HTML[:2000]=%s",
                    title, page.url, html[:2000],
                )

        except Exception as exc:
            log.error("Indigo scrape error for %s: %s", url, exc)
        finally:
            await page.close()
            await context.close()

        return products


def _parse_indigo_html(html: str, site_key: str, site_name: str) -> List[Product]:
    from bs4 import BeautifulSoup
    import re as _re
    soup = BeautifulSoup(html, "lxml")
    products = []

    # Strategy 1: explicit data attributes
    cards = soup.select("[data-product-id], [data-testid*='product'], [data-qa*='product']")

    # Strategy 2: known stable class fragments
    if not cards:
        cards = soup.select("[class*='ProductCard'], [class*='product-card'], [class*='ProductTile']")

    # Strategy 3: li/article elements inside a grid/list
    if not cards:
        cards = soup.select("ul.product-list li, ol.product-list li, .search-results li, main ul li")

    # Strategy 4: any link going to an Indigo product page
    if not cards:
        # Build products from all product links directly
        seen = set()
        for link in soup.select("a[href*='/en-ca/']"):
            href = link.get("href", "")
            if not href or len(href) < 10:
                continue
            if not href.startswith("http"):
                href = "https://www.chapters.indigo.ca" + href
            if href in seen:
                continue
            # Must look like a product URL (has product/book segment, not nav)
            if not _re.search(r'/en-ca/[a-z-]+/[a-z0-9-]+/', href):
                continue
            seen.add(href)
            title = link.get_text(strip=True)
            if not title or len(title) < 4:
                # try parent
                parent = link.find_parent(["li", "article", "div"])
                if parent:
                    title = parent.get_text(" ", strip=True)[:100]
            pid = href.split("/")[-2] if "/" in href else href[-20:]
            # look for price in parent context
            parent_el = link.find_parent(["li", "article", "div"])
            price = None
            if parent_el:
                price_el = parent_el.select_one("[class*='price'], [class*='Price']")
                if price_el:
                    price = parse_price(price_el.get_text(strip=True))
                if price is None:
                    price_match = _re.search(r'\$(\d+(?:\.\d{2})?)', parent_el.get_text())
                    if price_match:
                        try:
                            price = float(price_match.group(1))
                        except ValueError:
                            pass
            if title:
                products.append(Product(
                    site_key=site_key, site_name=site_name,
                    product_id=pid or title[:40],
                    title=title[:200], url=href,
                    price=price, in_stock=True,
                    image_url=None,
                ))
        log.info("Indigo strategy 4 (link scan): %d products", len(products))
        return products

    for card in cards:
        pid = card.get("data-product-id", "")
        link_el = card.select_one("a[href]")
        href = link_el["href"] if link_el else ""
        if href and not href.startswith("http"):
            href = "https://www.chapters.indigo.ca" + href
        if not pid and href:
            m = _re.search(r'/en-ca/[^/]+/([^/?#]+)/', href)
            pid = m.group(1) if m else href.split("/")[-1]

        title_el = (
            card.select_one("[class*='title'], [class*='Title'], [class*='name'], [class*='Name'], h3, h4, h2") or
            (link_el if link_el else None)
        )
        title = title_el.get_text(strip=True) if title_el else ""

        price_el = card.select_one("[class*='price'], [class*='Price'], [class*='sale'], [class*='Sale']")
        price = parse_price(price_el.get_text(strip=True)) if price_el else None
        if price is None:
            import re as _re2
            price_match = _re2.search(r'\$(\d+(?:\.\d{2})?)', card.get_text())
            if price_match:
                try:
                    price = float(price_match.group(1))
                except ValueError:
                    pass

        img_el = card.select_one("img[src]")
        image_url = img_el.get("src") or img_el.get("data-src") if img_el else None

        out = card.select_one("[class*='soldOut'], [class*='SoldOut'], [class*='out-of-stock'], [class*='OutOfStock']")
        in_stock = out is None and bool(href)

        if title or pid:
            products.append(Product(
                site_key=site_key, site_name=site_name,
                product_id=pid or title[:40],
                title=title[:200], url=href or "https://www.chapters.indigo.ca",
                price=price, in_stock=in_stock,
                image_url=image_url,
            ))

    log.info("Indigo HTML parse: %d products (strategies 1-3)", len(products))
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
            await page.goto(url, wait_until="networkidle", timeout=45000)
            await _dismiss_consent(page)
            await page.wait_for_timeout(random.randint(5000, 8000))

            try:
                await page.wait_for_selector(
                    '.product-tile, [class*="ProductTile"], [class*="product-tile"]',
                    timeout=15000,
                )
            except Exception:
                log.info("PokemonCenter: timed out waiting for product tile selector on %s", url)

            for _ in range(3):
                await page.keyboard.press("End")
                await page.wait_for_timeout(1500)

            html = await page.content()
            products = _parse_pokemon_center_html(html, site_key, site_name)

            if not products:
                title = await page.title()
                log.info(
                    "PokemonCenter 0 products. title=%r url=%s HTML[:2000]=%s",
                    title, page.url, html[:2000],
                )

        except Exception as exc:
            log.error("Pokemon Center scrape error for %s: %s", url, exc)
        finally:
            await page.close()
            await context.close()

        return products


def _parse_pokemon_center_html(html: str, site_key: str, site_name: str) -> List[Product]:
    from bs4 import BeautifulSoup
    import re as _re
    soup = BeautifulSoup(html, "lxml")
    products = []

    # SFCC standard selectors + Pokemon Center variants
    cards = (
        soup.select(".product-tile") or
        soup.select("[class*='product-tile']") or
        soup.select("[class*='ProductTile']") or
        soup.select("[class*='product-card']") or
        soup.select("li.grid-tile") or
        soup.select("[data-itemid], [data-pid]")
    )

    if not cards:
        log.info("PokemonCenter: no product cards found. Trying link scan.")
        seen = set()
        for link in soup.select("a[href*='/en-ca/product/']"):
            href = link.get("href", "")
            if not href.startswith("http"):
                href = "https://www.pokemoncenter.com" + href
            if href in seen:
                continue
            seen.add(href)
            title = link.get_text(strip=True)
            if not title:
                img = link.find("img")
                title = img.get("alt", "") if img else ""
            pid_m = _re.search(r'/product/([^/?#]+)', href)
            pid = pid_m.group(1) if pid_m else href.split("/")[-1]
            parent = link.find_parent(["li", "div", "article"])
            price = None
            if parent:
                price_el = parent.select_one("[class*='price'], [class*='Price']")
                if price_el:
                    price = parse_price(price_el.get_text(strip=True))
            img_el = link.find("img")
            image_url = img_el.get("src") if img_el else None
            if title or pid:
                products.append(Product(
                    site_key=site_key, site_name=site_name,
                    product_id=pid or title[:40],
                    title=title[:200], url=href,
                    price=price, in_stock=True,
                    image_url=image_url,
                ))
        return products

    for card in cards:
        link_el = card.select_one("a[href*='/en-ca/product/'], a[href*='/product/'], a[href]")
        href = link_el.get("href", "") if link_el else ""
        if href and not href.startswith("http"):
            href = "https://www.pokemoncenter.com" + href

        pid = card.get("data-pid") or card.get("data-itemid", "")
        if not pid and href:
            m = _re.search(r'/product/([^/?#]+)', href)
            pid = m.group(1) if m else href.split("/")[-1]

        title_el = card.select_one(
            ".product-name, .name-link, [class*='product-name'], [class*='ProductName'], "
            "[class*='title'], h3, h4, h2"
        )
        title = title_el.get_text(strip=True) if title_el else ""
        if not title and link_el:
            img = link_el.find("img")
            title = img.get("alt", "") if img else ""

        price_el = card.select_one(
            ".price-sales, .sales, .product-standard-price, "
            "[class*='price'], [class*='Price']"
        )
        price = parse_price(price_el.get_text(strip=True)) if price_el else None

        img_el = card.select_one("img[src]")
        image_url = img_el.get("src") or img_el.get("data-src") if img_el else None

        out = card.select_one(
            "[class*='soldOut'], [class*='SoldOut'], [class*='outOfStock'], "
            "[class*='OutOfStock'], [class*='unavailable']"
        )
        add_btn = card.select_one(
            "button[class*='add-to-cart'], button[class*='addToCart'], "
            "button[class*='AddToCart']"
        )
        in_stock = out is None and (add_btn is not None or bool(price))

        if title or pid:
            products.append(Product(
                site_key=site_key, site_name=site_name,
                product_id=pid or title[:40],
                title=title[:200], url=href or "https://www.pokemoncenter.com/en-ca",
                price=price, in_stock=in_stock,
                image_url=image_url,
            ))

    log.info("PokemonCenter HTML: %d products", len(products))
    return products


# ------------------------------------------------------------------ #
# GameStop Canada                                                       #
# ------------------------------------------------------------------ #

class GameStopScraper:
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
            await page.goto(url, wait_until="networkidle", timeout=40000)
            await _dismiss_consent(page)
            await page.wait_for_timeout(random.randint(3000, 5000))

            for _ in range(2):
                await page.keyboard.press("End")
                await page.wait_for_timeout(1500)

            html = await page.content()
            if not html or len(html) < 500:
                log.warning("GameStop: empty page for %s", url)
                return []

            products = _parse_gamestop_html(html, site_key, site_name)
            if not products:
                title = await page.title()
                log.info("GameStop 0 products. Title=%r URL=%s HTML[:2000]=%s", title, page.url, html[:2000])

        except Exception as exc:
            log.error("GameStop scrape error for %s: %s", url, exc)
        finally:
            await page.close()
            await context.close()

        log.info("GameStop %s: found %d products", url, len(products))
        return products


def _parse_gamestop_html(html: str, site_key: str, site_name: str) -> List[Product]:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    products = []

    # GameStop Canada product cards
    cards = (
        soup.select("[class*='product-card'], [class*='ProductCard']") or
        soup.select("[class*='product-item'], [class*='ProductItem']") or
        soup.select("[data-product-id], [data-pid]") or
        soup.select("li.product, article.product")
    )

    if not cards:
        # Fallback: scan all links to /en/product/ or /en/p/
        seen = set()
        for link in soup.select("a[href*='/en/product/'], a[href*='/en/p/'], a[href*='/products/']"):
            href = link.get("href", "")
            if not href.startswith("http"):
                href = "https://www.gamestop.ca" + href
            if href in seen:
                continue
            seen.add(href)
            title = link.get_text(strip=True)
            if not title:
                img = link.find("img")
                title = img.get("alt", "") if img else ""
            if not title:
                continue
            pid = href.split("/")[-1].split("?")[0] or title[:40]
            parent = link.find_parent(["li", "div", "article"])
            price = None
            if parent:
                price_el = parent.select_one("[class*='price'], [class*='Price']")
                if price_el:
                    price = parse_price(price_el.get_text(strip=True))
            img_el = (link.find("img") or (parent.find("img") if parent else None))
            image_url = img_el.get("src") if img_el else None
            products.append(Product(
                site_key=site_key, site_name=site_name,
                product_id=pid, title=title[:200], url=href,
                price=price, in_stock=bool(price),
                image_url=image_url,
            ))
        return products

    for card in cards:
        link_el = card.select_one("a[href]")
        href = link_el.get("href", "") if link_el else ""
        if href and not href.startswith("http"):
            href = "https://www.gamestop.ca" + href

        pid = card.get("data-product-id") or card.get("data-pid", "")
        if not pid and href:
            pid = href.split("/")[-1].split("?")[0]

        title_el = card.select_one("[class*='title'], [class*='Title'], [class*='name'], [class*='Name'], h3, h4")
        title = title_el.get_text(strip=True) if title_el else ""
        if not title and link_el:
            img = link_el.find("img")
            title = img.get("alt", "") if img else link_el.get_text(strip=True)

        price_el = card.select_one("[class*='price'], [class*='Price']")
        price = parse_price(price_el.get_text(strip=True)) if price_el else None

        img_el = card.select_one("img[src]")
        image_url = img_el.get("src") or img_el.get("data-src") if img_el else None

        out = card.select_one("[class*='soldOut'], [class*='out-of-stock'], [class*='unavailable']")
        in_stock = out is None and bool(href)

        if title or pid:
            products.append(Product(
                site_key=site_key, site_name=site_name,
                product_id=pid or title[:40],
                title=title[:200], url=href or "https://www.gamestop.ca",
                price=price, in_stock=in_stock,
                image_url=image_url,
            ))

    return products
