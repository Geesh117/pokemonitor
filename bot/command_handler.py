"""
Telegram command handler — long polling for incoming messages.

Supported commands:
  /help    — show all commands
  /drops   — recent drop alerts (last 72h)
  /news    — latest TCG news (last 24h)
  /sales   — recent price drops (last 48h)
  /prices  — search current prices across all stores
  /stores  — list all monitored stores
  /status  — store health check (last check time, errors)
  /start   — welcome + show chat ID (useful for new users getting access)
"""

import asyncio
from datetime import datetime, timedelta
from typing import Optional

import aiohttp
import pytz

from bot.logger_setup import get_logger

log = get_logger(__name__)
EST = pytz.timezone("America/Toronto")


def _now_est() -> str:
    return datetime.now(EST).strftime("%Y-%m-%d %H:%M:%S EST")


def _fmt_time(iso: Optional[str]) -> str:
    if not iso:
        return "never"
    return iso[:16].replace("T", " ") + " UTC"


class CommandHandler:
    """
    Polls Telegram for incoming messages and responds to slash commands.
    Runs as a background asyncio task alongside the monitoring loop.
    """

    def __init__(self, config: dict, db, telegram_service):
        self.config = config
        self.db = db
        self.tg = telegram_service

        tg_cfg = config["telegram"]
        self.token = tg_cfg["bot_token"]
        self._base = f"https://api.telegram.org/bot{self.token}"

        # All chat IDs authorised to use commands
        main_id = str(tg_cfg.get("chat_id", ""))
        extra = [str(x) for x in tg_cfg.get("extra_chat_ids", [])]
        self.allowed_ids: set = set([main_id] + extra) - {""}

        self._offset: Optional[int] = None
        self._running = False

    # ------------------------------------------------------------------ #
    # Polling loop                                                         #
    # ------------------------------------------------------------------ #

    async def poll(self):
        self._running = True
        log.info("Command handler started — %d authorised chat IDs", len(self.allowed_ids))
        while self._running:
            try:
                updates = await self._get_updates()
                for update in updates:
                    self._offset = update["update_id"] + 1
                    try:
                        await self._handle_update(update)
                    except Exception as exc:
                        log.error("Command handler error: %s", exc)
            except Exception as exc:
                log.error("Poll loop error: %s", exc)
                await asyncio.sleep(5)

    def stop(self):
        self._running = False

    # ------------------------------------------------------------------ #
    # Telegram API helpers                                                 #
    # ------------------------------------------------------------------ #

    async def _get_updates(self) -> list:
        params = {"timeout": 30, "allowed_updates": ["message"]}
        if self._offset is not None:
            params["offset"] = self._offset
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    f"{self._base}/getUpdates",
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=40),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("result", [])
        except asyncio.TimeoutError:
            pass
        except Exception as exc:
            log.debug("getUpdates error: %s", exc)
        return []

    async def _reply(self, chat_id: str, text: str):
        """Send a reply to a specific chat (not broadcast — used for command responses)."""
        try:
            async with aiohttp.ClientSession() as s:
                await s.post(
                    f"{self._base}/sendMessage",
                    json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                          "disable_web_page_preview": True},
                    timeout=aiohttp.ClientTimeout(total=10),
                )
        except Exception as exc:
            log.error("Reply to %s failed: %s", chat_id, exc)

    # ------------------------------------------------------------------ #
    # Update dispatcher                                                    #
    # ------------------------------------------------------------------ #

    async def _handle_update(self, update: dict):
        message = update.get("message", {})
        text = message.get("text", "").strip()
        if not text.startswith("/"):
            return

        chat_id = str(message.get("chat", {}).get("id", ""))
        username = message.get("from", {}).get("username", "unknown")
        cmd = text.split()[0].lower().split("@")[0]
        args = text.split()[1:]

        # Always allow /start so new users can get their chat ID
        if cmd == "/start":
            await self._cmd_start(chat_id, username)
            return

        if chat_id not in self.allowed_ids:
            log.info("Unauthorised command from chat_id=%s (@%s): %s", chat_id, username, cmd)
            await self._reply(
                chat_id,
                f"⛔ <b>Not authorised.</b>\n\n"
                f"Your chat ID is: <code>{chat_id}</code>\n"
                f"Share this with the bot owner to get access.",
            )
            return

        log.info("Command %s from @%s (chat_id=%s)", cmd, username, chat_id)

        dispatch = {
            "/help":   self._cmd_help,
            "/drops":  self._cmd_drops,
            "/news":   self._cmd_news,
            "/sales":  self._cmd_sales,
            "/prices": self._cmd_prices,
            "/stores": self._cmd_stores,
            "/status": self._cmd_status,
        }

        handler = dispatch.get(cmd)
        if handler:
            await handler(chat_id, args)
        else:
            await self._reply(chat_id, f"Unknown command: <code>{cmd}</code>\nType /help to see all commands.")

    # ------------------------------------------------------------------ #
    # /start                                                               #
    # ------------------------------------------------------------------ #

    async def _cmd_start(self, chat_id: str, username: str):
        if chat_id in self.allowed_ids:
            await self._reply(
                chat_id,
                f"👋 Welcome back, <b>@{username}</b>!\n\n"
                f"Type /help to see all available commands.\n"
                f"🕐 {_now_est()}",
            )
        else:
            await self._reply(
                chat_id,
                f"👋 Hey <b>@{username}</b>! This is a private TCG monitoring bot.\n\n"
                f"To get access, share your chat ID with the bot owner:\n"
                f"<code>{chat_id}</code>\n\n"
                f"They'll add you to the bot and you'll be able to:\n"
                f"• Get real-time alerts for Pokemon & One Piece drops\n"
                f"• Use commands like /drops, /news, /prices\n"
                f"• See restocks the second they happen across 19+ stores",
            )

    # ------------------------------------------------------------------ #
    # /help                                                                #
    # ------------------------------------------------------------------ #

    async def _cmd_help(self, chat_id: str, args: list):
        msg = (
            "🤖 <b>PokéMonitor — Commands</b>\n\n"
            "📍 <b>/drops</b>\n"
            "    Recent drop alerts — in-store finds, Reddit posts,\n"
            "    StockTrack restocks (last 72h)\n\n"
            "📰 <b>/news</b>\n"
            "    Latest Pokemon & One Piece TCG news (last 24h)\n\n"
            "🟢 <b>/sales</b>\n"
            "    Recent price drops across all monitored stores (last 48h)\n\n"
            "🔍 <b>/prices</b> <i>[product name]</i>\n"
            "    Search current in-stock prices across all stores\n"
            "    Example: <code>/prices prismatic etb</code>\n"
            "    Example: <code>/prices one piece op07</code>\n\n"
            "🏪 <b>/stores</b>\n"
            "    All 19 monitored stores and their websites\n\n"
            "📊 <b>/status</b>\n"
            "    Store health — last check time, any errors\n\n"
            "❓ <b>/help</b>\n"
            "    Show this message\n\n"
            f"🕐 {_now_est()}"
        )
        await self._reply(chat_id, msg)

    # ------------------------------------------------------------------ #
    # /drops                                                               #
    # ------------------------------------------------------------------ #

    async def _cmd_drops(self, chat_id: str, args: list):
        alerts = self.db.get_alerts_since(hours=72)
        drop_types = {"drop_location", "restock", "new_product"}
        drops = [a for a in alerts if a["alert_type"] in drop_types][:10]

        if not drops:
            await self._reply(chat_id, "📍 No drop alerts in the last 72 hours.")
            return

        lines = ["📍 <b>Recent Drops (last 72h)</b>\n"]
        for a in drops:
            t = _fmt_time(a.get("sent_at"))
            icon = {"drop_location": "📍", "restock": "🟢", "new_product": "🆕"}.get(a["alert_type"], "📦")
            price_str = f" — ${a['price']:.2f} CAD" if a.get("price") else ""
            lines.append(f"{icon} <b>{a['site_name']}</b>{price_str}")
            lines.append(f"    {a['title'][:90]}")
            lines.append(f"    <i>{t}</i>")
            if a.get("url"):
                lines.append(f"    {a['url']}")
            lines.append("")

        await self._reply(chat_id, "\n".join(lines).strip())

    # ------------------------------------------------------------------ #
    # /news                                                                #
    # ------------------------------------------------------------------ #

    async def _cmd_news(self, chat_id: str, args: list):
        cutoff = (datetime.utcnow() - timedelta(hours=24)).isoformat()
        news = self.db.get_recent_news(limit=50)
        recent = [n for n in news if n.get("first_seen", "") > cutoff][:10]

        if not recent:
            await self._reply(chat_id, "📰 No new TCG news in the last 24 hours.")
            return

        lines = ["📰 <b>Latest TCG News (last 24h)</b>\n"]
        for n in recent:
            lines.append(f"📡 <b>{n['source_name']}</b>")
            lines.append(f"    {n['title'][:100]}")
            lines.append(f"    {n['article_url']}")
            lines.append("")

        await self._reply(chat_id, "\n".join(lines).strip())

    # ------------------------------------------------------------------ #
    # /sales                                                               #
    # ------------------------------------------------------------------ #

    async def _cmd_sales(self, chat_id: str, args: list):
        alerts = self.db.get_alerts_since(hours=48)
        sales = [a for a in alerts if a["alert_type"] == "price_drop"][:10]

        if not sales:
            await self._reply(chat_id, "🟢 No price drops detected in the last 48 hours.")
            return

        lines = ["🟢 <b>Recent Price Drops (last 48h)</b>\n"]
        for a in sales:
            t = _fmt_time(a.get("sent_at"))
            price_str = f"${a['price']:.2f} CAD" if a.get("price") else "N/A"
            lines.append(f"💰 <b>{a['site_name']}</b> — {price_str}")
            lines.append(f"    {a['title'][:90]}")
            lines.append(f"    <i>{t}</i>")
            if a.get("url"):
                lines.append(f"    {a['url']}")
            lines.append("")

        await self._reply(chat_id, "\n".join(lines).strip())

    # ------------------------------------------------------------------ #
    # /prices                                                              #
    # ------------------------------------------------------------------ #

    async def _cmd_prices(self, chat_id: str, args: list):
        if not args:
            await self._reply(
                chat_id,
                "Usage: /prices <i>[product name]</i>\n"
                "Example: <code>/prices prismatic etb</code>\n"
                "Example: <code>/prices one piece op07 booster box</code>",
            )
            return

        query = " ".join(args).lower()
        products = self.db.search_products(query, limit=15)

        if not products:
            await self._reply(chat_id, f"🔍 No results found for: <b>{query}</b>\n\nTry a shorter search term.")
            return

        in_stock = [p for p in products if p.get("in_stock")]
        out_stock = [p for p in products if not p.get("in_stock")]

        lines = [f"🔍 <b>Prices for: {query}</b>"]
        lines.append(f"<i>{len(in_stock)} in stock, {len(out_stock)} out of stock</i>\n")

        if in_stock:
            lines.append("✅ <b>In Stock</b>")
            for p in in_stock:
                price_str = f"${p['price']:.2f} CAD" if p.get("price") else "N/A"
                lines.append(f"  • <b>{p['site_name']}</b> — {price_str}")
                lines.append(f"    {p['title'][:70]}")
                if p.get("url"):
                    lines.append(f"    {p['url']}")
            lines.append("")

        if out_stock and len(in_stock) < 8:
            lines.append("❌ <b>Out of Stock</b>")
            for p in out_stock[:5]:
                price_str = f"${p['price']:.2f} CAD" if p.get("price") else "N/A"
                lines.append(f"  • <b>{p['site_name']}</b> — {price_str}")
                lines.append(f"    {p['title'][:70]}")

        await self._reply(chat_id, "\n".join(lines).strip())

    # ------------------------------------------------------------------ #
    # /stores                                                              #
    # ------------------------------------------------------------------ #

    async def _cmd_stores(self, chat_id: str, args: list):
        sites = self.config.get("sites", {})
        enabled = [(k, v) for k, v in sites.items() if v.get("enabled", True)]

        lines = [f"🏪 <b>Monitored Stores ({len(enabled)} active)</b>\n"]
        for _, site in sorted(enabled, key=lambda x: x[1]["name"]):
            urls = site.get("urls", [])
            domain = ""
            if urls:
                u = urls[0]
                parts = u.split("/")
                domain = parts[2] if len(parts) > 2 else u
            lines.append(f"• <b>{site['name']}</b>")
            if domain:
                lines.append(f"  {domain}")

        drop_sources = self.config.get("drop_sources", {})
        if drop_sources:
            lines.append("\n📡 <b>Stock Trackers</b>")
            for _, ds in drop_sources.items():
                if ds.get("enabled", True):
                    lines.append(f"• {ds['name']}")

        lines.append(f"\n🕐 {_now_est()}")
        await self._reply(chat_id, "\n".join(lines))

    # ------------------------------------------------------------------ #
    # /status                                                              #
    # ------------------------------------------------------------------ #

    async def _cmd_status(self, chat_id: str, args: list):
        statuses = self.db.get_all_site_status()
        if not statuses:
            await self._reply(chat_id, "📊 No status data yet — bot may have just started.")
            return

        ok = [s for s in statuses if s["status"] == "ok"]
        errors = [s for s in statuses if s["status"] != "ok"]

        lines = [f"📊 <b>Store Status</b> — {len(ok)}/{len(statuses)} healthy\n"]

        if errors:
            lines.append("⚠️ <b>Issues</b>")
            for s in errors:
                last = _fmt_time(s.get("last_check"))
                lines.append(f"  ❌ <b>{s['site_name']}</b> — {s['consecutive_errors']} errors")
                lines.append(f"     Last checked: {last}")
            lines.append("")

        lines.append("✅ <b>Healthy</b>")
        for s in ok:
            last = _fmt_time(s.get("last_check"))
            lines.append(f"  • <b>{s['site_name']}</b> — {last}")

        lines.append(f"\n🕐 {_now_est()}")
        await self._reply(chat_id, "\n".join(lines))
