from __future__ import annotations

import json
import os
from datetime import datetime
import re
from pathlib import Path
from typing import Any

from flask import Flask, redirect, render_template_string, request, url_for

import requests

from TwitchChannelPointsMiner.classes.TwitchLogin import TwitchLogin
from TwitchChannelPointsMiner.constants import CLIENT_ID, USER_AGENTS

app = Flask(__name__)

CONFIG_PATH = Path(os.getenv("WEBUI_CONFIG_PATH", "/data/config.json"))
LOG_PATH = Path(os.getenv("WEBUI_LOG_PATH", "/data/logs/latest.log"))
COOKIES_PATH = Path(os.getenv("WEBUI_COOKIES_PATH", "/data/cookies"))


DEFAULT_CONFIG: dict[str, Any] = {
    "username": "",
    "password": "",
    "auth_token": "",
    "persistent": "",
    "cookie_file": "",
    "streamers": [],
    "make_predictions": True,
    "follow_raid": True,
    "claim_drops": True,
    "watch_streak": True,
    "chat_presence": "ONLINE",
    "proxy": "",
    "autostart_mode": "enabled",
    "max_login_tries": 3,
    "bet": {
        "strategy": "SMART",
        "percentage": 5,
        "max_points": 50000,
        "minimum_points": 0,
    },
}



LOGIN_TEST_TIMEOUT_SECONDS = int(os.getenv("WEBUI_LOGIN_TEST_TIMEOUT", "20"))
LAST_LOGIN_TEST: dict[str, Any] = {
    "ran_at": None,
    "success": None,
    "details": ["Noch kein Login-Test ausgeführt."],
}

LAST_COOKIE_IMPORT: dict[str, Any] = {
    "ran_at": None,
    "success": None,
    "details": ["Noch kein Cookie-Import durchgeführt."],
}


def run_login_test(username: str, password: str, proxy: str = "") -> dict[str, Any]:
    details: list[str] = []

    if not username.strip() or not password:
        return {
            "ran_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
            "success": False,
            "details": ["Username und Passwort müssen gesetzt sein."],
        }

    login = TwitchLogin(CLIENT_ID, username.strip(), USER_AGENTS["Linux"]["CHROME"], password=password)
    details.append("Login-Test gestartet.")
    details.append(f"Timeout: {LOGIN_TEST_TIMEOUT_SECONDS}s")

    if proxy.strip():
        login.session.proxies.update({"http": proxy.strip(), "https": proxy.strip()})
        details.append("Proxy für Login-Test aktiv.")

    payload = {
        "client_id": CLIENT_ID,
        "undelete_user": False,
        "remember_me": True,
        "username": username.strip(),
        "password": password,
    }

    try:
        http_response = login.session.post(
            "https://passport.twitch.tv/login",
            json=payload,
            timeout=LOGIN_TEST_TIMEOUT_SECONDS,
        )
        details.append(f"HTTP Status: {http_response.status_code}")
        content_type = (http_response.headers.get("content-type") or "unknown").strip()
        details.append(f"Content-Type: {content_type}")

        if http_response.status_code == 404:
            details.append("Der klassische Passwort-Endpoint liefert 404 und wird von Twitch häufig blockiert/abgeschaltet.")
            details.append("Empfehlung: auth-token/cookies im Login-Bereich nutzen und Token-basierten Test ausführen.")
            return {
                "ran_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
                "success": False,
                "details": details,
            }

        response = login.send_login_request(payload)

        if "json" not in content_type.lower():
            details.append("Antwort ist kein JSON (häufig CAPTCHA/Challenge oder Block-Seite).")
            preview = (http_response.text or "").strip()[:200]
            if preview:
                details.append(f"Body-Vorschau: {preview}")
        error_code = response.get("error_code")

        if "access_token" in response:
            details.append("Access Token erhalten.")
            login.set_token(response["access_token"])
            if login.check_login():
                details.append("Token-Prüfung erfolgreich (User-ID konnte ermittelt werden).")
                return {
                    "ran_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
                    "success": True,
                    "details": details,
                }
            details.append("Token erhalten, aber User-ID-Prüfung fehlgeschlagen.")
            return {
                "ran_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
                "success": False,
                "details": details,
            }

        error_map = {
            3001: "Ungültiger Username oder Passwort.",
            3003: "Ungültiger Username oder Passwort.",
            3011: "2FA erforderlich: Authy-Token fehlt.",
            3012: "2FA erforderlich: ungültiges Authy-Token.",
            3022: "Login-Verifizierung erforderlich (E-Mail/Code).",
            3023: "Login-Verifizierung fehlgeschlagen (Code ungültig).",
            1000: "CAPTCHA/Browser-Verifizierung erforderlich.",
        }
        if error_code is not None:
            details.append(f"Twitch Fehlercode: {error_code}")
            details.append(error_map.get(error_code, "Unbekannter Login-Fehlercode von Twitch."))
        else:
            details.append("Login fehlgeschlagen: JSON ohne access_token/error_code erhalten.")

        return {
            "ran_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
            "success": False,
            "details": details,
        }
    except ValueError:
        details.append("JSON konnte nicht geparst werden (ungültige Antwort).")
        return {
            "ran_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
            "success": False,
            "details": details,
        }
    except requests.RequestException as exc:
        details.append(f"Netzwerkfehler beim Login-Test: {exc.__class__.__name__}")
        return {
            "ran_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
            "success": False,
            "details": details,
        }


