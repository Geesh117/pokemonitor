"""
Generic Shopify scraper — uses the public /products.json API endpoint.
Works for: 401 Games, Hobbiesville, Untouchables, Clutch Games, Face to Face Games.
"""

import re
from typing import List
from urllib.parse import urlparse, urljoin

from scrapers.base import BaseScraper, Product, parse_price
from bot.logger_setup import get_logger

log = get_logger(__name__)


def _collection_from_url(url: str) -> str:
    """Extract /collections/<handle> from a URL."""
    m = re.search(r"/collections/([^/?#]+)", url)
    return m.group(1) if m else ""


class ShopifyScraper(BaseScraper):
    """Fetches products from a Shopify store via its public JSON API."""

    async def scrape_url(self, url: str, site_key: str, site_name: str) -> List[Product]:
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        collection = _collection_from_url(url)

        if not collection:
            log.warning("Could not extract collection from %s", url)
            return []

        products: List[Product] = []
        page = 1

        while True:
            api_url = f"{base}/collections/{collection}/products.json"
            status, data = await self._get(
                api_url,
                params={"limit": 250, "page": page},
                extra_headers={"Accept": "application/json"},
                json=True,
            )

            if status != 200 or not data:
                if page == 1:
                    log.warning("Shopify API failed for %s (status %s)", api_url, status)
                break

            items = data.get("products", [])
            if not items:
                break

            for item in items:
                handle = item.get("handle", "")
                title = item.get("title", "")
                product_url = f"{base}/products/{handle}"

                variants = item.get("variants", [])
                # Use the first variant's price; pick cheapest in-stock if multiple
                price = None
                in_stock = False

                available_variants = [v for v in variants if v.get("available", False)]
                all_prices = []

                for v in variants:
                    if v.get("available"):
                        in_stock = True
                    raw_price = v.get("price")
                    if raw_price:
                        p = parse_price(str(raw_price))
                        if p:
                            all_prices.append(p)

                if available_variants:
                    # Cheapest in-stock price
                    in_stock_prices = []
                    for v in available_variants:
                        p = parse_price(str(v.get("price", "")))
                        if p:
                            in_stock_prices.append(p)
                    price = min(in_stock_prices) if in_stock_prices else (min(all_prices) if all_prices else None)
                elif all_prices:
                    price = min(all_prices)

                # Featured image
                images = item.get("images", [])
                image_url = images[0].get("src") if images else None

                products.append(Product(
                    site_key=site_key,
                    site_name=site_name,
                    product_id=handle,
                    title=title,
                    url=product_url,
                    price=price,
                    in_stock=in_stock,
                    image_url=image_url,
                    raw=item,
                ))

            if len(items) < 250:
                break
            page += 1

        log.debug("Shopify %s/%s: found %d products", site_name, collection, len(products))
        return products
