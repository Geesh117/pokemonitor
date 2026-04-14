"""SQLite database service for tracking products, alerts, and news."""

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from bot.logger_setup import get_logger

log = get_logger(__name__)


class Database:
    def __init__(self, db_path: str = "data/pokemonitor.db"):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._local = threading.local()
        self._init_db()

    @contextmanager
    def _conn(self):
        if not hasattr(self._local, "connection") or self._local.connection is None:
            self._local.connection = sqlite3.connect(self.db_path, check_same_thread=False)
            self._local.connection.row_factory = sqlite3.Row
        conn = self._local.connection
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def _init_db(self):
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS products (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    site_key        TEXT    NOT NULL,
                    site_name       TEXT    NOT NULL,
                    product_id      TEXT    NOT NULL,
                    title           TEXT    NOT NULL,
                    url             TEXT    NOT NULL,
                    price           REAL,
                    in_stock        INTEGER NOT NULL DEFAULT 0,
                    first_seen      TEXT    NOT NULL,
                    last_seen       TEXT    NOT NULL,
                    last_alerted    TEXT,
                    alert_type      TEXT,
                    UNIQUE(site_key, product_id)
                );

                CREATE TABLE IF NOT EXISTS alerts (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    site_key        TEXT    NOT NULL,
                    site_name       TEXT    NOT NULL,
                    product_id      TEXT,
                    title           TEXT    NOT NULL,
                    url             TEXT,
                    price           REAL,
                    alert_type      TEXT    NOT NULL,
                    message         TEXT,
                    sent_at         TEXT    NOT NULL
                );

                CREATE TABLE IF NOT EXISTS news (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_key      TEXT    NOT NULL,
                    source_name     TEXT    NOT NULL,
                    article_url     TEXT    NOT NULL UNIQUE,
                    title           TEXT    NOT NULL,
                    published       TEXT,
                    alerted         INTEGER NOT NULL DEFAULT 0,
                    first_seen      TEXT    NOT NULL
                );

                CREATE TABLE IF NOT EXISTS site_status (
                    site_key        TEXT    PRIMARY KEY,
                    site_name       TEXT    NOT NULL,
                    last_check      TEXT,
                    last_success    TEXT,
                    consecutive_errors INTEGER NOT NULL DEFAULT 0,
                    status          TEXT    NOT NULL DEFAULT 'unknown'
                );

                CREATE TABLE IF NOT EXISTS price_history (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    site_key    TEXT    NOT NULL,
                    product_id  TEXT    NOT NULL,
                    title       TEXT,
                    site_name   TEXT,
                    price       REAL,
                    in_stock    INTEGER NOT NULL DEFAULT 0,
                    recorded_at TEXT    NOT NULL
                );

                CREATE TABLE IF NOT EXISTS watches (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id    TEXT NOT NULL,
                    query      TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(chat_id, query)
                );

                CREATE INDEX IF NOT EXISTS idx_products_site_key  ON products(site_key);
                CREATE INDEX IF NOT EXISTS idx_alerts_sent_at     ON alerts(sent_at);
                CREATE INDEX IF NOT EXISTS idx_news_first_seen    ON news(first_seen);
                CREATE INDEX IF NOT EXISTS idx_price_hist_product ON price_history(site_key, product_id);
                CREATE INDEX IF NOT EXISTS idx_watches_chat_id    ON watches(chat_id);
            """)
        log.info("Database initialised at %s", self.db_path)

    # ------------------------------------------------------------------ #
    # Products                                                             #
    # ------------------------------------------------------------------ #

    def upsert_product(
        self,
        site_key: str,
        site_name: str,
        product_id: str,
        title: str,
        url: str,
        price: Optional[float],
        in_stock: bool,
    ) -> dict:
        """Insert or update a product row; return a dict describing what changed."""
        now = datetime.utcnow().isoformat()
        change = {"is_new": False, "stock_changed": False, "price_changed": False,
                  "old_price": None, "old_in_stock": None}

        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM products WHERE site_key=? AND product_id=?",
                (site_key, product_id),
            ).fetchone()

            if row is None:
                conn.execute(
                    """INSERT INTO products
                       (site_key, site_name, product_id, title, url, price, in_stock, first_seen, last_seen)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (site_key, site_name, product_id, title, url, price,
                     int(in_stock), now, now),
                )
                change["is_new"] = True
            else:
                change["old_price"] = row["price"]
                change["old_in_stock"] = bool(row["in_stock"])
                change["stock_changed"] = bool(row["in_stock"]) != in_stock
                change["price_changed"] = (
                    row["price"] is not None
                    and price is not None
                    and abs(row["price"] - price) > 0.01
                )
                conn.execute(
                    """UPDATE products
                       SET title=?, url=?, price=?, in_stock=?, last_seen=?
                       WHERE site_key=? AND product_id=?""",
                    (title, url, price, int(in_stock), now, site_key, product_id),
                )
        return change

    def was_recently_alerted(self, site_key: str, product_id: str, hours: float = 2) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT last_alerted FROM products WHERE site_key=? AND product_id=?",
                (site_key, product_id),
            ).fetchone()
            if row is None or row["last_alerted"] is None:
                return False
            cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
            return row["last_alerted"] > cutoff

    def mark_alerted(self, site_key: str, product_id: str, alert_type: str):
        now = datetime.utcnow().isoformat()
        with self._conn() as conn:
            conn.execute(
                "UPDATE products SET last_alerted=?, alert_type=? WHERE site_key=? AND product_id=?",
                (now, alert_type, site_key, product_id),
            )

    def record_price_history(
        self,
        site_key: str,
        product_id: str,
        title: str,
        site_name: str,
        price: Optional[float],
        in_stock: bool,
    ):
        now = datetime.utcnow().isoformat()
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO price_history
                   (site_key, product_id, title, site_name, price, in_stock, recorded_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (site_key, product_id, title, site_name, price, int(in_stock), now),
            )

    def get_price_history(self, site_key: str, product_id: str, days: int = 30) -> list:
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT price, in_stock, recorded_at FROM price_history
                   WHERE site_key=? AND product_id=? AND recorded_at > ?
                   ORDER BY recorded_at ASC""",
                (site_key, product_id, cutoff),
            ).fetchall()
        return [dict(r) for r in rows]

    def search_price_history(self, query: str, days: int = 30, limit: int = 5) -> list:
        """Find products matching query and return their price history."""
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        terms = query.lower().split()
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT DISTINCT site_key, product_id, title, site_name
                   FROM price_history WHERE recorded_at > ?""",
                (cutoff,),
            ).fetchall()
        matches = []
        seen = set()
        for row in rows:
            key = (row["site_key"], row["product_id"])
            if key in seen:
                continue
            title_lower = (row["title"] or "").lower()
            if all(t in title_lower for t in terms):
                seen.add(key)
                matches.append(dict(row))
            if len(matches) >= limit:
                break
        return matches

    def search_products(self, query: str, limit: int = 15) -> list:
        """Full-text search across product titles. Returns in-stock results first."""
        terms = query.lower().split()
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM products ORDER BY in_stock DESC, last_seen DESC LIMIT 500"
            ).fetchall()
        results = []
        for row in rows:
            title_lower = (row["title"] or "").lower()
            if all(t in title_lower for t in terms):
                results.append(dict(row))
            if len(results) >= limit:
                break
        return results

    def get_all_products(self, site_key: Optional[str] = None) -> list:
        with self._conn() as conn:
            if site_key:
                rows = conn.execute(
                    "SELECT * FROM products WHERE site_key=? ORDER BY last_seen DESC",
                    (site_key,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM products ORDER BY last_seen DESC LIMIT 200"
                ).fetchall()
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # Alerts log                                                           #
    # ------------------------------------------------------------------ #

    def log_alert(
        self,
        site_key: str,
        site_name: str,
        title: str,
        alert_type: str,
        product_id: Optional[str] = None,
        url: Optional[str] = None,
        price: Optional[float] = None,
        message: Optional[str] = None,
    ):
        now = datetime.utcnow().isoformat()
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO alerts (site_key,site_name,product_id,title,url,price,alert_type,message,sent_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (site_key, site_name, product_id, title, url, price, alert_type, message, now),
            )

    def get_alerts_since(self, hours: float = 24) -> list:
        cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM alerts WHERE sent_at > ? ORDER BY sent_at DESC",
                (cutoff,),
            ).fetchall()
            return [dict(r) for r in rows]

    def count_alerts_today(self) -> int:
        cutoff = (datetime.utcnow() - timedelta(hours=24)).isoformat()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM alerts WHERE sent_at > ?", (cutoff,)
            ).fetchone()
            return row["cnt"]

    # ------------------------------------------------------------------ #
    # News                                                                 #
    # ------------------------------------------------------------------ #

    def news_seen(self, article_url: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id FROM news WHERE article_url=?", (article_url,)
            ).fetchone()
            return row is not None

    def add_news(
        self,
        source_key: str,
        source_name: str,
        article_url: str,
        title: str,
        published: Optional[str] = None,
    ):
        now = datetime.utcnow().isoformat()
        with self._conn() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO news
                   (source_key,source_name,article_url,title,published,alerted,first_seen)
                   VALUES (?,?,?,?,?,0,?)""",
                (source_key, source_name, article_url, title, published, now),
            )

    def mark_news_alerted(self, article_url: str):
        with self._conn() as conn:
            conn.execute("UPDATE news SET alerted=1 WHERE article_url=?", (article_url,))

    def get_recent_news(self, limit: int = 50) -> list:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM news ORDER BY first_seen DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # Site status                                                          #
    # ------------------------------------------------------------------ #

    def update_site_status(self, site_key: str, site_name: str, success: bool):
        now = datetime.utcnow().isoformat()
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT consecutive_errors FROM site_status WHERE site_key=?", (site_key,)
            ).fetchone()

            if existing is None:
                errors = 0 if success else 1
                conn.execute(
                    """INSERT INTO site_status (site_key,site_name,last_check,last_success,consecutive_errors,status)
                       VALUES (?,?,?,?,?,?)""",
                    (site_key, site_name, now,
                     now if success else None, errors,
                     "ok" if success else "error"),
                )
            else:
                errors = 0 if success else (existing["consecutive_errors"] + 1)
                conn.execute(
                    """UPDATE site_status
                       SET site_name=?, last_check=?, last_success=?, consecutive_errors=?, status=?
                       WHERE site_key=?""",
                    (site_name, now,
                     now if success else None,
                     errors,
                     "ok" if success else "error",
                     site_key),
                )

    def get_all_site_status(self) -> list:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM site_status ORDER BY site_name").fetchall()
            return [dict(r) for r in rows]

    def get_site_last_check(self, site_key: str) -> Optional[str]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT last_check FROM site_status WHERE site_key=?", (site_key,)
            ).fetchone()
            return row["last_check"] if row else None

    # ------------------------------------------------------------------ #
    # Watches                                                              #
    # ------------------------------------------------------------------ #

    def add_watch(self, chat_id: str, query: str):
        now = datetime.utcnow().isoformat()
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO watches (chat_id, query, created_at) VALUES (?,?,?)",
                (chat_id, query.lower().strip(), now),
            )

    def remove_watch(self, chat_id: str, query: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM watches WHERE chat_id=? AND query=?",
                (chat_id, query.lower().strip()),
            )
            return cur.rowcount > 0

    def get_watches(self, chat_id: str) -> list:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT query, created_at FROM watches WHERE chat_id=? ORDER BY created_at DESC",
                (chat_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_all_watches(self) -> list:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT chat_id, query FROM watches"
            ).fetchall()
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # Restock patterns                                                     #
    # ------------------------------------------------------------------ #

    def get_restock_patterns(self, site_key: str, product_id: str, days: int = 90) -> dict:
        """Analyse price_history to find common restock days/hours."""
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT recorded_at FROM price_history
                   WHERE site_key=? AND product_id=? AND in_stock=1 AND recorded_at > ?
                   ORDER BY recorded_at ASC""",
                (site_key, product_id, cutoff),
            ).fetchall()

        times = [r["recorded_at"] for r in rows]
        if not times:
            return {"restock_times": [], "common_day": None, "common_hour": None, "total_restocks": 0}

        from collections import Counter
        day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        day_counts: Counter = Counter()
        hour_counts: Counter = Counter()

        # Deduplicate — only count transitions (avoid counting every polling cycle)
        deduped = [times[0]]
        for t in times[1:]:
            prev = datetime.fromisoformat(deduped[-1])
            curr = datetime.fromisoformat(t)
            if (curr - prev).total_seconds() > 3600:  # >1h gap = new restock event
                deduped.append(t)

        for t in deduped:
            dt = datetime.fromisoformat(t)
            day_counts[dt.weekday()] += 1
            hour_counts[dt.hour] += 1

        common_day = day_names[day_counts.most_common(1)[0][0]] if day_counts else None
        common_hour = hour_counts.most_common(1)[0][0] if hour_counts else None

        return {
            "restock_times": deduped,
            "common_day": common_day,
            "common_hour": common_hour,
            "total_restocks": len(deduped),
        }