def _cookie_file_for_username(username: str) -> Path:
    safe_username = re.sub(r"[^a-zA-Z0-9_.-]", "_", username.strip())
    return COOKIES_PATH / f"{safe_username}.pkl"


def save_manual_cookies(username: str, auth_token: str, persistent_id: str = "") -> tuple[bool, list[str]]:
    details: list[str] = []
    if not username.strip() or not auth_token.strip():
        return False, ["Username und auth-token sind erforderlich, um Cookies zu speichern."]

    twitch_login = TwitchLogin(CLIENT_ID, username.strip(), USER_AGENTS["Linux"]["CHROME"], password="")
    twitch_login.token = auth_token.strip()
    twitch_login.user_id = persistent_id.strip()

    cookie_file = _cookie_file_for_username(username)
    cookie_file.parent.mkdir(parents=True, exist_ok=True)
    twitch_login.save_cookies(str(cookie_file))

    details.append(f"Cookie-Datei gespeichert: {cookie_file}")
    details.append("Hinweis: Die Datei wird beim nächsten Miner-Start automatisch genutzt.")
    return True, details


def load_cookies_from_file(cookie_path: str) -> tuple[bool, dict[str, str], list[str]]:
    details: list[str] = []
    target = Path(cookie_path).expanduser()
    if not target.exists() or not target.is_file():
        return False, {}, [f"Cookie-Datei nicht gefunden: {target}"]

    try:
        import pickle

        cookies = pickle.load(target.open("rb"))
        cookie_map = {}
        for item in cookies:
            if isinstance(item, dict) and item.get("name"):
                cookie_map[item["name"]] = str(item.get("value", ""))

        auth_token = cookie_map.get("auth-token", "")
        persistent = cookie_map.get("persistent", "")
        if not auth_token:
            return False, {}, ["Cookie-Datei enthält keinen auth-token."]

        details.append(f"Cookie-Datei geladen: {target}")
        details.append("auth-token und persistent wurden in das Login-Formular übernommen.")
        return True, {"auth_token": auth_token, "persistent": persistent, "cookie_file": str(target)}, details
    except Exception as exc:
        return False, {}, [f"Cookie-Datei konnte nicht gelesen werden: {exc.__class__.__name__}"]


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




def extract_runtime_status(log_lines: list[str]) -> dict[str, Any]:
    status = {"login_ok": False, "login_failed": False, "streamers": {}}

    online_pattern = re.compile(r"Streamer\(username=([^,]+),.*\) is Online")
    offline_pattern = re.compile(r"Streamer\(username=([^,]+),.*\) is Offline")

    for line in log_lines:
        lowered = line.lower()
        if "start session" in lowered or "loading data for" in lowered:
            status["login_ok"] = True
        if "login" in lowered and ("fail" in lowered or "error" in lowered):
            status["login_failed"] = True

        online_match = online_pattern.search(line)
        if online_match:
            status["streamers"][online_match.group(1)] = "ONLINE"

        offline_match = offline_pattern.search(line)
        if offline_match:
            status["streamers"][offline_match.group(1)] = "OFFLINE"

    return status

