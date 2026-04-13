"""
News scraper for:
  - RSS feeds (PokeBeach, PTCGOne, TCGPlayer, YouTube channels)
  - Reddit JSON API (r/PokemonTCG, r/OnePieceTCG)
  - Scraped pages (One Piece official, Bandai)
"""

import asyncio
import random
import re
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

import aiohttp
import feedparser

from scrapers.base import BaseScraper, random_user_agent
from bot.logger_setup import get_logger

log = get_logger(__name__)

NEWS_KEYWORDS = [
    "pokemon", "pokémon", "one piece", "tcg", "booster", "set",
    "release", "reveal", "announce", "card", "tournament", "price",
    "print run", "reprint", "expansion", "etb", "elite trainer",
    "scarlet", "violet", "sword", "shield",
]


@dataclass
class NewsItem:
    source_key: str
    source_name: str
    title: str
    url: str
    published: Optional[str] = None


def _is_relevant_news(title: str) -> bool:
    title_lower = title.lower()
    return any(kw in title_lower for kw in NEWS_KEYWORDS)


class NewsScraper(BaseScraper):
    """Scrapes news from RSS feeds, Reddit, and HTML pages."""

    async def scrape_source(self, source_key: str, source: dict) -> List[NewsItem]:
        stype = source.get("type", "rss")
        url = source["url"]
        name = source["name"]

        try:
            if stype == "rss":
                return await self._scrape_rss(source_key, name, url)
            elif stype == "rss_playwright":
                return await self._scrape_rss_playwright(source_key, name, url)
            elif stype == "reddit":
                return await self._scrape_reddit(source_key, name, url)
            elif stype == "scrape":
                return await self._scrape_html(source_key, name, url)
        except Exception as exc:
            log.error("News scrape failed for %s: %s", source_key, exc)
        return []

    # ------------------------------------------------------------------ #
    # RSS                                                                  #
    # ------------------------------------------------------------------ #

    async def _scrape_rss(self, source_key: str, name: str, url: str) -> List[NewsItem]:
        await asyncio.sleep(random.uniform(1, 3))
        status, text = await self._get(
            url,
            extra_headers={"Accept": "application/rss+xml, application/xml, text/xml, */*"},
        )
        if not text:
            log.debug("RSS empty response for %s", url)
            return []

        feed = feedparser.parse(text)
        items = []
        for entry in feed.entries[:20]:
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            if not title or not link:
                continue
            published = None
            if hasattr(entry, "published"):
                published = entry.published
            elif hasattr(entry, "updated"):
                published = entry.updated

            items.append(NewsItem(
                source_key=source_key,
                source_name=name,
                title=title,
                url=link,
                published=published,
            ))
        log.debug("RSS %s: found %d items", name, len(items))
        return items

    # ------------------------------------------------------------------ #
    # Reddit                                                               #
    # ------------------------------------------------------------------ #

    async def _scrape_reddit(self, source_key: str, name: str, url: str) -> List[NewsItem]:
        await asyncio.sleep(random.uniform(1, 3))
        status, data = await self._get(
            url,
            extra_headers={
                "Accept": "application/json",
                "User-Agent": "pokemonitor-bot/1.0 (monitoring TCG news)",
            },
            json=True,
        )
        if not data:
            return []

        items = []
        posts = data.get("data", {}).get("children", [])
        for post in posts[:25]:
            pd = post.get("data", {})
            title = pd.get("title", "").strip()
            permalink = pd.get("permalink", "")
            link = f"https://www.reddit.com{permalink}" if permalink else pd.get("url", "")
            score = pd.get("score", 0)
            created = pd.get("created_utc")
            published = datetime.utcfromtimestamp(created).isoformat() if created else None

            if not title or not link:
                continue
            # Allow score 0+ so brand-new posts (just submitted) are caught immediately
            # Drop posts need to arrive before shelves are cleared
            if score < 0:
                continue

            items.append(NewsItem(
                source_key=source_key,
                source_name=name,
                title=title,
                url=link,
                published=published,
            ))
        log.debug("Reddit %s: found %d posts", name, len(items))
        return items

    # ------------------------------------------------------------------ #
    # RSS via Playwright (for Cloudflare-protected feeds)                 #
    # ------------------------------------------------------------------ #

    async def _scrape_rss_playwright(self, source_key: str, name: str, url: str) -> List[NewsItem]:
        """Fetch an RSS feed via Playwright to bypass Cloudflare/bot protection."""
        try:
            from scrapers.playwright_scraper import new_context
            from playwright_stealth import Stealth as _Stealth
            _stealth = _Stealth()
        except ImportError:
            log.warning("Playwright not available, falling back to plain RSS for %s", name)
            return await self._scrape_rss(source_key, name, url)

        context = await new_context()
        page = await context.new_page()
        items = []
        try:
            await _stealth.apply_stealth_async(page)
            await page.goto(url, wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(3000)
            # The page may render the XML feed as text
            content = await page.content()
            feed = feedparser.parse(content)
            for entry in feed.entries[:20]:
                title = entry.get("title", "").strip()
                link = entry.get("link", "").strip()
                if title and link:
                    items.append(NewsItem(
                        source_key=source_key, source_name=name,
                        title=title, url=link,
                        published=getattr(entry, "published", None),
                    ))
            if not items:
                # Fallback: scrape article links from the rendered page
                items = await self._scrape_html(source_key, name, url)
        except Exception as exc:
            log.error("RSS Playwright scrape failed for %s: %s", name, exc)
        finally:
            await page.close()
            await context.close()
        return items

    # ------------------------------------------------------------------ #
    # HTML scrape                                                          #
    # ------------------------------------------------------------------ #

    async def _scrape_html(self, source_key: str, name: str, url: str) -> List[NewsItem]:
        from bs4 import BeautifulSoup

        await asyncio.sleep(random.uniform(1, 3))
        status, html = await self._get(url)
        if not html:
            return []

        soup = BeautifulSoup(html, "lxml")
        items = []

        # Generic news article selectors
        selectors = [
            "article a[href]",
            ".news-item a[href]",
            ".post-title a[href]",
            "h2 a[href]", "h3 a[href]",
            ".entry-title a[href]",
            "[class*='news'] a[href]",
            "[class*='article'] a[href]",
        ]

        seen_hrefs = set()
        for sel in selectors:
            for el in soup.select(sel):
                href = el.get("href", "").strip()
                if not href:
                    continue
                if not href.startswith("http"):
                    from urllib.parse import urljoin
                    href = urljoin(url, href)
                if href in seen_hrefs:
                    continue
                seen_hrefs.add(href)

                title = el.get_text(strip=True)
                if not title or len(title) < 5:
                    # Try parent element text
                    parent = el.parent
                    if parent:
                        title = parent.get_text(strip=True)[:200]

                if title:
                    items.append(NewsItem(
                        source_key=source_key,
                        source_name=name,
                        title=title,
                        url=href,
                    ))
                if len(items) >= 25:
                    break
            if len(items) >= 25:
                break

        log.debug("HTML scrape %s: found %d items", name, len(items))
        return items
