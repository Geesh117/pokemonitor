"""
Best Buy Canada scraper using Playwright (their internal API changed).
Parses the rendered HTML product grid for each configured URL.
"""

import json
import re
from typing import List

from scrapers.base import Product, parse_price
from bot.logger_setup import get_logger

log = get_logger(__name__)

try:
    from playwright.async_api import async_playwright
    from playwright_stealth import Stealth as _Stealth
    _stealth = _Stealth()

    async def _stealth_page(page):
        await _stealth.apply_stealth_async(page)

    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False


class BestBuyScraper:
    """Scrapes Best Buy Canada using Playwright to render JS pages."""

    def __init__(self, proxies=None, **kwargs):
        self.proxies = proxies or []

    async def scrape_url(self, url: str, site_key: str, site_name: str) -> List[Product]:
        if not PLAYWRIGHT_AVAILABLE:
            log.warning("Playwright not available for Best Buy scraper")
            return []

        from scrapers.playwright_scraper import new_context, _dismiss_consent
        import random

        products: List[Product] = []
        context = await new_context(self.proxies)
        page = await context.new_page()

        try:
            await _stealth_page(page)
            await page.goto(url, wait_until="domcontentloaded", timeout=35000)
            await _dismiss_consent(page)
            await page.wait_for_timeout(random.randint(2000, 3500))

            title = await page.title()
            if "access denied" in title.lower() or "blocked" in title.lower():
                log.warning(
                    "Best Buy: Akamai bot protection triggered for %s. "
                    "Add residential proxies to config.json to bypass.", url
                )
                return []

            # Scroll to load lazy products
            for _ in range(2):
                await page.keyboard.press("End")
                await page.wait_for_timeout(1200)

            # Try to extract JSON data embedded in the page
            next_data = await page.evaluate("""
                () => {
                    const el = document.getElementById('__NEXT_DATA__');
                    return el ? el.textContent : null;
                }
            """)

            if next_data:
                try:
                    data = json.loads(next_data)
                    items = _extract_bestbuy_products(data)
                    for item in items:
                        products.append(_make_product(item, site_key, site_name))
                except Exception as exc:
                    log.debug("Best Buy JSON parse error: %s", exc)

            if not products:
                html = await page.content()
                products = _parse_bestbuy_html(html, site_key, site_name, url)

        except Exception as exc:
            log.error("Best Buy scrape error for %s: %s", url, exc)
        finally:
            await page.close()
            await context.close()

        log.debug("Best Buy %s: found %d products", url, len(products))
        return products


def _extract_bestbuy_products(data: dict) -> list:
    """Navigate Best Buy's __NEXT_DATA__ structure to find products."""
    results = []
    try:
        props = data.get("props", {}).get("pageProps", {})
        # Try multiple known paths
        for key in ("products", "items", "searchResults"):
            found = _deep_find(props, key)
            if found and isinstance(found, list):
                results.extend(found)
                break
    except Exception:
        pass
    return results


def _deep_find(obj, key, depth=0):
    if depth > 8:
        return None
    if isinstance(obj, dict):
        if key in obj and isinstance(obj[key], list) and len(obj[key]) > 0:
            return obj[key]
        for v in obj.values():
            result = _deep_find(v, key, depth + 1)
            if result:
                return result
    elif isinstance(obj, list):
        for item in obj[:3]:
            result = _deep_find(item, key, depth + 1)
            if result:
                return result
    return None


def _make_product(item: dict, site_key: str, site_name: str) -> Product:
    sku = str(item.get("sku", item.get("productId", item.get("id", ""))))
    title = item.get("name", item.get("title", ""))
    pdp = item.get("pdpUrl", item.get("productUrl", f"/en-ca/product/{sku}"))
    url = pdp if pdp.startswith("http") else f"https://www.bestbuy.ca{pdp}"

    price = None
    for key in ("salePrice", "regularPrice", "price"):
        val = item.get(key)
        if val:
            price = float(val)
            break

    avail = item.get("availability", {})
    in_stock = (
        bool(avail.get("isAvailableOnline"))
        or bool(avail.get("isAvailable"))
        or bool(item.get("isAvailable"))
    )

    return Product(
        site_key=site_key,
        site_name=site_name,
        product_id=sku,
        title=title,
        url=url,
        price=price,
        in_stock=in_stock,
        raw=item,
    )


def _parse_bestbuy_html(html: str, site_key: str, site_name: str, base_url: str) -> List[Product]:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    products = []

    # Best Buy product item selectors (as of 2024-2025)
    cards = soup.select(
        '[data-automation="productListingV2"], '
        '.x-productListItem, '
        '[class*="productItemContainer"], '
        '[class*="ProductListItem"], '
        'article.col-xs-12'
    )

    for card in cards:
        link_el = card.select_one("a[href*='/product/'], a[href*='/en-ca/product/']")
        if not link_el:
            link_el = card.select_one("a[href]")
        href = link_el["href"] if link_el else ""
        if href and not href.startswith("http"):
            href = "https://www.bestbuy.ca" + href

        pid = ""
        if href:
            m = re.search(r"/product/[^/]+/(\d+)", href)
            pid = m.group(1) if m else href.split("/")[-1]

        title_el = card.select_one(
            '[class*="productTitle"], [class*="productName"], '
            '[data-automation="productTitle"], h3, h4'
        )
        title = title_el.get_text(strip=True) if title_el else ""

        price_el = card.select_one(
            '[class*="salePrice"], [class*="regularPrice"], '
            '[data-automation="product-price"], [class*="priceWrapper"]'
        )
        price = parse_price(price_el.get_text(strip=True)) if price_el else None

        sold_out = card.select_one('[class*="soldOut"], [class*="addToCartButton"][disabled]')
        add_btn = card.select_one('[data-automation="addToCartButton"]:not([disabled])')
        in_stock = sold_out is None and (add_btn is not None or price is not None)

        if title or pid:
            products.append(Product(
                site_key=site_key,
                site_name=site_name,
                product_id=pid or title[:40],
                title=title,
                url=href or base_url,
                price=price,
                in_stock=in_stock,
            ))

    return products
