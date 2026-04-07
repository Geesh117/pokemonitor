#!/usr/bin/env python3
"""
Watchdog process — monitors main.py and auto-restarts it if it crashes.
Also sends a Telegram alert when the bot goes down or comes back up.

Run this script instead of main.py for production deployments.
Usage: python watchdog_runner.py
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).parent
MAIN_SCRIPT = SCRIPT_DIR / "main.py"
CONFIG_FILE = SCRIPT_DIR / "config.json"
LOG_FILE = SCRIPT_DIR / "logs" / "watchdog.log"

RESTART_DELAY = 10        # seconds before restarting after crash
MAX_RESTART_ATTEMPTS = 10 # resets after 1 hour of uptime
UPTIME_RESET_SECONDS = 3600


def load_config():
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


def send_telegram(token: str, chat_id: str, message: str):
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        requests.post(
            url,
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as exc:
        _log(f"Failed to send Telegram message: {exc}")


def _log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] WATCHDOG | {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def main():
    _log("Watchdog starting")
    config = load_config()
    token = config["telegram"]["bot_token"]
    chat_id = config["telegram"]["chat_id"]

    restart_count = 0
    last_long_uptime = time.time()
    process = None

    while True:
        _log(f"Starting main.py (attempt #{restart_count + 1})")
        start_time = time.time()

        try:
            process = subprocess.Popen(
                [sys.executable, str(MAIN_SCRIPT)],
                cwd=str(SCRIPT_DIR),
            )

            if restart_count > 0:
                send_telegram(
                    token, chat_id,
                    f"🟢 <b>PokéMonitor restarted</b> (attempt #{restart_count})\n"
                    f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                )

            process.wait()
            exit_code = process.returncode
            uptime = time.time() - start_time

        except KeyboardInterrupt:
            _log("Watchdog interrupted by user")
            if process:
                process.terminate()
            break
        except Exception as exc:
            _log(f"Failed to start process: {exc}")
            exit_code = -1
            uptime = 0

        _log(f"main.py exited with code {exit_code} after {uptime:.0f}s")

        # Reset restart counter if the process ran for a long time
        if uptime >= UPTIME_RESET_SECONDS:
            restart_count = 0
            last_long_uptime = time.time()

        restart_count += 1

        if restart_count > MAX_RESTART_ATTEMPTS:
            msg = (
                f"⚠️ <b>PokéMonitor FAILED {restart_count} times</b> — watchdog giving up.\n"
                f"Manual intervention required.\n"
                f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            _log(msg)
            send_telegram(token, chat_id, msg)
            break

        # Alert on crash
        if exit_code != 0:
            send_telegram(
                token, chat_id,
                f"⚠️ <b>PokéMonitor crashed</b> (exit code {exit_code})\n"
                f"Restarting in {RESTART_DELAY}s… (attempt #{restart_count}/{MAX_RESTART_ATTEMPTS})\n"
                f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            )

        _log(f"Restarting in {RESTART_DELAY}s…")
        time.sleep(RESTART_DELAY)


if __name__ == "__main__":
    main()
