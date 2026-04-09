"""
Core monitoring loop.

Orchestrates:
  - Retail site scraping (every 60-90s)
  - News scraping (every 5-10 min)
  - Health check pings (every hour)
  - Daily digest (9am EST)
  - Stale-site alerts (if a site hasn't been checked in 10 min)
"""

import asyncio
import json
import os
import random
import re
from datetime import datetime, timedelta
from typing import Optional

import aiohttp
import pytz

from bot.database import Database
from bot.logger_setup import get_logger, setup_logger
from bot.telegram_service import TelegramService
from bot.url_verifier import verify_url
from scrapers.base import Product

log = get_logger(__name__)
EST = pytz.timezone("America/Toronto")

# Keyword whitelist — case-insensitive
KEYWORD_WHITELIST: list = []

# Price sanity thresholds
BOOSTER_BOX_MIN = 10.0
BOOSTER_BOX_MAX = 1000.0
BOOSTER_BOX_KWS = ["booster box", "booster bundle", "36 pack"]


def _matches_whitelist(title: str, whitelist: list) -> bool:
    if not whitelist:
        return True
    title_lower = title.lower()
    return any(kw.lower() in title_lower for kw in whitelist)


def _is_booster_box(title: str) -> bool:
    title_lower = title.lower()
    return any(kw in title_lower for kw in BOOSTER_BOX_KWS)


def _price_is_suspicious(price: Optional[float], title: str) -> Optional[str]:
    if price is None:
        return None
    if _is_booster_box(title):
        if price < BOOSTER_BOX_MIN:
            return f"price ${price:.2f} below minimum ${BOOSTER_BOX_MIN:.2f} for booster box"
        if price > BOOSTER_BOX_MAX:
            return f"price ${price:.2f} above maximum ${BOOSTER_BOX_MAX:.2f} for booster box"
    return None