TEMPLATE = """
<!doctype html>
<html lang="de">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Twitch Miner Control Panel</title>
<style>
body { font-family: Arial, sans-serif; margin: 0; background: #10131a; color: #f5f7ff; }
.container { max-width: 1024px; margin: 0 auto; padding: 20px; }
.card { background: #1a1f2b; border-radius: 10px; padding: 16px; margin-bottom: 16px; }
input, select { width: 100%; padding: 8px; margin: 6px 0 12px; border-radius: 6px; border: 1px solid #37415a; background: #0f1522; color: #fff; }
button { background: #6f42c1; color: #fff; border: 0; padding: 10px 16px; border-radius: 6px; cursor: pointer; }
.button-secondary { background: #2f3b57; }
.row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.button-row { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; }
pre { background: #0c1019; padding: 12px; border-radius: 8px; max-height: 450px; overflow: auto; }
.meta { color: #97a0bb; font-size: 0.9rem; }
.copy-box { display: grid; grid-template-columns: 1fr auto; gap: 8px; align-items: center; }
.copy-box input { margin: 0; font-family: monospace; }
</style></head><body><div class="container">
<h1>Twitch Channel Points Miner – Webinterface</h1>
<p class="meta">Config-Datei: {{ config_path }} | Log-Datei: {{ log_path }}</p>

<div class="card"><h2>Login</h2>
<form method="post" action="{{ url_for('save_login') }}">
<label>Username</label><input name="username" value="{{ config.get('username', '') }}">
<label>Password</label><input name="password" type="password" value="{{ config.get('password', '') }}">
<label>auth-token (optional)</label><input name="auth_token" value="{{ config.get('auth_token', '') }}">
<p class="meta">Wenn Twitch-Login per Passwort blockiert ist (z. B. HTTP 404), nutze auth-token + optional persistent-ID aus deinen Browser-Cookies.</p>
<p class="meta">Token holen: In Twitch eingeloggt <strong>F12 → Konsole</strong> öffnen und folgenden Befehl ausführen:</p>
<div class="copy-box"><input id="token-command" readonly value="document.cookie.split('; ').find(c => c.startsWith('auth-token='))?.split('=')[1]"><button type="button" class="button-secondary" onclick="copyTokenCommand()">Befehl kopieren</button></div>
<label>persistent / User-ID (optional)</label><input name="persistent" value="{{ config.get('persistent', '') }}">
<button type="submit">Login-Daten speichern</button></form>
<h3>Login-Test</h3>
<div class="button-row">
<form method="post" action="{{ url_for('login_test') }}"><button type="submit">Passwort-Login testen</button></form>
<form method="post" action="{{ url_for('login_test_token') }}"><button type="submit" class="button-secondary">Token-Login testen</button></form>
</div>
<p class="meta">Letzter Test: {{ login_test.ran_at or 'Noch nie' }}</p>
<pre>{% for line in login_test.details %}{{ line }}
{% endfor %}</pre>
<h3>Cookie-Import</h3>
<form method="post" action="{{ url_for('save_cookies') }}">
<label>auth-token</label><input name="auth_token" value="{{ config.get('auth_token', '') }}">
<label>persistent (optional User-ID)</label><input name="persistent" value="{{ config.get('persistent', '') }}">
<button type="submit">Cookies speichern und übernehmen</button></form>
<form method="post" action="{{ url_for('import_cookie_file') }}">
<label>Vorhandene Cookie-Datei laden (z. B. /data/cookies/cognifex.pkl)</label><input name="cookie_file" value="{{ config.get('cookie_file', '') }}">
<button type="submit">Cookie-Datei laden</button></form>
<p class="meta">Letzter Import: {{ cookie_import.ran_at or 'Noch nie' }}</p>
<pre>{% for line in cookie_import.details %}{{ line }}
{% endfor %}</pre></div>

<div class="card"><h2>Konfiguration</h2>
<form method="post" action="{{ url_for('save') }}">
<label>Proxy URL (optional)</label><input name="proxy" value="{{ config.get('proxy', '') }}">
<div class="row"><div><label>Autostart-Modus</label><select name="autostart_mode"><option value="disabled" {% if config.get('autostart_mode', 'enabled') == 'disabled' %}selected{% endif %}>Aus (manuell)</option><option value="enabled" {% if config.get('autostart_mode', 'enabled') == 'enabled' %}selected{% endif %}>An</option><option value="max_tries" {% if config.get('autostart_mode', 'enabled') == 'max_tries' %}selected{% endif %}>An mit Max Login-Trys</option></select></div>
<div><label>Max Login-Trys (nur Modus "max_tries")</label><input name="max_login_tries" type="number" min="1" value="{{ config.get('max_login_tries', 3) }}"></div></div>
<label>Streamer (kommagetrennt)</label><input name="streamers" value="{{ ','.join(config.get('streamers', [])) }}">
<div class="row"><div><label>Chat Presence</label><select name="chat_presence">{% for option in ['ALWAYS','NEVER','ONLINE','OFFLINE'] %}<option value="{{ option }}" {% if config.get('chat_presence') == option %}selected{% endif %}>{{ option }}</option>{% endfor %}</select></div>
<div><label>Bet Strategy</label><select name="bet_strategy">{% for option in ['SMART','PERCENTAGE','MOST_VOTED'] %}<option value="{{ option }}" {% if config.get('bet', {}).get('strategy') == option %}selected{% endif %}>{{ option }}</option>{% endfor %}</select></div></div>
<div class="row"><div><label>Bet Percentage</label><input name="bet_percentage" type="number" min="1" max="100" value="{{ config.get('bet', {}).get('percentage', 5) }}"></div><div><label>Max Points</label><input name="bet_max_points" type="number" min="1" value="{{ config.get('bet', {}).get('max_points', 50000) }}"></div></div>
<div class="row"><div><label>Minimum Points</label><input name="bet_minimum_points" type="number" min="0" value="{{ config.get('bet', {}).get('minimum_points', 0) }}"></div><div></div></div>
<button type="submit">Speichern</button></form></div>

<div class="card"><h2>Status</h2>
<p>Autostart-Modus: <strong>{{ config.get('autostart_mode', 'enabled') }}</strong></p>
<p>Login-Status:{% if runtime_status.login_failed %}<strong style="color:#ff7d7d"> Fehlgeschlagen</strong>{% elif runtime_status.login_ok %}<strong style="color:#7dff9a"> Erfolgreich gestartet</strong>{% else %}<strong style="color:#ffd77d"> Noch kein eindeutiger Login-Status</strong>{% endif %}</p></div>
<div class="card"><h2>Monitoring (Live-Log Tail)</h2><pre>{% for line in logs %}{{ line }}
{% endfor %}</pre></div>
</div>
<script>
function copyTokenCommand() {
  const tokenInput = document.getElementById("token-command");
  tokenInput.select();
  tokenInput.setSelectionRange(0, 99999);
  navigator.clipboard.writeText(tokenInput.value);
}
</script>
</body></html>
"""


