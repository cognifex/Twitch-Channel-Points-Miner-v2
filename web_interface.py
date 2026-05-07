from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Flask, redirect, render_template_string, request, url_for

app = Flask(__name__)

CONFIG_PATH = Path(os.getenv("WEBUI_CONFIG_PATH", "/data/config.json"))
LOG_PATH = Path(os.getenv("WEBUI_LOG_PATH", "/data/logs/latest.log"))

DEFAULT_CONFIG: dict[str, Any] = {
    "username": "",
    "password": "",
    "streamers": [],
    "make_predictions": True,
    "follow_raid": True,
    "claim_drops": True,
    "watch_streak": True,
    "chat_presence": "ONLINE",
    "bet": {
        "strategy": "SMART",
        "percentage": 5,
        "max_points": 50000,
        "minimum_points": 0,
    },
}


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG

    with CONFIG_PATH.open("r", encoding="utf-8") as fp:
        return json.load(fp)


def save_config(config: dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CONFIG_PATH.open("w", encoding="utf-8") as fp:
        json.dump(config, fp, indent=2)


def read_log_tail(lines: int = 200) -> list[str]:
    if not LOG_PATH.exists():
        return ["Noch keine Logs gefunden."]

    with LOG_PATH.open("r", encoding="utf-8", errors="ignore") as fp:
        content = fp.readlines()

    return [line.rstrip("\n") for line in content[-lines:]]


TEMPLATE = """
<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Twitch Miner Control Panel</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 0; background: #10131a; color: #f5f7ff; }
    .container { max-width: 1024px; margin: 0 auto; padding: 20px; }
    .card { background: #1a1f2b; border-radius: 10px; padding: 16px; margin-bottom: 16px; }
    input, select, textarea { width: 100%; padding: 8px; margin: 6px 0 12px; border-radius: 6px; border: 1px solid #37415a; background: #0f1522; color: #fff; }
    button { background: #6f42c1; color: #fff; border: 0; padding: 10px 16px; border-radius: 6px; cursor: pointer; }
    .row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
    pre { background: #0c1019; padding: 12px; border-radius: 8px; max-height: 450px; overflow: auto; }
    .meta { color: #97a0bb; font-size: 0.9rem; }
  </style>
</head>
<body>
<div class="container">
  <h1>Twitch Channel Points Miner – Webinterface</h1>
  <p class="meta">Config-Datei: {{ config_path }} | Log-Datei: {{ log_path }}</p>

  <div class="card">
    <h2>Konfiguration</h2>
    <form method="post" action="{{ url_for('save') }}">
      <label>Username</label>
      <input name="username" value="{{ config.get('username', '') }}">
      <label>Password</label>
      <input name="password" type="password" value="{{ config.get('password', '') }}">

      <label>Streamer (kommagetrennt)</label>
      <input name="streamers" value="{{ ','.join(config.get('streamers', [])) }}">

      <div class="row">
        <div>
          <label>Chat Presence</label>
          <select name="chat_presence">
            {% for option in ['ALWAYS', 'NEVER', 'ONLINE', 'OFFLINE'] %}
              <option value="{{ option }}" {% if config.get('chat_presence') == option %}selected{% endif %}>{{ option }}</option>
            {% endfor %}
          </select>
        </div>
        <div>
          <label>Bet Strategy</label>
          <select name="bet_strategy">
            {% for option in ['SMART', 'PERCENTAGE', 'MOST_VOTED'] %}
              <option value="{{ option }}" {% if config.get('bet', {}).get('strategy') == option %}selected{% endif %}>{{ option }}</option>
            {% endfor %}
          </select>
        </div>
      </div>

      <div class="row">
        <div><label>Bet Percentage</label><input name="bet_percentage" type="number" min="1" max="100" value="{{ config.get('bet', {}).get('percentage', 5) }}"></div>
        <div><label>Max Points</label><input name="bet_max_points" type="number" min="1" value="{{ config.get('bet', {}).get('max_points', 50000) }}"></div>
      </div>

      <div class="row">
        <div><label>Minimum Points</label><input name="bet_minimum_points" type="number" min="0" value="{{ config.get('bet', {}).get('minimum_points', 0) }}"></div>
        <div></div>
      </div>

      <button type="submit">Speichern</button>
    </form>
    <p class="meta">Zuletzt gespeichert: {{ saved_at }}</p>
  </div>

  <div class="card">
    <h2>Monitoring (Live-Log Tail)</h2>
    <p><a href="{{ url_for('index') }}" style="color:#b9c4ff;">Aktualisieren</a></p>
    <pre>{% for line in logs %}{{ line }}
{% endfor %}</pre>
  </div>
</div>
</body>
</html>
"""


@app.get("/")
def index() -> str:
    config = load_config()
    logs = read_log_tail()
    saved_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    return render_template_string(
        TEMPLATE,
        config=config,
        logs=logs,
        saved_at=saved_at,
        config_path=CONFIG_PATH,
        log_path=LOG_PATH,
    )


@app.post("/save")
def save() -> Any:
    streamers = [s.strip() for s in request.form.get("streamers", "").split(",") if s.strip()]
    config = {
        "username": request.form.get("username", ""),
        "password": request.form.get("password", ""),
        "streamers": streamers,
        "chat_presence": request.form.get("chat_presence", "ONLINE"),
        "bet": {
            "strategy": request.form.get("bet_strategy", "SMART"),
            "percentage": int(request.form.get("bet_percentage", 5)),
            "max_points": int(request.form.get("bet_max_points", 50000)),
            "minimum_points": int(request.form.get("bet_minimum_points", 0)),
        },
    }
    save_config(config)
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("WEBUI_PORT", "8080")), debug=False)
