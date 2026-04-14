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
GAME_FILTER: list = []

# Price sanity thresholds
BOOSTER_BOX_MIN = 10.0
BOOSTER_BOX_MAX = 1000.0
BOOSTER_BOX_KWS = ["booster box", "booster bundle", "36 pack"]


def _matches_whitelist(title: str, whitelist: list) -> bool:
    if not whitelist:
        return True
    title_lower = title.lower()
    return any(kw.lower() in title_lower for kw in whitelist)


def _matches_game_filter(title: str, game_filter: list) -> bool:
    """Return True if title mentions at least one of the allowed games."""
    if not game_filter:
        return True
    title_lower = title.lower()
    return any(kw.lower() in title_lower for kw in game_filter)


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
            extra_chat_ids=config["telegram"].get("extra_chat_ids", []),
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

        global GAME_FILTER
        GAME_FILTER = config["keywords"].get("game_filter", [])

        self._last_digest_date: Optional[str] = None
        self._last_calendar_check: Optional[str] = None
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
            elif scraper_type == "stocktrack":
                from scrapers.drop_scraper import StockTrackScraper
                self._scrapers[scraper_type] = StockTrackScraper(
                    proxies=self.proxies,
                    delay_min=3,
                    delay_max=7,
                )
            elif scraper_type == "nowinstock":
                from scrapers.drop_scraper import NowinStockScraper
                self._scrapers[scraper_type] = NowinStockScraper(
                    proxies=self.proxies,
                    delay_min=3,
                    delay_max=7,
                )
        return self._scrapers.get(scraper_type)

    def _get_playwright_scraper(self, site_key: str):
        from scrapers.playwright_scraper import (
            WalmartScraper, CostcoScraper, AmazonScraper,
            IndigoScraper, PokemonCenterScraper, GameStopScraper,
        )
        mapping = {
            "walmart_ca": WalmartScraper,
            "costco_ca": CostcoScraper,
            "amazon_ca": AmazonScraper,
            "indigo_ca": IndigoScraper,
            "pokemon_center": PokemonCenterScraper,
            "gamestop_ca": GameStopScraper,
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
            if not _matches_game_filter(product.title, GAME_FILTER):
                continue
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

            # Record price history on first seen or any change
            if change["is_new"] or change["price_changed"] or change["stock_changed"]:
                self.db.record_price_history(
                    site_key=site_key,
                    product_id=product.product_id,
                    title=product.title,
                    site_name=product.site_name,
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

    async def _fetch_market_price(self, title: str) -> Optional[float]:
        """Best-effort TCGPlayer market price fetch — returns CAD or None."""
        try:
            from scrapers.tcgplayer_scraper import fetch_tcgplayer_price
            rate = await self._get_usd_cad_rate()
            return await asyncio.wait_for(
                fetch_tcgplayer_price(title, rate), timeout=6
            )
        except Exception:
            return None

    async def _send_product_alert(self, product: Product, alert_type: str, change: dict) -> bool:
        img = product.image_url
        qty = product.quantity
        market_price = None

        # Fetch TCGPlayer market price for restock/new product alerts
        if alert_type in ("restock", "new_product"):
            market_price = await self._fetch_market_price(product.title)

        # Pokemon Center gets a special high-priority alert format
        if product.site_key == "pokemon_center" and alert_type in ("restock", "new_product"):
            sent = await self.tg.send_pokemon_center_alert(
                product_name=product.title,
                price=product.price,
                url=product.url,
                alert_type=alert_type,
                image_url=img,
                quantity=qty,
                market_price_cad=market_price,
            )
        elif alert_type == "restock":
            sent = await self.tg.send_restock(
                product.site_name, product.title, product.price, product.url, img,
                quantity=qty, market_price_cad=market_price,
            )
        elif alert_type == "new_product":
            sent = await self.tg.send_new_product(
                product.site_name, product.title, product.price, product.url, product.in_stock, img,
                quantity=qty, market_price_cad=market_price,
            )
        elif alert_type == "price_drop":
            sent = await self.tg.send_price_drop(
                product.site_name, product.title,
                change["old_price"], product.price, product.url, img,
            )
        else:
            return False

        # Fire watch alerts for any user watching this product
        if sent and alert_type in ("restock", "new_product"):
            await self._fire_watch_alerts(product, qty, market_price)

        return sent

    async def _fire_watch_alerts(self, product: Product, quantity: Optional[int], market_price: Optional[float]):
        """Send personalised watch alerts to any user watching this product."""
        try:
            watches = self.db.get_all_watches()
            title_lower = product.title.lower()
            for watch in watches:
                words = watch["query"].lower().split()
                if all(w in title_lower for w in words):
                    await self.tg.send_watch_alert(
                        chat_id=watch["chat_id"],
                        product_name=product.title,
                        site_name=product.site_name,
                        price=product.price,
                        url=product.url,
                        quantity=quantity,
                        market_price_cad=market_price,
                    )
        except Exception as exc:
            log.debug("Watch alert error: %s", exc)

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
                elif scraper_type in ("playwright", "gamestop"):
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

    def _is_local_drop_post(self, title: str, source_key: str = "") -> bool:
        """
        Return True if this post is worth sending as a drop alert.

        Detection rules (all require a game keyword match first):
          1. Standard:             location + drop keyword   (in-store find, live restock)
          2. Advance intel:        location + advance word   (upcoming drop, time/units mentioned)
          3. Canada-only source + drop keyword               (sub is Canada-specific, no city needed)
          4. Canada-only source + advance word               (upcoming drop in CA-specific sub)
        """
        drop_cfg    = self.config.get("drop_alerts", {})
        loc_kws     = drop_cfg.get("location_keywords", [])
        drop_kws    = drop_cfg.get("drop_keywords", [])
        advance_kws = drop_cfg.get("advance_keywords", [])
        game_kws    = drop_cfg.get("game_keywords", ["pokemon", "pokémon", "one piece", "tcg"])
        ca_sources  = drop_cfg.get("canada_only_sources", [])

        t = title.lower()

        # Game must always be present
        if not any(kw in t for kw in game_kws):
            return False

        has_location = any(kw in t for kw in loc_kws)
        has_drop     = any(kw in t for kw in drop_kws)
        has_advance  = any(kw in t for kw in advance_kws)
        is_ca_source = source_key in ca_sources

        # Rule 1: live in-store find / restock with a specific location
        if has_location and has_drop:
            return True
        # Rule 2: advance intel with specific location ("Costco Newmarket this Saturday 6am")
        if has_location and has_advance:
            return True
        # Rule 3: inherently Canadian sub — location is implicit, just needs a drop signal
        if is_ca_source and has_drop:
            return True
        # Rule 4: inherently Canadian sub + advance intel ("Pokemon ETBs dropping this weekend")
        if is_ca_source and has_advance:
            return True

        return False

    async def check_news(self):
        news_sources = self.config.get("news_sources", {})
        news_scraper = self._get_scraper("news")
        if not news_scraper:
            from scrapers.news_scraper import NewsScraper
            news_scraper = NewsScraper(proxies=self.proxies, delay_min=1, delay_max=3)
            self._scrapers["news"] = news_scraper

        drop_alerts_enabled = self.config.get("drop_alerts_enabled", False)

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
                        tag = "[DROP]" if self._is_local_drop_post(item.title, source_key) else "[NEWS]"
                        print(f"  {tag} [{item.source_name}] {item.title}\n    URL: {item.url}")
                        continue

                    # Local GTA drop posts bypass the news_alerts_enabled flag
                    if drop_alerts_enabled and self._is_local_drop_post(item.title, source_key):
                        sent = await self.tg.send_drop_location(item.source_name, item.title, item.url)
                        if sent:
                            self.db.mark_news_alerted(item.url)
                            self.db.log_alert(
                                site_key=source_key,
                                site_name=item.source_name,
                                title=item.title,
                                alert_type="drop_location",
                                url=item.url,
                            )
                        continue

                    if not self.config.get("news_alerts_enabled", True):
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
    # StockTrack drop source check                                         #
    # ------------------------------------------------------------------ #

    async def check_drop_sources(self):
        drop_sources = self.config.get("drop_sources", {})
        for source_key, source_cfg in drop_sources.items():
            if not source_cfg.get("enabled", True):
                continue
            source_type = source_cfg.get("type")
            if source_type not in ("stocktrack", "nowinstock"):
                continue

            site_name = source_cfg["name"]
            scraper = self._get_scraper(source_type)
            if not scraper:
                continue

            try:
                products = await scraper.scrape_all(source_key, site_name)
                log.info("%s: scraped %d products", site_name, len(products))

                for product in products:
                    change = self.db.upsert_product(
                        site_key=source_key,
                        site_name=site_name,
                        product_id=product.product_id,
                        title=product.title,
                        url=product.url,
                        price=product.price,
                        in_stock=product.in_stock,
                    )

                    if self.test_mode:
                        status = "IN" if product.in_stock else "OUT"
                        flags = []
                        if change["is_new"]:
                            flags.append("NEW")
                        if change["stock_changed"]:
                            flags.append("STOCK CHANGED")
                        print(
                            f"  [StockTrack] {product.title} | "
                            f"{'${:.2f}'.format(product.price) if product.price else 'N/A'} | "
                            f"{status} | {', '.join(flags) or 'unchanged'}"
                        )
                        continue

                    cooldown_ok = not self.db.was_recently_alerted(
                        source_key, product.product_id, self.alert_cooldown_hours
                    )
                    if not cooldown_ok:
                        continue

                    # Extract retailer/location from raw metadata
                    raw = product.raw if hasattr(product, "raw") and product.raw else {}
                    retailer = raw.get("retailer", site_name)
                    location = raw.get("location", "Online")

                    alert_type = None
                    if change["is_new"] and product.in_stock:
                        alert_type = "new_product"
                    elif change["stock_changed"] and product.in_stock and not change["old_in_stock"]:
                        alert_type = "restock"
                    elif (
                        change["price_changed"]
                        and product.price is not None
                        and change["old_price"] is not None
                        and product.price < change["old_price"]
                        and product.in_stock
                    ):
                        drop_abs = change["old_price"] - product.price
                        drop_pct = drop_abs / change["old_price"] * 100
                        if drop_pct >= self.min_price_drop_pct or drop_abs >= self.min_price_drop_abs:
                            alert_type = "price_drop"

                    if alert_type:
                        # Strip " @ Retailer [Location]" from the display name
                        display_name = product.title.split(" @ ")[0] if " @ " in product.title else product.title
                        sent = await self.tg.send_online_stock_alert(
                            product_name=display_name,
                            retailer=retailer,
                            location=location,
                            price=product.price,
                            url=product.url,
                            alert_type=alert_type,
                            old_price=change.get("old_price"),
                        )
                        if sent:
                            self.db.mark_alerted(source_key, product.product_id, alert_type)
                            self.db.log_alert(
                                site_key=source_key,
                                site_name=site_name,
                                title=product.title,
                                alert_type=alert_type,
                                product_id=product.product_id,
                                url=product.url,
                                price=product.price,
                            )
            except Exception as exc:
                log.error("Drop source check failed for %s: %s", source_key, exc)

    # ------------------------------------------------------------------ #
    # Health check                                                         #
    # ------------------------------------------------------------------ #

    async def health_check(self):
        statuses = self.db.get_all_site_status()
        if not self.test_mode:
            await self.tg.send_health_check(statuses)
        log.info("Health check sent (%d sites)", len(statuses))

        # Stale site check — consolidate into one message to avoid spam
        stale_threshold = timedelta(minutes=self.stale_minutes)
        now = datetime.utcnow()
        stale_sites = []
        for s in statuses:
            last = s.get("last_check")
            if last:
                last_dt = datetime.fromisoformat(last)
                if (now - last_dt) > stale_threshold:
                    log.warning("%s hasn't been checked in >%s min", s['site_name'], self.stale_minutes)
                    stale_sites.append(s['site_name'])
        if stale_sites and not self.test_mode:
            names = "\n".join(f"  • {n}" for n in stale_sites)
            await self.tg.send(
                f"⚠️ <b>{len(stale_sites)} site(s) stale</b> (no check in >{self.stale_minutes} min):\n{names}"
            )

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

        print("\n--- Drop Sources (StockTrack / NowinStock) ---")
        await self.check_drop_sources()

        print("\n--- News / Reddit Sources ---")
        await self.check_news()

        print("\n=== TEST RUN COMPLETE ===")

    async def _check_release_calendar(self):
        """Send reminders for set releases 3 days, 1 day, and day-of."""
        import os
        calendar = self.config.get("release_calendar", [])
        if not calendar:
            return
        today = datetime.now(EST).date()
        today_str = today.isoformat()
        if self._last_calendar_check == today_str:
            return
        self._last_calendar_check = today_str

        for entry in calendar:
            try:
                rel_date = datetime.strptime(entry["release_date"], "%Y-%m-%d").date()
                days_until = (rel_date - today).days
                if days_until not in (0, 1, 3):
                    continue
                set_name = entry["name"]
                flag_path = f"data/.cal_{abs(hash(set_name + today_str + str(days_until))) % 999999}"
                if os.path.exists(flag_path):
                    continue
                if not self.test_mode:
                    sent = await self.tg.send_release_reminder(
                        set_name, entry["release_date"], days_until, entry.get("notes", "")
                    )
                    if sent:
                        open(flag_path, "w").close()
                else:
                    print(f"  [CALENDAR] {set_name} in {days_until} day(s)")
            except Exception as exc:
                log.debug("Calendar check error: %s", exc)

    def _get_retail_interval(self) -> float:
        """Return a sleep interval (seconds) based on time of day.

        Weekdays 2–5 pm EST = online restock window → scrape faster (30–45s).
        All other times → normal cadence (retail_min–retail_max).
        """
        now_est = datetime.now(EST)
        if now_est.weekday() < 5 and 14 <= now_est.hour < 17:
            return random.uniform(30, 45)
        return random.uniform(self.retail_min, self.retail_max)

    async def run(self):
        """Main production loop."""
        self._running = True
        log.info("PokéMonitor starting (test_mode=%s)", self.test_mode)

        if not self.test_mode:
            # Start Telegram command handler as background task
            from bot.command_handler import CommandHandler
            cmd_handler = CommandHandler(self.config, self.db, self.tg)
            asyncio.create_task(cmd_handler.poll())
            log.info("Telegram command handler started")

            await self.tg.send("🤖 <b>PokéMonitor started!</b> Bot is now monitoring stores and news.\n\nType /help to see available commands.")

        sites = self.config.get("sites", {})
        drop_interval = self.config.get("drop_check_interval_seconds", 300)
        last_news_check = datetime.utcnow() - timedelta(seconds=self.news_max)
        last_health_check = datetime.utcnow() - timedelta(seconds=self.health_interval)
        last_drop_check = datetime.utcnow() - timedelta(seconds=drop_interval)

        while self._running:
            # Retail sites
            for site_key, site_config in sites.items():
                if not self._running:
                    break
                await self.check_site(site_key, site_config)
                # Stagger inter-site delay
                await asyncio.sleep(random.uniform(self.delay_min, self.delay_max))

            now = datetime.utcnow()

            # Drop sources (StockTrack / NowinStock) — every 5 min
            if (now - last_drop_check).total_seconds() >= drop_interval:
                await self.check_drop_sources()
                last_drop_check = datetime.utcnow()

            # News / Reddit check (includes local drop post detection)
            if (now - last_news_check).total_seconds() >= random.uniform(self.news_min, self.news_max):
                await self.check_news()
                last_news_check = datetime.utcnow()

            # Health check
            if (now - last_health_check).total_seconds() >= self.health_interval:
                await self.health_check()
                last_health_check = datetime.utcnow()

            # Daily digest
            await self._maybe_send_daily_digest()

            # Release calendar reminders (once per day)
            await self._check_release_calendar()

            # Wait before next full retail cycle — faster during online restock window
            wait = self._get_retail_interval()
            log.debug("Sleeping %.1fs before next cycle", wait)
            await asyncio.sleep(wait)

    def stop(self):
        self._running = False
        log.info("Monitor stop requested")