@app.get("/")
def index() -> str:
    config = load_config()
    logs = read_log_tail()
    saved_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    runtime_status = extract_runtime_status(logs)
    return render_template_string(
        TEMPLATE,
        config=config,
        logs=logs,
        saved_at=saved_at,
        config_path=CONFIG_PATH,
        log_path=LOG_PATH,
        runtime_status=runtime_status,
        login_test=LAST_LOGIN_TEST,
        cookie_import=LAST_COOKIE_IMPORT,
    )


@app.post("/save")
def save() -> Any:
    streamers = [s.strip() for s in request.form.get("streamers", "").split(",") if s.strip()]
    existing = load_config()
    config = {
        "username": existing.get("username", ""),
        "password": existing.get("password", ""),
        "auth_token": existing.get("auth_token", ""),
        "persistent": existing.get("persistent", ""),
        "cookie_file": existing.get("cookie_file", ""),
        "streamers": streamers,
        "chat_presence": request.form.get("chat_presence", "ONLINE"),
        "proxy": request.form.get("proxy", "").strip(),
        "autostart_mode": request.form.get("autostart_mode", "enabled"),
        "max_login_tries": max(1, int(request.form.get("max_login_tries", 3))),
        "bet": {
            "strategy": request.form.get("bet_strategy", "SMART"),
            "percentage": int(request.form.get("bet_percentage", 5)),
            "max_points": int(request.form.get("bet_max_points", 50000)),
            "minimum_points": int(request.form.get("bet_minimum_points", 0)),
        },
    }
    save_config(config)
    return redirect(url_for("index"))


