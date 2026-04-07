"""
Flask-based local web dashboard at http://localhost:5000
Shows bot status, site last-check times, recent alerts, recent news, and errors.
Runs in a separate daemon thread alongside the main monitor.
"""

import json
import os
import threading
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, render_template_string

from bot.database import Database
from bot.logger_setup import get_logger

log = get_logger(__name__)

TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta http-equiv="refresh" content="30">
  <title>PokéMonitor Dashboard</title>
  <style>
    :root {
      --bg: #0f0f0f; --card: #1a1a2e; --accent: #e94560;
      --green: #4ade80; --yellow: #fbbf24; --red: #f87171;
      --text: #e2e8f0; --sub: #94a3b8;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { background: var(--bg); color: var(--text); font-family: 'Segoe UI', system-ui, sans-serif; padding: 1rem; }
    h1 { color: var(--accent); font-size: 1.8rem; margin-bottom: 0.25rem; }
    .subtitle { color: var(--sub); font-size: 0.85rem; margin-bottom: 1.5rem; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 1rem; margin-bottom: 1.5rem; }
    .card { background: var(--card); border-radius: 10px; padding: 1rem; border: 1px solid #2d2d44; }
    .card h2 { font-size: 1rem; color: var(--sub); margin-bottom: 0.75rem; text-transform: uppercase; letter-spacing: 0.05em; }
    .stat { font-size: 2.2rem; font-weight: 700; color: var(--accent); }
    .status-ok { color: var(--green); }
    .status-error { color: var(--red); }
    .status-unknown { color: var(--yellow); }
    table { width: 100%; border-collapse: collapse; font-size: 0.82rem; }
    th { text-align: left; color: var(--sub); padding: 0.4rem 0.6rem; border-bottom: 1px solid #2d2d44; font-weight: 600; }
    td { padding: 0.4rem 0.6rem; border-bottom: 1px solid #1e1e30; vertical-align: top; }
    tr:hover td { background: #22223a; }
    a { color: #60a5fa; text-decoration: none; }
    a:hover { text-decoration: underline; }
    .badge { display: inline-block; padding: 0.1rem 0.5rem; border-radius: 9999px; font-size: 0.72rem; font-weight: 600; }
    .badge-green  { background: #14532d; color: var(--green); }
    .badge-red    { background: #450a0a; color: var(--red); }
    .badge-yellow { background: #451a03; color: var(--yellow); }
    .badge-blue   { background: #0c2a4a; color: #60a5fa; }
    .section { background: var(--card); border-radius: 10px; padding: 1rem; margin-bottom: 1rem; border: 1px solid #2d2d44; }
    .section h2 { font-size: 1rem; color: var(--sub); margin-bottom: 0.75rem; text-transform: uppercase; letter-spacing: 0.05em; }
    .refresh-note { color: var(--sub); font-size: 0.75rem; margin-top: 1rem; text-align: right; }
    .truncate { max-width: 300px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  </style>
</head>
<body>
  <h1>🔴 PokéMonitor</h1>
  <p class="subtitle">Live monitoring dashboard — auto-refreshes every 30s | {{ now }}</p>

  <div class="grid">
    <div class="card">
      <h2>Bot Status</h2>
      <div class="stat status-ok">● RUNNING</div>
      <p style="color:var(--sub);font-size:0.8rem;margin-top:0.5rem">Uptime since startup</p>
    </div>
    <div class="card">
      <h2>Alerts Today</h2>
      <div class="stat">{{ stats.alerts_today }}</div>
    </div>
    <div class="card">
      <h2>Sites Monitored</h2>
      <div class="stat">{{ stats.total_sites }}</div>
    </div>
    <div class="card">
      <h2>Products Tracked</h2>
      <div class="stat">{{ stats.total_products }}</div>
    </div>
  </div>

  <!-- Site Status Table -->
  <div class="section">
    <h2>Site Status</h2>
    <table>
      <thead>
        <tr>
          <th>Site</th><th>Status</th><th>Last Check (UTC)</th><th>Errors</th>
        </tr>
      </thead>
      <tbody>
        {% for s in site_statuses %}
        <tr>
          <td>{{ s.site_name }}</td>
          <td>
            {% if s.status == 'ok' %}
              <span class="badge badge-green">OK</span>
            {% elif s.status == 'error' %}
              <span class="badge badge-red">ERROR</span>
            {% else %}
              <span class="badge badge-yellow">UNKNOWN</span>
            {% endif %}
          </td>
          <td>{{ s.last_check or 'Never' }}</td>
          <td>{{ s.consecutive_errors }}</td>
        </tr>
        {% else %}
        <tr><td colspan="4" style="color:var(--sub)">No sites checked yet</td></tr>
        {% endfor %}
      </tbody>
    </table>
  </div>

  <!-- Recent Alerts -->
  <div class="section">
    <h2>Recent Product Alerts (last 24h)</h2>
    <table>
      <thead>
        <tr><th>Type</th><th>Store</th><th>Product</th><th>Price</th><th>Time (UTC)</th></tr>
      </thead>
      <tbody>
        {% for a in recent_alerts %}
        <tr>
          <td>
            {% if a.alert_type == 'restock' %}<span class="badge badge-green">RESTOCK</span>
            {% elif a.alert_type == 'new_product' %}<span class="badge badge-blue">NEW</span>
            {% elif a.alert_type == 'price_drop' %}<span class="badge badge-yellow">PRICE DROP</span>
            {% elif a.alert_type == 'out_of_stock' %}<span class="badge badge-red">OOS</span>
            {% elif a.alert_type == 'news' %}<span class="badge badge-blue">NEWS</span>
            {% else %}<span class="badge badge-yellow">{{ a.alert_type }}</span>
            {% endif %}
          </td>
          <td>{{ a.site_name }}</td>
          <td class="truncate">
            {% if a.url %}<a href="{{ a.url }}" target="_blank">{{ a.title }}</a>
            {% else %}{{ a.title }}{% endif %}
          </td>
          <td>{% if a.price %}${{ "%.2f"|format(a.price) }}{% else %}N/A{% endif %}</td>
          <td style="white-space:nowrap">{{ a.sent_at[:19] if a.sent_at else '' }}</td>
        </tr>
        {% else %}
        <tr><td colspan="5" style="color:var(--sub)">No alerts in the last 24 hours</td></tr>
        {% endfor %}
      </tbody>
    </table>
  </div>

  <!-- Recent News -->
  <div class="section">
    <h2>Recent News</h2>
    <table>
      <thead>
        <tr><th>Source</th><th>Title</th><th>Seen (UTC)</th></tr>
      </thead>
      <tbody>
        {% for n in recent_news %}
        <tr>
          <td>{{ n.source_name }}</td>
          <td class="truncate"><a href="{{ n.article_url }}" target="_blank">{{ n.title }}</a></td>
          <td style="white-space:nowrap">{{ n.first_seen[:19] if n.first_seen else '' }}</td>
        </tr>
        {% else %}
        <tr><td colspan="3" style="color:var(--sub)">No news yet</td></tr>
        {% endfor %}
      </tbody>
    </table>
  </div>

  <p class="refresh-note">Page auto-refreshes every 30 seconds</p>
</body>
</html>
"""


class Dashboard:
    def __init__(self, db: Database, host: str = "127.0.0.1", port: int = 5000):
        self.db = db
        self.host = host
        self.port = port
        self.app = Flask(__name__)
        self._thread: threading.Thread = None
        self._register_routes()

    def _register_routes(self):
        db = self.db

        @self.app.route("/")
        def index():
            site_statuses = db.get_all_site_status()
            recent_alerts = db.get_alerts_since(hours=24)
            recent_news = db.get_recent_news(limit=30)
            all_products = db.get_all_products()

            stats = {
                "alerts_today": db.count_alerts_today(),
                "total_sites": len(site_statuses),
                "total_products": len(all_products),
            }

            now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
            return render_template_string(
                TEMPLATE,
                site_statuses=site_statuses,
                recent_alerts=recent_alerts,
                recent_news=recent_news,
                stats=stats,
                now=now,
            )

        @self.app.route("/api/status")
        def api_status():
            return jsonify({
                "status": "running",
                "sites": db.get_all_site_status(),
                "alerts_today": db.count_alerts_today(),
            })

        @self.app.route("/api/products")
        def api_products():
            return jsonify(db.get_all_products())

        @self.app.route("/api/alerts")
        def api_alerts():
            return jsonify(db.get_alerts_since(hours=24))

        @self.app.route("/api/news")
        def api_news():
            return jsonify(db.get_recent_news(limit=50))

    def start(self):
        import logging
        logging.getLogger("werkzeug").setLevel(logging.ERROR)

        self._thread = threading.Thread(
            target=lambda: self.app.run(
                host=self.host, port=self.port, debug=False, use_reloader=False
            ),
            daemon=True,
            name="dashboard",
        )
        self._thread.start()
        log.info("Dashboard running at http://%s:%s", self.host, self.port)
