"""Telegram notification service."""

import asyncio
from datetime import datetime
from typing import Optional

import aiohttp
import pytz

from bot.logger_setup import get_logger

log = get_logger(__name__)
EST = pytz.timezone("America/Toronto")


def _now_est() -> str:
    return datetime.now(EST).strftime("%Y-%m-%d %H:%M:%S EST")


class TelegramService:
    BASE_URL = "https://api.telegram.org/bot{token}/sendMessage"
    PHOTO_URL = "https://api.telegram.org/bot{token}/sendPhoto"

    def __init__(self, token: str, chat_id: str, extra_chat_ids: Optional[list] = None):
        self.token = token
        self.chat_id = str(chat_id)
        # All chat IDs that receive push alerts
        self.all_chat_ids: list = [self.chat_id] + [str(x) for x in (extra_chat_ids or [])]
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            )
        return self._session

    async def _send_to(self, chat_id: str, text: str, disable_preview: bool = True) -> bool:
        """Send a message to a specific chat ID."""
        url = self.BASE_URL.format(token=self.token)
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": disable_preview,
        }
        try:
            session = await self._get_session()
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    return True
                body = await resp.text()
                log.error("Telegram error %s to chat %s: %s", resp.status, chat_id, body)
                return False
        except Exception as exc:
            log.error("Telegram send failed to %s: %s", chat_id, exc)
            return False

    async def send(self, text: str, disable_preview: bool = True) -> bool:
        """Broadcast a message to all authorised chat IDs. Returns True if at least one succeeds."""
        results = await asyncio.gather(
            *[self._send_to(cid, text, disable_preview) for cid in self.all_chat_ids],
            return_exceptions=True,
        )
        return any(r is True for r in results)

    async def send_with_photo(self, caption: str, image_url: Optional[str]) -> bool:
        """Send a photo with caption. Falls back to plain text if no image or sendPhoto fails."""
        if not image_url:
            return await self.send(caption)
        url = self.PHOTO_URL.format(token=self.token)
        payload = {
            "chat_id": self.chat_id,
            "photo": image_url,
            "caption": caption[:1024],
            "parse_mode": "HTML",
        }
        try:
            session = await self._get_session()
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    return True
                log.warning("sendPhoto failed (%s), falling back to text", resp.status)
                return await self.send(caption)
        except Exception as exc:
            log.warning("sendPhoto exception (%s), falling back to text", exc)
            return await self.send(caption)

    # ------------------------------------------------------------------ #
    # Formatted alert helpers                                              #
    # ------------------------------------------------------------------ #

    async def send_pokemon_center_alert(
        self,
        product_name: str,
        price: Optional[float],
        url: str,
        alert_type: str = "restock",
        image_url: Optional[str] = None,
    ) -> bool:
        price_str = f"${price:.2f} CAD" if price else "N/A"
        label = "RESTOCK" if alert_type == "restock" else "NEW LISTING"
        msg = (
            f"🚨🚨 <b>POKEMON CENTER CA — {label}</b> 🚨🚨\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📦 <b>{product_name}</b>\n"
            f"💰 <b>Price:</b> {price_str}\n"
            f"🔗 <b>BUY NOW:</b> {url}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⚡ Ships direct from Pokemon — act fast!\n"
            f"🕐 {_now_est()}"
        )
        return await self.send_with_photo(msg, image_url)

    async def send_restock(
        self,
        site_name: str,
        product_name: str,
        price: Optional[float],
        url: str,
        image_url: Optional[str] = None,
    ) -> bool:
        price_str = f"${price:.2f} CAD" if price else "N/A"
        msg = (
            f"🟢 <b>RESTOCK ALERT</b>\n"
            f"🏪 <b>Store:</b> {site_name}\n"
            f"📦 <b>Product:</b> {product_name}\n"
            f"💰 <b>Price:</b> {price_str}\n"
            f"✅ <b>Status:</b> In Stock\n"
            f"🔗 <b>URL:</b> {url}\n"
            f"🕐 <b>Time:</b> {_now_est()}"
        )
        return await self.send_with_photo(msg, image_url)

    async def send_new_product(
        self,
        site_name: str,
        product_name: str,
        price: Optional[float],
        url: str,
        in_stock: bool,
        image_url: Optional[str] = None,
    ) -> bool:
        price_str = f"${price:.2f} CAD" if price else "N/A"
        stock_str = "✅ In Stock" if in_stock else "❌ Out of Stock"
        msg = (
            f"🆕 <b>NEW PRODUCT LISTED</b>\n"
            f"🏪 <b>Store:</b> {site_name}\n"
            f"📦 <b>Product:</b> {product_name}\n"
            f"💰 <b>Price:</b> {price_str}\n"
            f"✅ <b>Status:</b> {stock_str}\n"
            f"🔗 <b>URL:</b> {url}\n"
            f"🕐 <b>Time:</b> {_now_est()}"
        )
        return await self.send_with_photo(msg, image_url)

    async def send_price_drop(
        self,
        site_name: str,
        product_name: str,
        old_price: float,
        new_price: float,
        url: str,
        image_url: Optional[str] = None,
    ) -> bool:
        drop_pct = ((old_price - new_price) / old_price) * 100 if old_price > 0 else 0
        msg = (
            f"💸 <b>PRICE DROP</b>\n"
            f"🏪 <b>Store:</b> {site_name}\n"
            f"📦 <b>Product:</b> {product_name}\n"
            f"💰 <b>Old Price:</b> ${old_price:.2f} CAD\n"
            f"💰 <b>New Price:</b> ${new_price:.2f} CAD  ({drop_pct:.0f}% off)\n"
            f"🔗 <b>URL:</b> {url}\n"
            f"🕐 <b>Time:</b> {_now_est()}"
        )
        return await self.send_with_photo(msg, image_url)

    async def send_out_of_stock(
        self,
        site_name: str,
        product_name: str,
        url: str,
    ) -> bool:
        msg = (
            f"🔴 <b>OUT OF STOCK</b>\n"
            f"🏪 <b>Store:</b> {site_name}\n"
            f"📦 <b>Product:</b> {product_name}\n"
            f"❌ <b>Status:</b> Out of Stock\n"
            f"🔗 <b>URL:</b> {url}\n"
            f"🕐 <b>Time:</b> {_now_est()}"
        )
        return await self.send(msg)

    async def send_drop_location(
        self,
        source_name: str,
        title: str,
        url: str,
    ) -> bool:
        msg = (
            f"📍 <b>LOCAL DROP ALERT</b>\n"
            f"📡 <b>Source:</b> {source_name}\n"
            f"📋 <b>Post:</b> {title}\n"
            f"🔗 <b>URL:</b> {url}\n"
            f"🕐 <b>Time:</b> {_now_est()}"
        )
        return await self.send(msg)

    async def send_online_stock_alert(
        self,
        product_name: str,
        retailer: str,
        location: str,
        price: Optional[float],
        url: str,
        alert_type: str = "restock",
        old_price: Optional[float] = None,
    ) -> bool:
        price_str = f"${price:.2f} CAD" if price else "N/A"
        icon = "🟢" if alert_type == "restock" else "🆕"
        label = "RESTOCK" if alert_type == "restock" else "NOW AVAILABLE"
        loc_str = f" — {location}" if location and location not in ("Online", "Canada", "Ontario") else ""
        price_line = ""
        if alert_type == "price_drop" and old_price and price:
            drop_pct = ((old_price - price) / old_price) * 100
            price_line = (
                f"💰 <b>Price:</b> <s>${old_price:.2f}</s> → ${price:.2f} CAD  ({drop_pct:.0f}% off)\n"
            )
        else:
            price_line = f"💰 <b>Price:</b> {price_str}\n"
        msg = (
            f"{icon} <b>STOCK TRACKER — {label}</b>\n"
            f"🏪 <b>Retailer:</b> {retailer}{loc_str}\n"
            f"📦 <b>Product:</b> {product_name}\n"
            f"{price_line}"
            f"🔗 <b>URL:</b> {url}\n"
            f"🕐 <b>Time:</b> {_now_est()}"
        )
        return await self.send(msg)

    async def send_news(
        self,
        source_name: str,
        title: str,
        url: str,
    ) -> bool:
        msg = (
            f"📰 <b>NEWS ALERT</b>\n"
            f"📡 <b>Source:</b> {source_name}\n"
            f"📋 <b>Title:</b> {title}\n"
            f"🔗 <b>URL:</b> {url}\n"
            f"🕐 <b>Time:</b> {_now_est()}"
        )
        return await self.send(msg)

    async def send_health_check(self, site_statuses: list) -> bool:
        lines = [f"⚠️ <b>BOT HEALTH CHECK</b> — {_now_est()}\n"]
        for s in site_statuses:
            last = s.get("last_check", "never")
            status = s.get("status", "?")
            icon = "✅" if status == "ok" else "❌"
            lines.append(f"{icon} {s['site_name']}: last check {last}")
        return await self.send("\n".join(lines))

    async def send_bot_down(self, reason: str) -> bool:
        msg = (
            f"⚠️ <b>BOT DOWN ALERT</b>\n"
            f"🕐 <b>Time:</b> {_now_est()}\n"
            f"ℹ️ <b>Reason:</b> {reason}"
        )
        return await self.send(msg)

    async def send_daily_digest(self, alerts: list, news_items: list) -> bool:
        from datetime import datetime as _dt
        import pytz as _pytz
        est = _pytz.timezone("America/Toronto")
        today = _dt.now(est).strftime("%b %d")

        restocks   = [a for a in alerts if a["alert_type"] == "restock"]
        new_prods  = [a for a in alerts if a["alert_type"] == "new_product"]
        drops      = [a for a in alerts if a["alert_type"] == "price_drop"]
        loc_drops  = [a for a in alerts if a["alert_type"] == "drop_location"]

        lines = [f"☀️ <b>TCG Briefing — {today}</b>\n"]

        if loc_drops:
            lines.append(f"📍 <b>Local Drops ({len(loc_drops)})</b>")
            for a in loc_drops[:4]:
                lines.append(f"   • {a['title'][:70]}")
            lines.append("")

        if restocks:
            lines.append(f"🟢 <b>Restocks ({len(restocks)})</b>")
            for a in restocks[:4]:
                p = f"${a['price']:.2f}" if a.get("price") else "?"
                lines.append(f"   • {a['title'][:55]} — {p} @ {a['site_name']}")
            lines.append("")

        if new_prods:
            lines.append(f"🆕 <b>New Listings ({len(new_prods)})</b>")
            for a in new_prods[:4]:
                p = f"${a['price']:.2f}" if a.get("price") else "?"
                lines.append(f"   • {a['title'][:55]} — {p} @ {a['site_name']}")
            lines.append("")

        if drops:
            lines.append(f"💰 <b>Price Drops ({len(drops)})</b>")
            for a in drops[:3]:
                p = f"${a['price']:.2f}" if a.get("price") else "?"
                lines.append(f"   • {a['title'][:55]} → {p} @ {a['site_name']}")
            lines.append("")

        if news_items:
            lines.append(f"📰 <b>{len(news_items)} news items</b> — /news to read")
            lines.append("")

        if not any([restocks, new_prods, drops, loc_drops, news_items]):
            lines.append("😴 Quiet day — nothing notable in the last 24h.")
        else:
            lines.append("<i>/drops • /prices • /ask anything</i>")

        return await self.send("\n".join(lines))

    async def send_suspicious_price(
        self,
        site_name: str,
        product_name: str,
        price: float,
        url: str,
        reason: str,
    ) -> bool:
        msg = (
            f"⚠️ <b>SUSPICIOUS PRICE FLAGGED (logged only)</b>\n"
            f"🏪 {site_name}\n"
            f"📦 {product_name}\n"
            f"💰 ${price:.2f} CAD — {reason}\n"
            f"🔗 {url}"
        )
        log.warning("Suspicious price: %s | %s | $%.2f | %s | %s", site_name, product_name, price, reason, url)
        # Note: do NOT send to Telegram per spec — just log
        return True

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