@app.post("/save-login")
def save_login() -> Any:
    config = load_config()
    config["username"] = request.form.get("username", "").strip()
    config["password"] = request.form.get("password", "")
    config["auth_token"] = request.form.get("auth_token", "").strip()
    config["persistent"] = request.form.get("persistent", "").strip()
    save_config(config)
    return redirect(url_for("index"))


@app.post("/login-test")
def login_test() -> Any:
    config = load_config()
    global LAST_LOGIN_TEST
    username = config.get("username", "").strip()
    password = config.get("password", "")
    auth_token = config.get("auth_token", "").strip()
    if username and auth_token and not password:
        twitch_login = TwitchLogin(CLIENT_ID, username, USER_AGENTS["Linux"]["CHROME"], password="")
        twitch_login.set_token(auth_token)
        success = twitch_login.check_login()
        LAST_LOGIN_TEST = {
            "ran_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
            "success": success,
            "details": ["Token-basierter Login-Test ausgeführt.", "Erfolgreich." if success else "Fehlgeschlagen: Token konnte nicht validiert werden."],
        }
    else:
        LAST_LOGIN_TEST = run_login_test(username, password, config.get("proxy", ""))
    return redirect(url_for("index"))


@app.post("/login-test-token")
def login_test_token() -> Any:
    config = load_config()
    username = config.get("username", "").strip()
    auth_token = config.get("auth_token", "").strip()

    global LAST_LOGIN_TEST
    if not username or not auth_token:
        LAST_LOGIN_TEST = {
            "ran_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
            "success": False,
            "details": ["Token-basierter Login-Test konnte nicht gestartet werden.", "Username und auth-token sind erforderlich."],
        }
        return redirect(url_for("index"))

    twitch_login = TwitchLogin(CLIENT_ID, username, USER_AGENTS["Linux"]["CHROME"], password="")
    twitch_login.set_token(auth_token)
    success = twitch_login.check_login()
    LAST_LOGIN_TEST = {
        "ran_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        "success": success,
        "details": ["Token-basierter Login-Test ausgeführt.", "Erfolgreich." if success else "Fehlgeschlagen: Token konnte nicht validiert werden."],
    }
    return redirect(url_for("index"))


@app.post("/save-cookies")
def save_cookies() -> Any:
    config = load_config()
    username = config.get("username", "")
    auth_token = request.form.get("auth_token", "")
    persistent = request.form.get("persistent", "")

    success, details = save_manual_cookies(username, auth_token, persistent)
    if success:
        config["auth_token"] = auth_token.strip()
        config["persistent"] = persistent.strip()
        save_config(config)

    global LAST_COOKIE_IMPORT
    LAST_COOKIE_IMPORT = {
        "ran_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        "success": success,
        "details": details,
    }
    return redirect(url_for("index"))


@app.post("/import-cookie-file")
def import_cookie_file() -> Any:
    config = load_config()
    success, cookie_data, details = load_cookies_from_file(request.form.get("cookie_file", ""))
    if success:
        config.update(cookie_data)
        save_config(config)

    global LAST_COOKIE_IMPORT
    LAST_COOKIE_IMPORT = {
        "ran_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        "success": success,
        "details": details,
    }
    return redirect(url_for("index"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("WEBUI_PORT", "8080")), debug=False)
