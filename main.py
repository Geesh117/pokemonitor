#!/usr/bin/env python3
"""
PokéMonitor — Pokemon TCG & One Piece TCG monitoring bot.

Usage:
  python main.py           # Production mode
  python main.py --test    # Test mode (one cycle, no Telegram alerts)
  python main.py --digest  # Send daily digest immediately and exit
"""

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent))


def load_config(path: str = "config.json") -> dict:
    config_path = Path(path)
    if not config_path.exists():
        print(f"ERROR: config.json not found at {config_path.absolute()}")
        sys.exit(1)
    with open(config_path, encoding="utf-8") as f:
        config = json.load(f)

    # Allow env var overrides for cloud deployments (Railway, etc.)
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        config["telegram"]["bot_token"] = os.environ["TELEGRAM_BOT_TOKEN"]
    if os.environ.get("TELEGRAM_CHAT_ID"):
        config["telegram"]["chat_id"] = os.environ["TELEGRAM_CHAT_ID"]
    if os.environ.get("PORT"):
        config["dashboard"]["port"] = int(os.environ["PORT"])
        config["dashboard"]["host"] = "0.0.0.0"

    return config


async def _run_production(config: dict):
    from bot.monitor import Monitor
    from bot.logger_setup import get_logger

    log = get_logger("main")

    # Start dashboard
    dashboard_cfg = config.get("dashboard", {})
    if dashboard_cfg.get("enabled", True):
        from bot.database import Database
        from dashboard.app import Dashboard

        db = Database(config["database"]["path"])
        dash = Dashboard(
            db=db,
            host=dashboard_cfg.get("host", "127.0.0.1"),
            port=dashboard_cfg.get("port", 5000),
        )
        dash.start()
        print(f"Dashboard: http://{dashboard_cfg.get('host','127.0.0.1')}:{dashboard_cfg.get('port',5000)}")

    monitor = Monitor(config, test_mode=False)

    # Install Playwright browsers if needed
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log.warning("Playwright not installed. Run setup.bat to install.")

    # Graceful shutdown
    loop = asyncio.get_running_loop()

    def _shutdown(sig, frame):
        log.info("Received signal %s, shutting down…", sig)
        monitor.stop()

    try:
        signal.signal(signal.SIGINT, _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)
    except (OSError, ValueError):
        pass

    try:
        await monitor.run()
    except asyncio.CancelledError:
        pass
    finally:
        await monitor.tg.close()
        log.info("PokéMonitor stopped.")


async def _run_test(config: dict):
    from bot.monitor import Monitor
    monitor = Monitor(config, test_mode=True)
    await monitor.run_once()
    await monitor.tg.close()


async def _send_digest(config: dict):
    from bot.monitor import Monitor
    from bot.database import Database

    db = Database(config["database"]["path"])
    from bot.telegram_service import TelegramService

    tg = TelegramService(
        token=config["telegram"]["bot_token"],
        chat_id=config["telegram"]["chat_id"],
    )
    alerts = db.get_alerts_since(hours=24)
    news = db.get_recent_news(limit=25)
    await tg.send_daily_digest(alerts, news)
    await tg.close()
    print("Digest sent.")


def main():
    # Route all loggers to stdout so Railway captures output
    logging.basicConfig(
        stream=sys.stdout,
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="PokéMonitor — TCG Bot")
    parser.add_argument("--test", action="store_true", help="Run one check cycle without sending Telegram alerts")
    parser.add_argument("--digest", action="store_true", help="Send daily digest immediately and exit")
    parser.add_argument("--config", default="config.json", help="Path to config file")
    args = parser.parse_args()

    config = load_config(args.config)

    # Setup logging early
    from bot.logger_setup import setup_logger
    setup_logger("pokemonitor", log_dir=config["logs"]["directory"], level=config["logs"]["level"])

    if args.test:
        print("Running in TEST mode — no Telegram alerts will be sent.\n")
        asyncio.run(_run_test(config))
    elif args.digest:
        asyncio.run(_send_digest(config))
    else:
        print("PokéMonitor starting in PRODUCTION mode.")
        print(f"Dashboard will be available at: http://{config['dashboard']['host']}:{config['dashboard']['port']}")
        print("Press Ctrl+C to stop.\n")
        asyncio.run(_run_production(config))


if __name__ == "__main__":
    main()