class Monitor:
    def __init__(self, config: dict, test_mode: bool = False):
        self.config = config
        self.test_mode = test_mode

        # Logging
        setup_logger(
            "pokemonitor",
            log_dir=config["logs"]["directory"],
            level=config["logs"]["level"],
        )

        # Services
        self.db = Database(config["database"]["path"])
        self.tg = TelegramService(
            token=config["telegram"]["bot_token"],
            chat_id=config["telegram"]["chat_id"],
        )

        # Config shortcuts
        intervals = config["intervals"]
        self.retail_min = intervals["retail_min_seconds"]
        self.retail_max = intervals["retail_max_seconds"]
        self.news_min = intervals["news_min_seconds"]
        self.news_max = intervals["news_max_seconds"]
        self.health_interval = intervals["health_check_seconds"]
        self.stale_minutes = intervals["stale_site_alert_minutes"]
        self.digest_hour = intervals["daily_digest_hour_est"]
        self.alert_cooldown_hours = config["alert_cooldown_hours"]

        delays = config["delays"]
        self.delay_min = delays["between_requests_min"]
        self.delay_max = delays["between_requests_max"]
        self.proxies = config.get("proxies", [])

        global KEYWORD_WHITELIST, BOOSTER_BOX_MIN, BOOSTER_BOX_MAX, BOOSTER_BOX_KWS
        KEYWORD_WHITELIST = config["keywords"]["whitelist"]
        BOOSTER_BOX_MIN = config["price_sanity"]["booster_box_min_cad"]
        BOOSTER_BOX_MAX = config["price_sanity"]["booster_box_max_cad"]
        BOOSTER_BOX_KWS = config["keywords"].get("booster_box_keywords", BOOSTER_BOX_KWS)

        self.min_price_drop_pct = config.get("min_price_drop_pct", 2.0)
        self.min_price_drop_abs = config.get("min_price_drop_abs_cad", 5.0)

        self._last_digest_date: Optional[str] = None
        self._running = False

        # FX rate cache
        self._usd_cad_rate: Optional[float] = None
        self._usd_cad_rate_fetched: Optional[datetime] = None
        self._usd_cad_fallback = config.get("usd_cad_fallback_rate", 1.39)

        # Scrapers are created lazily
        self._scrapers: dict = {}

    # ------------------------------------------------------------------ #
    # Scraper factory                                                      #
    # ------------------------------------------------------------------ #

    def _get_scraper(self, scraper_type: str):
        if scraper_type not in self._scrapers:
            if scraper_type == "shopify":
                from scrapers.shopify_scraper import ShopifyScraper
                self._scrapers[scraper_type] = ShopifyScraper(
                    proxies=self.proxies,
                    delay_min=self.delay_min,
                    delay_max=self.delay_max,
                )
            elif scraper_type == "bestbuy":
                from scrapers.bestbuy_scraper import BestBuyScraper
                self._scrapers[scraper_type] = BestBuyScraper(proxies=self.proxies)
            elif scraper_type == "playwright":
                # Each site gets its own playwright scraper instance
                # (differentiated by context)
                pass
            elif scraper_type == "news":
                from scrapers.news_scraper import NewsScraper
                self._scrapers[scraper_type] = NewsScraper(
                    proxies=self.proxies,
                    delay_min=1,
                    delay_max=3,
                )
        return self._scrapers.get(scraper_type)

    def _get_playwright_scraper(self, site_key: str):
        from scrapers.playwright_scraper import (
            WalmartScraper, CostcoScraper, AmazonScraper,
            IndigoScraper, PokemonCenterScraper,
        )
        mapping = {
            "walmart_ca": WalmartScraper,
            "costco_ca": CostcoScraper,
            "amazon_ca": AmazonScraper,
            "indigo_ca": IndigoScraper,
            "pokemon_center": PokemonCenterScraper,
        }
        cls = mapping.get(site_key)
        if cls is None:
            log.warning("No playwright scraper for site key: %s", site_key)
            return None
        key = f"pw_{site_key}"
        if key not in self._scrapers:
            self._scrapers[key] = cls(proxies=self.proxies)
        return self._scrapers[key]

    # ------------------------------------------------------------------ #
    # Product processing                                                   #
    # ------------------------------------------------------------------ #

    async def _process_products(self, products: list, site_key: str):
        for product in products:
            if not _matches_whitelist(product.title, KEYWORD_WHITELIST):
                continue

            suspicious = _price_is_suspicious(product.price, product.title)
            if suspicious:
                await self.tg.send_suspicious_price(
                    product.site_name, product.title,
                    product.price, product.url, suspicious,
                )
                # Still record in DB but don't send public alert
                self.db.upsert_product(
                    site_key=site_key,
                    site_name=product.site_name,
                    product_id=product.product_id,
                    title=product.title,
                    url=product.url,
                    price=product.price,
                    in_stock=product.in_stock,
                )
                continue

            change = self.db.upsert_product(
                site_key=site_key,
                site_name=product.site_name,
                product_id=product.product_id,
                title=product.title,
                url=product.url,
                price=product.price,
                in_stock=product.in_stock,
            )

            if self.test_mode:
                self._print_test_product(product, change)
                continue

            cooldown_ok = not self.db.was_recently_alerted(
                site_key, product.product_id, self.alert_cooldown_hours
            )

            # Determine alert type
            alert_type = None

            if change["is_new"] and product.in_stock:
                alert_type = "new_product"
            elif change["stock_changed"] and product.in_stock and not change["old_in_stock"]:
                alert_type = "restock"
            # out_of_stock alerts disabled
            elif (
                change["price_changed"]
                and product.price < change["old_price"]
                and product.in_stock
            ):
                drop_abs = change["old_price"] - product.price
                drop_pct = drop_abs / change["old_price"] * 100
                if drop_pct >= self.min_price_drop_pct or drop_abs >= self.min_price_drop_abs:
                    alert_type = "price_drop"
            elif change["is_new"] and not product.in_stock:
                alert_type = "new_product"  # List even if OOS

            if alert_type and cooldown_ok:
                sent = await self._send_product_alert(product, alert_type, change)
                if sent:
                    self.db.mark_alerted(site_key, product.product_id, alert_type)
                    self.db.log_alert(
                        site_key=site_key,
                        site_name=product.site_name,
                        title=product.title,
                        alert_type=alert_type,
                        product_id=product.product_id,
                        url=product.url,
                        price=product.price,
                    )

    async def _send_product_alert(self, product: Product, alert_type: str, change: dict) -> bool:
        img = product.image_url
        if alert_type == "restock":
            return await self.tg.send_restock(
                product.site_name, product.title, product.price, product.url, img
            )
        elif alert_type == "new_product":
            return await self.tg.send_new_product(
                product.site_name, product.title, product.price, product.url, product.in_stock, img
            )
        elif alert_type == "price_drop":
            return await self.tg.send_price_drop(
                product.site_name, product.title,
                change["old_price"], product.price, product.url, img
            )
        return False

    def _print_test_product(self, product: Product, change: dict):
        status = "IN STOCK" if product.in_stock else "OUT OF STOCK"
        price = f"${product.price:.2f} CAD" if product.price else "N/A"
        flags = []
        if change["is_new"]:
            flags.append("NEW")
        if change["stock_changed"]:
            flags.append(f"STOCK CHANGED ({change['old_in_stock']} -> {product.in_stock})")
        if change["price_changed"]:
            flags.append(f"PRICE CHANGED (${change['old_price']:.2f} -> ${product.price:.2f})")
        flag_str = " | ".join(flags) if flags else "unchanged"
        print(
            f"  [{product.site_name}] {product.title} | {price} | {status} | {flag_str}\n"
            f"    URL: {product.url}"
        )

    # ------------------------------------------------------------------ #
    # Currency conversion                                                  #
    # ------------------------------------------------------------------ #

    async def _get_usd_cad_rate(self) -> float:
        """Return USD→CAD exchange rate, cached for 24 h."""
        now = datetime.utcnow()
        if (
            self._usd_cad_rate is not None
            and self._usd_cad_rate_fetched is not None
            and (now - self._usd_cad_rate_fetched).total_seconds() < 86400
        ):
            return self._usd_cad_rate

        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
                async with s.get("https://open.er-api.com/v6/latest/USD") as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        rate = data["rates"].get("CAD")
                        if rate:
                            self._usd_cad_rate = float(rate)
                            self._usd_cad_rate_fetched = now
                            log.info("USD→CAD rate updated: %.4f", self._usd_cad_rate)
                            return self._usd_cad_rate
        except Exception as exc:
            log.warning("Could not fetch USD→CAD rate: %s — using fallback %.4f", exc, self._usd_cad_fallback)

        return self._usd_cad_fallback

    # ------------------------------------------------------------------ #
    # Site check                                                           #
    # ------------------------------------------------------------------ #

    async def check_site(self, site_key: str, site_config: dict):
        if not site_config.get("enabled", True):
            return

        site_name = site_config["name"]
        scraper_type = site_config["type"]
        urls = site_config["urls"]

        log.info("Checking %s (%d URLs)", site_name, len(urls))

        try:
            all_products = []
            for url in urls:
                if scraper_type == "shopify":
                    scraper = self._get_scraper("shopify")
                    products = await scraper.scrape_url(url, site_key, site_name)
                elif scraper_type == "bestbuy":
                    scraper = self._get_scraper("bestbuy")
                    products = await scraper.scrape_url(url, site_key, site_name)
                elif scraper_type == "playwright":
                    scraper = self._get_playwright_scraper(site_key)
                    if scraper:
                        products = await scraper.scrape_url(url, site_key, site_name)
                    else:
                        products = []
                else:
                    log.warning("Unknown scraper type %s for %s", scraper_type, site_key)
                    products = []

                all_products.extend(products)
                await asyncio.sleep(random.uniform(self.delay_min, self.delay_max))

            # Convert USD prices to CAD if this store prices in USD
            if site_config.get("currency", "").upper() == "USD" and all_products:
                rate = await self._get_usd_cad_rate()
                for p in all_products:
                    if p.price is not None:
                        p.price = round(p.price * rate, 2)
                log.info("%s: applied USD→CAD rate %.4f to %d products", site_name, rate, len(all_products))

            await self._process_products(all_products, site_key)
            self.db.update_site_status(site_key, site_name, success=True)
            log.info("%s: processed %d products", site_name, len(all_products))

        except Exception as exc:
            log.error("Error checking site %s: %s", site_name, exc)
            self.db.update_site_status(site_key, site_name, success=False)

    # ------------------------------------------------------------------ #
    # News check                                                           #
    # ------------------------------------------------------------------ #

    async def check_news(self):
        news_sources = self.config.get("news_sources", {})
        news_scraper = self._get_scraper("news")
        if not news_scraper:
            from scrapers.news_scraper import NewsScraper
            news_scraper = NewsScraper(proxies=self.proxies, delay_min=1, delay_max=3)
            self._scrapers["news"] = news_scraper

        for source_key, source_config in news_sources.items():
            if not source_config.get("enabled", True):
                continue
            try:
                items = await news_scraper.scrape_source(source_key, source_config)
                for item in items:
                    if self.db.news_seen(item.url):
                        continue
                    self.db.add_news(
                        source_key=source_key,
                        source_name=item.source_name,
                        article_url=item.url,
                        title=item.title,
                        published=item.published,
                    )
                    if self.test_mode:
                        print(f"  [NEWS] [{item.source_name}] {item.title}\n    URL: {item.url}")
                        continue

                    sent = await self.tg.send_news(item.source_name, item.title, item.url)
                    if sent:
                        self.db.mark_news_alerted(item.url)
                        self.db.log_alert(
                            site_key=source_key,
                            site_name=item.source_name,
                            title=item.title,
                            alert_type="news",
                            url=item.url,
                        )
            except Exception as exc:
                log.error("News check error for %s: %s", source_key, exc)
            await asyncio.sleep(random.uniform(1, 3))

    # ------------------------------------------------------------------ #
    # Health check                                                         #
    # ------------------------------------------------------------------ #

    async def health_check(self):
        statuses = self.db.get_all_site_status()
        if not self.test_mode:
            await self.tg.send_health_check(statuses)
        log.info("Health check sent (%d sites)", len(statuses))

        # Stale site check
        stale_threshold = timedelta(minutes=self.stale_minutes)
        now = datetime.utcnow()
        for s in statuses:
            last = s.get("last_check")
            if last:
                last_dt = datetime.fromisoformat(last)
                if (now - last_dt) > stale_threshold:
                    msg = f"⚠️ {s['site_name']} hasn't been checked in >{self.stale_minutes} min!"
                    log.warning(msg)
                    if not self.test_mode:
                        await self.tg.send(msg)

    # ------------------------------------------------------------------ #
    # Daily digest                                                         #
    # ------------------------------------------------------------------ #

    async def _maybe_send_daily_digest(self):
        now_est = datetime.now(EST)
        today_str = now_est.strftime("%Y-%m-%d")
        if (
            now_est.hour == self.digest_hour
            and self._last_digest_date != today_str
        ):
            self._last_digest_date = today_str
            alerts = self.db.get_alerts_since(hours=24)
            news = self.db.get_recent_news(limit=25)
            if not self.test_mode:
                await self.tg.send_daily_digest(alerts, news)
            log.info("Daily digest sent: %d alerts, %d news", len(alerts), len(news))

    # ------------------------------------------------------------------ #
    # Main run loop                                                        #
    # ------------------------------------------------------------------ #

    async def run_once(self):
        """Run one full check cycle (used by --test mode)."""
        print("\n=== POKEMONITOR TEST RUN ===\n")

        print("--- Retail Sites ---")
        sites = self.config.get("sites", {})
        for site_key, site_config in sites.items():
            if not site_config.get("enabled", True):
                continue
            print(f"\n[{site_config['name']}]")
            await self.check_site(site_key, site_config)

        print("\n--- News Sources ---")
        await self.check_news()

        print("\n=== TEST RUN COMPLETE ===")

    async def run(self):
        """Main production loop."""
        self._running = True
        log.info("PokéMonitor starting (test_mode=%s)", self.test_mode)

        if not self.test_mode:
            await self.tg.send("🤖 <b>PokéMonitor started!</b> Bot is now monitoring stores and news.")

        sites = self.config.get("sites", {})
        last_news_check = datetime.utcnow() - timedelta(seconds=self.news_max)
        last_health_check = datetime.utcnow() - timedelta(seconds=self.health_interval)

        while self._running:
            # Retail sites
            for site_key, site_config in sites.items():
                if not self._running:
                    break
                await self.check_site(site_key, site_config)
                # Stagger inter-site delay
                await asyncio.sleep(random.uniform(self.delay_min, self.delay_max))

            # News check
            now = datetime.utcnow()
            if (now - last_news_check).total_seconds() >= random.uniform(self.news_min, self.news_max):
                await self.check_news()
                last_news_check = datetime.utcnow()

            # Health check
            if (now - last_health_check).total_seconds() >= self.health_interval:
                await self.health_check()
                last_health_check = datetime.utcnow()

            # Daily digest
            await self._maybe_send_daily_digest()

            # Wait before next full retail cycle
            wait = random.uniform(self.retail_min, self.retail_max)
            log.debug("Sleeping %.1fs before next cycle", wait)
            await asyncio.sleep(wait)

    def stop(self):
        self._running = False
        log.info("Monitor stop requested")
