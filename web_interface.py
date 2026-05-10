from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import uuid
from collections import deque
from datetime import datetime
import re
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, redirect, render_template_string, request, url_for
from werkzeug.serving import make_server

import requests

from TwitchChannelPointsMiner.classes.AnalyticsServer import (
    favorites_live_status,
    index as analytics_index,
    json_all,
    read_json,
    streamers as analytics_streamers,
)
from TwitchChannelPointsMiner.classes.TwitchLogin import TwitchLogin
from TwitchChannelPointsMiner.classes.Telegram import Telegram
from TwitchChannelPointsMiner.classes.Discord import Discord
from TwitchChannelPointsMiner.constants import CLIENT_ID, USER_AGENTS
from TwitchChannelPointsMiner.classes.Chat import ChatPresence
from TwitchChannelPointsMiner.classes.entities.Bet import BetSettings, Condition, DelayMode, OutcomeKeys, Strategy
from TwitchChannelPointsMiner.classes.entities.Streamer import Streamer, StreamerSettings
from TwitchChannelPointsMiner.classes.Settings import Priority

app = Flask(__name__)

CONFIG_PATH = Path(os.getenv("WEBUI_CONFIG_PATH", "/data/config.json"))
LOG_PATH = Path(os.getenv("WEBUI_LOG_PATH", "/data/logs/latest.log"))
COOKIES_PATH = Path(os.getenv("WEBUI_COOKIES_PATH", "/data/cookies"))
MINER_COMMAND = os.getenv("WEBUI_MINER_COMMAND", "python /data/run.py")
WEBUI_LOG_TAIL_LINES = max(10, int(os.getenv("WEBUI_LOG_TAIL_LINES", "200")))
WEBUI_MAX_LOG_LINE_CHARS = max(256, int(os.getenv("WEBUI_MAX_LOG_LINE_CHARS", "4096")))


DEFAULT_CONFIG: dict[str, Any] = {
    "username": "",
    "password": "",
    "auth_token": "",
    "persistent": "",
    "cookie_file": "",
    "streamers": [],
    "blacklist": [],
    "make_predictions": True,
    "follow_raid": True,
    "claim_drops": True,
    "watch_streak": True,
    "chat_presence": "ONLINE",
    "priority": ["STREAK", "DROPS", "ORDER"],
    "proxy": "",
    "autostart_mode": "enabled",
    "max_login_tries": 3,
    "login_mode": "token",
    "bet": {
        "strategy": "SMART",
        "percentage": 5,
        "percentage_gap": 20,
        "max_points": 50000,
        "minimum_points": 0,
        "stealth_mode": False,
        "delay_mode": "FROM_END",
        "delay": 6,
    },
    "telegram": {
        "chat_id": "",
        "token": "",
        "events": ["BET_WIN", "BET_LOSE"],
        "disable_notification": False,
    },
    "discord": {
        "webhook_api": "",
        "events": ["BET_WIN", "BET_LOSE"],
    },
    "analytics": {"host": "127.0.0.1", "port": 5000, "refresh": 5, "days_ago": 7},
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


class MinerProcessManager:
    def __init__(self, command: str):
        self.command = command
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._state = "stopped"
        self._last_error = ""

    def _is_process_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def _refresh_state_locked(self) -> None:
        if self._state == "starting" and self._is_process_running():
            self._state = "running"
            self._last_error = ""
        elif self._process is not None and self._process.poll() is not None:
            if self._state in {"running", "starting"}:
                self._state = "error"
                self._last_error = f"Miner beendet (Code {self._process.poll()})."
            self._process = None

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            self._refresh_state_locked()
            return {
                "state": self._state,
                "pid": self._process.pid if self._is_process_running() else None,
                "command": self.command,
                "last_error": self._last_error,
            }

    def start(self) -> tuple[bool, str]:
        with self._lock:
            self._refresh_state_locked()
            if self._is_process_running() or self._state == "starting":
                return False, "Miner läuft bereits oder startet gerade."
            if not CONFIG_PATH.exists():
                return False, f"Config-Datei fehlt: {CONFIG_PATH}"
            self._state = "starting"
            self._last_error = ""
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            Path("/data/logs").mkdir(parents=True, exist_ok=True)
            try:
                current_config = load_config()
                _write_run_py_from_config(current_config, target_path="/data/run.py")
                login_mode = _sanitize_login_mode(current_config.get("login_mode"))
                mapped_priority = _map_priority_values(current_config.get("priority", DEFAULT_CONFIG["priority"]))
                process_env = os.environ.copy()
                process_env["TWITCH_LOGIN_MODE"] = login_mode
                process_env["TWITCH_PRIORITY"] = json.dumps([item.name for item in mapped_priority])
                process_env["TCPM_LOGS_DIR"] = "/data/logs"

                autostart_mode = str(current_config.get("autostart_mode", "enabled")).strip().lower()
                max_login_tries = max(1, int(current_config.get("max_login_tries", 3)))
                process_env["TCPM_LOGIN_MAX_TRIES"] = str(max_login_tries)
                if autostart_mode == "disabled":
                    process_env["TWITCH_LOGIN_MODE"] = "none"
                if autostart_mode in {"enabled", "max_tries"}:
                    process_env["TCPM_KEEP_ALIVE_ON_LOGIN_FAILURE"] = "1"
                else:
                    process_env["TCPM_KEEP_ALIVE_ON_LOGIN_FAILURE"] = "0"

                auth_token = str(current_config.get("auth_token", "")).strip()
                if auth_token:
                    process_env["TWITCH_AUTH_TOKEN"] = auth_token

                self._process = subprocess.Popen(  # noqa: S603
                    self.command,
                    shell=True,  # noqa: S602
                    stdout=open(LOG_PATH, "a", encoding="utf-8"),  # noqa: SIM115
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    env=process_env,
                    cwd="/data",
                )
                time.sleep(1)
                if self._process.poll() is not None:
                    code = self._process.poll()
                    self._state = "error"
                    self._last_error = f"Miner beendet direkt nach Start (Code {code}). Siehe Log: {LOG_PATH}"
                    self._process = None
                    return False, self._last_error
                self._state = "running"
                return True, "Miner wurde gestartet."
            except Exception as exc:
                self._state = "error"
                self._process = None
                self._last_error = f"Start fehlgeschlagen: {exc.__class__.__name__}"
                return False, self._last_error

    def stop(self) -> tuple[bool, str]:
        with self._lock:
            self._refresh_state_locked()
            if not self._is_process_running():
                self._state = "stopped"
                self._process = None
                return False, "Miner läuft nicht."
            assert self._process is not None
            try:
                self._process.terminate()
                self._process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=10)
            finally:
                self._process = None
                self._state = "stopped"
            return True, "Miner wurde gestoppt."

    def restart(self) -> tuple[bool, str]:
        stop_success, stop_msg = self.stop()
        start_success, start_msg = self.start()
        if start_success:
            if stop_success:
                return True, "Miner wurde neu gestartet."
            return True, f"Miner neu gestartet (vorher: {stop_msg})"
        return False, f"Restart fehlgeschlagen: {start_msg}"


MINER_MANAGER = MinerProcessManager(MINER_COMMAND)


class AnalyticsServerManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._state = "stopped"
        self._last_error = ""
        self._thread: threading.Thread | None = None
        self._server: Any = None
        self._host = "127.0.0.1"
        self._port = 5000

    def _build_app(self, refresh: int, days_ago: int) -> Flask:
        analytics_app = Flask("analytics-webui")
        analytics_app.add_url_rule("/", "index", analytics_index, defaults={"refresh": refresh, "days_ago": days_ago}, methods=["GET"])
        analytics_app.add_url_rule("/streamers", "streamers", analytics_streamers, methods=["GET"])
        analytics_app.add_url_rule("/json/<string:streamer>", "json", read_json, methods=["GET"])
        analytics_app.add_url_rule("/json_all", "json_all", json_all, methods=["GET"])
        analytics_app.add_url_rule("/api/favorites/live-status", "favorites_live_status", favorites_live_status, methods=["GET"])
        return analytics_app

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            return {"state": self._state, "host": self._host, "port": self._port, "last_error": self._last_error}

    def start(self, host: str, port: int, refresh: int, days_ago: int) -> tuple[bool, str]:
        with self._lock:
            if self._state == "running":
                return False, "Analytics-Server läuft bereits."
            try:
                analytics_app = self._build_app(refresh, days_ago)
                self._server = make_server(host, port, analytics_app, threaded=True)
                self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
                self._thread.start()
                self._state = "running"
                self._host = host
                self._port = port
                self._last_error = ""
                return True, "Analytics-Server wurde gestartet."
            except Exception as exc:
                self._state = "error"
                self._last_error = f"Start fehlgeschlagen: {exc.__class__.__name__}"
                return False, self._last_error

    def stop(self) -> tuple[bool, str]:
        with self._lock:
            if self._state != "running" or self._server is None:
                self._state = "stopped"
                return False, "Analytics-Server läuft nicht."
            self._server.shutdown()
            self._server = None
            self._thread = None
            self._state = "stopped"
            return True, "Analytics-Server wurde gestoppt."


ANALYTICS_MANAGER = AnalyticsServerManager()

VALID_LOGIN_MODES = {"none", "token", "credentials"}
PRIORITY_UI_OPTIONS = ["STREAK", "DROPS", "ORDER", "POINTS_ASCENDING", "POINTS_DESCEDING", "SUBSCRIBED"]

BET_STRATEGIES = [strategy.name for strategy in Strategy]
DELAY_MODES = [mode.name for mode in DelayMode]
FILTER_BY_OPTIONS = [
    OutcomeKeys.PERCENTAGE_USERS,
    OutcomeKeys.ODDS_PERCENTAGE,
    OutcomeKeys.ODDS,
    OutcomeKeys.TOP_POINTS,
    OutcomeKeys.TOTAL_USERS,
    OutcomeKeys.TOTAL_POINTS,
    OutcomeKeys.DECISION_USERS,
    OutcomeKeys.DECISION_POINTS,
]
FILTER_WHERE_OPTIONS = [condition.name for condition in Condition]


def _sanitize_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "on", "yes"}


def _normalize_bet_config(bet: dict[str, Any] | None) -> dict[str, Any]:
    bet = dict(bet or {})
    strategy = str(bet.get("strategy", "SMART")).strip().upper()
    if strategy not in BET_STRATEGIES:
        strategy = "SMART"
    delay_mode = str(bet.get("delay_mode", "FROM_END")).strip().upper()
    if delay_mode not in DELAY_MODES:
        delay_mode = "FROM_END"

    filter_condition = bet.get("filter_condition")
    normalized_filter = None
    if isinstance(filter_condition, dict):
        by = str(filter_condition.get("by", "")).strip()
        where = str(filter_condition.get("where", "")).strip().upper()
        value = filter_condition.get("value")
        if by in FILTER_BY_OPTIONS and where in FILTER_WHERE_OPTIONS and str(value).strip() != "":
            normalized_filter = {"by": by, "where": where, "value": float(value)}

    normalized = {
        "strategy": strategy,
        "percentage": int(bet.get("percentage", 5)),
        "percentage_gap": int(bet.get("percentage_gap", 20)),
        "max_points": int(bet.get("max_points", 50000)),
        "minimum_points": int(bet.get("minimum_points", 0)),
        "stealth_mode": bool(bet.get("stealth_mode", False)),
        "delay_mode": delay_mode,
        "delay": float(bet.get("delay", 6)),
    }
    if normalized_filter is not None:
        normalized["filter_condition"] = normalized_filter
    return normalized


def _parse_and_validate_bet_settings(form: dict[str, str], existing_bet: dict[str, Any]) -> dict[str, Any]:
    strategy = str(form.get("bet_strategy", existing_bet.get("strategy", "SMART"))).strip().upper()
    if strategy not in BET_STRATEGIES:
        raise ValueError("Ungültige Bet-Strategie.")

    percentage = int(form.get("bet_percentage", existing_bet.get("percentage", 5)))
    percentage_gap = int(form.get("bet_percentage_gap", existing_bet.get("percentage_gap", 20)))
    max_points = int(form.get("bet_max_points", existing_bet.get("max_points", 50000)))
    minimum_points = int(form.get("bet_minimum_points", existing_bet.get("minimum_points", 0)))
    stealth_mode = _sanitize_bool(form.get("bet_stealth_mode"), bool(existing_bet.get("stealth_mode", False)))
    delay_mode = str(form.get("bet_delay_mode", existing_bet.get("delay_mode", "FROM_END"))).strip().upper()
    delay = float(form.get("bet_delay", existing_bet.get("delay", 6)))

    if delay_mode not in DELAY_MODES:
        raise ValueError("Ungültiger Delay-Modus.")
    if not 1 <= percentage <= 100:
        raise ValueError("Bet Percentage muss zwischen 1 und 100 liegen.")
    if percentage_gap < 0:
        raise ValueError("Percentage Gap muss >= 0 sein.")
    if max_points < 1:
        raise ValueError("Max Points muss >= 1 sein.")
    if minimum_points < 0:
        raise ValueError("Minimum Points muss >= 0 sein.")
    if delay < 0:
        raise ValueError("Delay muss >= 0 sein.")

    if strategy == "SMART" and percentage_gap <= 0:
        raise ValueError("SMART benötigt percentage_gap > 0.")
    if strategy == "PERCENTAGE" and not 1 <= percentage <= 100:
        raise ValueError("PERCENTAGE benötigt percentage zwischen 1 und 100.")

    filter_by = str(form.get("filter_by", "")).strip()
    filter_where = str(form.get("filter_where", "")).strip().upper()
    filter_value_raw = str(form.get("filter_value", "")).strip()

    bet: dict[str, Any] = {
        "strategy": strategy,
        "percentage": percentage,
        "percentage_gap": percentage_gap,
        "max_points": max_points,
        "minimum_points": minimum_points,
        "stealth_mode": stealth_mode,
        "delay_mode": delay_mode,
        "delay": delay,
    }

    has_filter = bool(filter_by or filter_where or filter_value_raw)
    if has_filter:
        if filter_by not in FILTER_BY_OPTIONS:
            raise ValueError("Ungültiges Filterfeld (by).")
        if filter_where not in FILTER_WHERE_OPTIONS:
            raise ValueError("Ungültiger Filteroperator (where).")
        if filter_value_raw == "":
            raise ValueError("Filterwert (value) fehlt.")
        bet["filter_condition"] = {
            "by": filter_by,
            "where": filter_where,
            "value": float(filter_value_raw),
        }

    return bet


class RuntimeStatusStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._session_id = str(uuid.uuid4())
        self._started_at = datetime.utcnow()
        self._login_state = "unknown"
        self._active_streamers: dict[str, str] = {}
        self._errors: list[dict[str, str]] = []
        self._reconnect_count = 0
        self._streak_counts: dict[str, int] = {}
        self._last_log_size = 0

    def ingest_logs(self, log_lines: list[str]) -> None:
        with self._lock:
            new_size = len(log_lines)
            start_idx = self._last_log_size if new_size >= self._last_log_size else 0
            for line in log_lines[start_idx:]:
                lowered = line.lower()
                if "start session" in lowered or "loading data for" in lowered:
                    self._login_state = "ok"
                if "login" in lowered and ("fail" in lowered or "error" in lowered):
                    self._login_state = "failed"
                if "reconnect" in lowered:
                    self._reconnect_count += 1
                if "error" in lowered or "exception" in lowered:
                    self._errors.append(
                        {"timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"), "message": line}
                    )
                    self._errors = self._errors[-200:]

                online_match = re.search(r"Streamer\(username=([^,]+),.*\) is Online", line) or re.search(r"([A-Za-z0-9_]+) is Online!?", line)
                if online_match:
                    username = online_match.group(1).lower()
                    self._active_streamers[username] = "ONLINE"
                    self._streak_counts.setdefault(username, 0)

                offline_match = re.search(r"Streamer\(username=([^,]+),.*\) is Offline", line) or re.search(r"([A-Za-z0-9_]+) is Offline!?", line)
                if offline_match:
                    self._active_streamers.pop(offline_match.group(1).lower(), None)

                streak_match = re.search(r"watch streak", line, re.IGNORECASE)
                if streak_match:
                    streamer_hint = re.search(r"Streamer\(username=([^,]+),", line)
                    if streamer_hint:
                        key = streamer_hint.group(1).lower()
                        self._streak_counts[key] = self._streak_counts.get(key, 0) + 1
            self._last_log_size = new_size

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            uptime_seconds = int((datetime.utcnow() - self._started_at).total_seconds())
            return {
                "session_id": self._session_id,
                "uptime_seconds": uptime_seconds,
                "login_state": self._login_state,
                "active_streamers": sorted(self._active_streamers.keys()),
                "errors_count": len(self._errors),
                "reconnect_count": self._reconnect_count,
                "streak_counts": self._streak_counts,
            }

    def streamers(self) -> dict[str, Any]:
        with self._lock:
            return {"active_streamers": sorted(self._active_streamers.keys())}

    def errors(self) -> dict[str, Any]:
        with self._lock:
            return {"errors": self._errors[-100:]}

    def reset(self) -> None:
        with self._lock:
            self._session_id = str(uuid.uuid4())
            self._started_at = datetime.utcnow()
            self._login_state = "unknown"
            self._active_streamers = {}
            self._errors = []
            self._reconnect_count = 0
            self._streak_counts = {}
            self._last_log_size = 0


RUNTIME_STATUS = RuntimeStatusStore()




def _parse_tag_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in str(raw).split(",") if part.strip()]


def _bool_from_form(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "on", "yes"}


def _normalize_streamer_overrides(overrides: dict[str, Any] | None) -> dict[str, Any]:
    return dict(overrides or {})


def build_streamers_from_config(config: dict[str, Any]) -> list[Streamer]:
    streamers = _parse_tag_list(",".join(config.get("streamers", [])) if isinstance(config.get("streamers"), list) else config.get("streamers", ""))
    return [Streamer(username=s.lower().strip(), settings=StreamerSettings()) for s in streamers if s.strip()]



def _fetch_streamer_online(username: str) -> bool | None:
    try:
        payload = {
            "operationName": "VideoPlayerStreamInfoOverlayChannel",
            "variables": {"channel": username},
            "extensions": {
                "persistedQuery": {
                    "version": 1,
                    "sha256Hash": "198492e0857f6aedead9665c81c5a06d67b25b58034649687124083ff288597d",
                }
            },
        }
        response = requests.post(
            "https://gql.twitch.tv/gql",
            headers={"Client-Id": CLIENT_ID, "Content-Type": "application/json"},
            json=payload,
            timeout=6,
        )
        response.raise_for_status()
        user = response.json().get("data", {}).get("user")
        return bool(user and user.get("stream"))
    except Exception:
        return None


def _build_streamer_presence(config: dict[str, Any], snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    configured = [str(s).strip().lower() for s in config.get("streamers", []) if str(s).strip()]
    joined = set(snapshot.get("active_streamers", []))
    rows: list[dict[str, Any]] = []
    for name in configured:
        online = _fetch_streamer_online(name)
        rows.append({
            "username": name,
            "online": online,
            "joined": name in joined,
            "state": "ONLINE_JOINED" if online and name in joined else ("ONLINE_NOT_JOINED" if online else ("OFFLINE" if online is False else "UNKNOWN")),
        })
    return rows



def _write_run_py_from_config(config: dict[str, Any], target_path: str = "/data/run.py") -> None:
    username = str(config.get("username", "")).strip() or "your-twitch-username"
    streamers = [str(x).strip().lower() for x in config.get("streamers", []) if str(x).strip()]
    streamers_literal = ",\n        ".join([f'"{s}"' for s in streamers]) or '""'
    content = f"""# Auto-generated by WebUI
import os
from TwitchChannelPointsMiner import TwitchChannelPointsMiner
from TwitchChannelPointsMiner.classes.Settings import Priority

miner = TwitchChannelPointsMiner(
    username={username!r},
    password="",
    login_mode=os.getenv("TWITCH_LOGIN_MODE", "token"),
    priority=[Priority.STREAK, Priority.DROPS, Priority.ORDER],
)

miner.mine([
        {streamers_literal}
    ])
"""
    Path(target_path).write_text(content, encoding="utf-8")


def _sanitize_login_mode(value: str | None) -> str:
    mode = (value or "token").strip().lower()
    return mode if mode in VALID_LOGIN_MODES else "token"


def _map_priority_values(values: list[str] | Any) -> list[Priority]:
    if not isinstance(values, list):
        return [Priority.STREAK, Priority.DROPS, Priority.ORDER]
    mapped: list[Priority] = []
    for raw in values:
        key = str(raw or "").strip().upper()
        if key in Priority.__members__:
            mapped.append(Priority[key])
    return mapped or [Priority.STREAK, Priority.DROPS, Priority.ORDER]


def _validate_priority(values: list[str] | Any) -> list[str]:
    selected = [str(v).strip().upper() for v in values] if isinstance(values, list) else []
    warnings: list[str] = []
    if not selected:
        return ["Keine Priorität gewählt. Empfohlen: STREAK, DROPS, ORDER."]
    if "ORDER" in selected and ("POINTS_ASCENDING" in selected or "POINTS_DESCEDING" in selected):
        warnings.append("ORDER zusammen mit POINTS_ASCENDING/POINTS_DESCEDING ist widersprüchlich.")
    if "POINTS_ASCENDING" in selected and "POINTS_DESCEDING" in selected:
        warnings.append("POINTS_ASCENDING und POINTS_DESCEDING gleichzeitig ist widersprüchlich.")
    if "STREAK" in selected and len(selected) == 1:
        warnings.append("Nur STREAK kann nach erfüllten Streaks zu Leerlauf führen.")
    return warnings


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


def _parse_events(value: str) -> list[str]:
    return [event.strip() for event in value.split(",") if event.strip()]


def _safe_chat_id(value: str | int | None) -> int | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def build_logger_settings_payload(config: dict[str, Any]) -> dict[str, Any]:
    telegram_config = config.get("telegram", {}) or {}
    discord_config = config.get("discord", {}) or {}

    return {
        "telegram": {
            "chat_id": _safe_chat_id(telegram_config.get("chat_id")),
            "token": (telegram_config.get("token") or "").strip(),
            "events": [str(e).strip() for e in telegram_config.get("events", []) if str(e).strip()],
            "disable_notification": bool(telegram_config.get("disable_notification", False)),
        },
        "discord": {
            "webhook_api": (discord_config.get("webhook_api") or "").strip(),
            "events": [str(e).strip() for e in discord_config.get("events", []) if str(e).strip()],
        },
    }


def read_log_tail(lines: int = WEBUI_LOG_TAIL_LINES) -> list[str]:
    if not LOG_PATH.exists():
        return ["Noch keine Logs gefunden."]

    with LOG_PATH.open("r", encoding="utf-8", errors="ignore") as fp:
        tail = deque(fp, maxlen=lines)

    result: list[str] = []
    for raw_line in tail:
        line = raw_line.rstrip("\n")
        if len(line) > WEBUI_MAX_LOG_LINE_CHARS:
            line = f"{line[:WEBUI_MAX_LOG_LINE_CHARS]} … [truncated]"
        result.append(line)
    return result




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
.priority-controls { display: grid; grid-template-columns: 1fr auto; gap: 8px; align-items: start; }
.priority-buttons { display: flex; flex-direction: column; gap: 8px; }
.warning { color: #ffd77d; }
</style></head><body><div class="container">
<h1>Twitch Channel Points Miner – Webinterface</h1>
<p class="meta">Config-Datei: {{ config_path }} | Log-Datei: {{ log_path }}</p>
<p class="meta">Analytics: <a href="{{ analytics_url }}" target="_blank" rel="noopener">{{ analytics_url }}</a> | Health: <strong style="color:{% if analytics_health.reachable %}#7dff9a{% else %}#ff9f9f{% endif %}">{% if analytics_health.reachable %}reachable{% else %}not reachable{% endif %}</strong></p>

<div class="card"><h2>Login</h2>
<form method="post" action="{{ url_for('save_login') }}">
<label>Username</label><input name="username" value="{{ config.get('username', '') }}">
<label>Password</label><input name="password" type="password" value="{{ config.get('password', '') }}">
<label>auth-token (optional)</label><input name="auth_token" type="password" autocomplete="off" value="{{ config.get('auth_token', '') }}">
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
<label>auth-token</label><input name="auth_token" type="password" autocomplete="off" value="{{ config.get('auth_token', '') }}">
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
<label>Blacklist (kommagetrennt oder zeilenweise)</label><input name="blacklist" value="{{ ','.join(config.get('blacklist', [])) }}">
<div class="row"><div><label>Login-Modus</label><select name="login_mode"><option value="token" {% if config.get('login_mode', 'token') == 'token' %}selected{% endif %}>token (empfohlen)</option><option value="credentials" {% if config.get('login_mode', 'token') == 'credentials' %}selected{% endif %}>credentials</option><option value="none" {% if config.get('login_mode', 'token') == 'none' %}selected{% endif %}>none</option></select></div><div></div></div>
<p class="meta"><strong>token</strong>: Start nur über vorhandene Cookies/Token (ideal für Container ohne interaktiven Login). <strong>credentials</strong>: erlaubt Username/Passwort-Login beim Start (lokal/interaktiv). <strong>none</strong>: überspringt Startup-Login komplett; nutze das nur, wenn Session/Cookies bereits vorbereitet sind.</p>
<div class="row"><div><label>Chat Presence</label><select name="chat_presence">{% for option in ['ALWAYS','NEVER','ONLINE','OFFLINE'] %}<option value="{{ option }}" {% if config.get('chat_presence') == option %}selected{% endif %}>{{ option }}</option>{% endfor %}</select></div>
<div><label>Bet Strategy</label><select name="bet_strategy">{% for option in bet_strategies %}<option value="{{ option }}" {% if config.get('bet', {}).get('strategy') == option %}selected{% endif %}>{{ option }}</option>{% endfor %}</select></div></div>
<div class="row"><div><label>Bet Percentage</label><input name="bet_percentage" type="number" min="1" max="100" value="{{ config.get('bet', {}).get('percentage', 5) }}"></div><div><label>Max Points</label><input name="bet_max_points" type="number" min="1" value="{{ config.get('bet', {}).get('max_points', 50000) }}"></div></div>
<div class="row"><div><label>Minimum Points</label><input name="bet_minimum_points" type="number" min="0" value="{{ config.get('bet', {}).get('minimum_points', 0) }}"></div><div></div></div>
<h3>Telegram</h3>
<div class="row"><div><label>Chat ID</label><input name="telegram_chat_id" value="{{ config.get('telegram', {}).get('chat_id', '') }}"></div><div><label>Token</label><input name="telegram_token" type="password" autocomplete="off" value="{{ config.get('telegram', {}).get('token', '') }}"></div></div>
<div class="row"><div><label>Events (kommagetrennt)</label><input name="telegram_events" value="{{ ','.join(config.get('telegram', {}).get('events', [])) }}"></div><div><label><input type="checkbox" name="telegram_disable_notification" {% if config.get('telegram', {}).get('disable_notification') %}checked{% endif %}> Disable Notification</label></div></div>
<h3>Discord</h3>
<div class="row"><div><label>Webhook API</label><input name="discord_webhook_api" type="password" autocomplete="off" value="{{ config.get('discord', {}).get('webhook_api', '') }}"></div><div><label>Events (kommagetrennt)</label><input name="discord_events" value="{{ ','.join(config.get('discord', {}).get('events', [])) }}"></div></div>
<div class="button-row">
<button type="submit" formaction="{{ url_for('send_test_message') }}" formmethod="post" class="button-secondary">Testnachricht senden</button>
</div>
<button type="submit">Speichern</button></form></div>

<div class="card"><h2>Analytics</h2>
<form method="post" action="{{ url_for('save_analytics') }}">
<div class="row"><div><label>Host</label><input name="analytics_host" value="{{ config.get('analytics', {}).get('host', '127.0.0.1') }}"></div><div><label>Port</label><input name="analytics_port" type="number" min="1" max="65535" value="{{ config.get('analytics', {}).get('port', 5000) }}"></div></div>
<div class="row"><div><label>Refresh (Minuten)</label><input name="analytics_refresh" type="number" min="1" value="{{ config.get('analytics', {}).get('refresh', 5) }}"></div><div><label>Days Ago</label><input name="analytics_days_ago" type="number" min="1" value="{{ config.get('analytics', {}).get('days_ago', 7) }}"></div></div>
<button type="submit">Analytics-Settings speichern</button></form>
<div class="button-row">
<form method="post" action="{{ url_for('analytics_start') }}"><button type="submit">Analytics starten</button></form>
<form method="post" action="{{ url_for('analytics_stop') }}"><button type="submit" class="button-secondary">Analytics stoppen</button></form>
</div>
<p>Status: <strong>{{ analytics_status.state }}</strong></p>
{% if analytics_status.last_error %}<p style="color:#ff9f9f">Letzter Fehler: {{ analytics_status.last_error }}</p>{% endif %}
{% if analytics_action_message %}<p class="meta">Aktion: {{ analytics_action_message }}</p>{% endif %}
<h3>Favorites / Live-Status</h3>
<iframe src="{{ analytics_url }}/api/favorites/live-status" style="width:100%;height:220px;border:1px solid #37415a;border-radius:8px;background:#0c1019;"></iframe>
</div>

<div class="card"><h2>Status</h2>
<p>Autostart-Modus: <strong>{{ config.get('autostart_mode', 'enabled') }}</strong></p>
<div class="button-row">
<form method="post" action="{{ url_for('miner_start') }}"><button type="submit">Miner starten</button></form>
<form method="post" action="{{ url_for('miner_stop') }}"><button type="submit" class="button-secondary">Miner stoppen</button></form>
<form method="post" action="{{ url_for('miner_restart') }}"><button type="submit" class="button-secondary">Miner neu starten</button></form>
</div>
<p>Miner-Status: <strong id="miner-state">{{ miner_status.state }}</strong> <span id="miner-pid">{% if miner_status.pid %}(PID {{ miner_status.pid }}){% endif %}</span></p>
<p style="color:#ff9f9f" id="miner-error">{% if miner_status.last_error %}Letzter Fehler: {{ miner_status.last_error }}{% endif %}</p>
<p class="meta">Startkommando: {{ miner_status.command }}</p>
{% if miner_action_message %}<p class="meta">Aktion: {{ miner_action_message }}</p>{% endif %}
<p>Session-ID: <strong id="session-id">-</strong> | Uptime: <strong id="uptime">-</strong></p>
<p>Login-Status: <strong id="login-state">-</strong> | Reconnects: <strong id="reconnect-count">0</strong></p>
<p>Aktive Streamer: <strong id="active-streamers">-</strong></p>
<p>Streak-Counts: <strong id="streak-counts">-</strong></p>
<p>Join-Status: <strong id="join-status">-</strong></p>
<p>Errors: <strong id="error-count">0</strong></p></div>
<div class="card"><h2>Debug (Live-Log Tail)</h2>
<div class="button-row"><button type="button" class="button-secondary" onclick="clearDebugState()">Debug + Status clearen</button></div>
<pre>{% for line in logs %}{{ line }}
{% endfor %}</pre></div>
</div>
<script>
function copyTokenCommand() {
  const tokenInput = document.getElementById("token-command");
  tokenInput.select();
  tokenInput.setSelectionRange(0, 99999);
  navigator.clipboard.writeText(tokenInput.value);
}
function fmtUptime(sec) {
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  return `${h}h ${m}m ${s}s`;
}
async function refreshApiStatus() {
  const [statusRes, minerRes] = await Promise.all([
    fetch('/api/status'),
    fetch('/api/miner/status')
  ]);
  const status = await statusRes.json();
  const miner = await minerRes.json();
  document.getElementById('session-id').textContent = status.session_id || '-';
  document.getElementById('uptime').textContent = fmtUptime(status.uptime_seconds || 0);
  document.getElementById('login-state').textContent = status.login_state || 'unknown';
  document.getElementById('reconnect-count').textContent = status.reconnect_count || 0;
  document.getElementById('active-streamers').textContent = (status.active_streamers || []).join(', ') || '-';
  document.getElementById('error-count').textContent = status.errors_count || 0;
  const streaks = status.streak_counts || {};
  const streakText = Object.keys(streaks).length ? Object.entries(streaks).map(([k,v]) => `${k}: ${v}`).join(', ') : '-';
  document.getElementById('streak-counts').textContent = streakText;
  const presence = status.streamer_presence || [];
  const presenceText = presence.length ? presence.map(p => `${p.username}:${p.state}`).join(' | ') : '-';
  document.getElementById('join-status').textContent = presenceText;
  document.getElementById('miner-state').textContent = miner.state || '-';
  document.getElementById('miner-pid').textContent = miner.pid ? `(PID ${miner.pid})` : '';
  document.getElementById('miner-error').textContent = miner.last_error ? `Letzter Fehler: ${miner.last_error}` : '';
}
async function clearDebugState() {
  const res = await fetch('/api/debug/clear', { method: 'POST' });
  const payload = await res.json();
  if (!res.ok || !payload.success) {
    alert(`Clear fehlgeschlagen: ${payload.message || 'unbekannter Fehler'}`);
    return;
  }
  window.location.reload();
}
refreshApiStatus();
setInterval(refreshApiStatus, 5000);
</script>
</body></html>
"""


@app.get("/")
def index() -> str:
    config = load_config()
    config["streamer_overrides"] = _normalize_streamer_overrides(config.get("streamer_overrides", {}))
    logs = read_log_tail()
    saved_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    RUNTIME_STATUS.ingest_logs(logs)
    analytics = config.get("analytics", {})
    analytics_url = f"http://{analytics.get('host', '127.0.0.1')}:{analytics.get('port', 5000)}"
    analytics_health = ANALYTICS_MANAGER.get_status()
    return render_template_string(
        TEMPLATE,
        config=config,
        logs=logs,
        saved_at=saved_at,
        config_path=CONFIG_PATH,
        log_path=LOG_PATH,
        login_test=LAST_LOGIN_TEST,
        cookie_import=LAST_COOKIE_IMPORT,
        miner_status=MINER_MANAGER.get_status(),
        miner_action_message=request.args.get("miner_message", ""),
        analytics_url=analytics_url,
        analytics_status=ANALYTICS_MANAGER.get_status(),
        analytics_action_message=request.args.get("analytics_message", ""),
        analytics_health=analytics_health,
    )


@app.post("/save")
def save() -> Any:
    streamers = _parse_tag_list(request.form.get("streamers", ""))
    blacklist = _parse_tag_list(request.form.get("blacklist", ""))
    existing = load_config()
    streamer_overrides: dict[str, Any] = {}
    for streamer_name in streamers:
        key = streamer_name.lower().strip()
        streamer_overrides[key] = {
            "make_predictions": _bool_from_form(request.form.get(f"ov_{key}_make_predictions")),
            "follow_raid": _bool_from_form(request.form.get(f"ov_{key}_follow_raid")),
            "claim_drops": _bool_from_form(request.form.get(f"ov_{key}_claim_drops")),
            "watch_streak": _bool_from_form(request.form.get(f"ov_{key}_watch_streak")),
            "chat": request.form.get(f"ov_{key}_chat", existing.get("chat_presence", "ONLINE")).strip().upper(),
            "bet": {
                "strategy": request.form.get(f"ov_{key}_bet_strategy", "SMART").strip().upper(),
                "percentage": int(request.form.get(f"ov_{key}_bet_percentage", existing.get("bet", {}).get("percentage", 5))),
                "max_points": int(request.form.get(f"ov_{key}_bet_max_points", existing.get("bet", {}).get("max_points", 50000))),
                "minimum_points": int(request.form.get(f"ov_{key}_bet_minimum_points", existing.get("bet", {}).get("minimum_points", 0))),
            },
        }
    priority_values = [v for v in request.form.getlist("priority") if str(v).strip()]
    config = {
        "username": existing.get("username", ""),
        "password": existing.get("password", ""),
        "auth_token": existing.get("auth_token", ""),
        "persistent": existing.get("persistent", ""),
        "cookie_file": existing.get("cookie_file", ""),
        "streamers": streamers,
        "blacklist": blacklist,
        "chat_presence": request.form.get("chat_presence", "ONLINE"),
        "priority": priority_values or existing.get("priority", DEFAULT_CONFIG["priority"]),
        "proxy": request.form.get("proxy", "").strip(),
        "autostart_mode": request.form.get("autostart_mode", "enabled"),
        "max_login_tries": max(1, int(request.form.get("max_login_tries", 3))),
        "login_mode": _sanitize_login_mode(request.form.get("login_mode", existing.get("login_mode", "token"))),
        "make_predictions": existing.get("make_predictions", True),
        "follow_raid": existing.get("follow_raid", True),
        "claim_drops": existing.get("claim_drops", True),
        "watch_streak": existing.get("watch_streak", True),
        "bet": {
            "strategy": request.form.get("bet_strategy", "SMART"),
            "percentage": int(request.form.get("bet_percentage", 5)),
            "max_points": int(request.form.get("bet_max_points", 50000)),
            "minimum_points": int(request.form.get("bet_minimum_points", 0)),
        },
        "telegram": {
            "chat_id": request.form.get("telegram_chat_id", "").strip(),
            "token": request.form.get("telegram_token", "").strip(),
            "events": _parse_events(request.form.get("telegram_events", "")),
            "disable_notification": request.form.get("telegram_disable_notification") == "on",
        },
        "discord": {
            "webhook_api": request.form.get("discord_webhook_api", "").strip(),
            "events": _parse_events(request.form.get("discord_events", "")),
        },
    }
    save_config(config)
    return redirect(url_for("index"))


@app.post("/save-analytics")
def save_analytics() -> Any:
    config = load_config()
    config["analytics"] = {
        "host": request.form.get("analytics_host", "127.0.0.1").strip() or "127.0.0.1",
        "port": max(1, min(65535, int(request.form.get("analytics_port", 5000)))),
        "refresh": max(1, int(request.form.get("analytics_refresh", 5))),
        "days_ago": max(1, int(request.form.get("analytics_days_ago", 7))),
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


@app.post("/miner/start")
def miner_start() -> Any:
    config = load_config()
    build_streamers_from_config(config)
    _, message = MINER_MANAGER.start()
    return redirect(url_for("index", miner_message=message))


@app.post("/miner/stop")
def miner_stop() -> Any:
    _, message = MINER_MANAGER.stop()
    return redirect(url_for("index", miner_message=message))


@app.post("/miner/restart")
def miner_restart() -> Any:
    _, message = MINER_MANAGER.restart()
    return redirect(url_for("index", miner_message=message))


@app.post("/analytics/start")
def analytics_start() -> Any:
    config = load_config()
    analytics = config.get("analytics", {})
    success, message = ANALYTICS_MANAGER.start(
        host=str(analytics.get("host", "127.0.0.1")),
        port=int(analytics.get("port", 5000)),
        refresh=int(analytics.get("refresh", 5)),
        days_ago=int(analytics.get("days_ago", 7)),
    )
    return redirect(url_for("index", analytics_message=message if success else f"Fehler: {message}"))


@app.post("/analytics/stop")
def analytics_stop() -> Any:
    _, message = ANALYTICS_MANAGER.stop()
    return redirect(url_for("index", analytics_message=message))


@app.get("/api/miner/status")
def api_miner_status() -> Any:
    return jsonify(MINER_MANAGER.get_status())


@app.get("/api/status")
def api_status() -> Any:
    RUNTIME_STATUS.ingest_logs(read_log_tail())
    snapshot = RUNTIME_STATUS.snapshot()
    snapshot["streamer_presence"] = _build_streamer_presence(load_config(), snapshot)
    return jsonify(snapshot)


@app.get("/api/streamers")
def api_streamers() -> Any:
    RUNTIME_STATUS.ingest_logs(read_log_tail())
    return jsonify(RUNTIME_STATUS.streamers())


@app.get("/api/errors")
def api_errors() -> Any:
    RUNTIME_STATUS.ingest_logs(read_log_tail())
    return jsonify(RUNTIME_STATUS.errors())


@app.post("/api/miner/start")
def api_miner_start() -> Any:
    success, message = MINER_MANAGER.start()
    payload = MINER_MANAGER.get_status()
    payload.update({"success": success, "message": message})
    return jsonify(payload), (200 if success else 409)


@app.post("/api/miner/stop")
def api_miner_stop() -> Any:
    success, message = MINER_MANAGER.stop()
    payload = MINER_MANAGER.get_status()
    payload.update({"success": success, "message": message})
    return jsonify(payload), (200 if success else 409)


@app.post("/api/miner/restart")
def api_miner_restart() -> Any:
    success, message = MINER_MANAGER.restart()
    payload = MINER_MANAGER.get_status()
    payload.update({"success": success, "message": message})
    return jsonify(payload), (200 if success else 409)


@app.post("/api/debug/clear")
def api_debug_clear() -> Any:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        LOG_PATH.write_text("", encoding="utf-8")
        RUNTIME_STATUS.reset()
        return jsonify({"success": True, "message": "Debug-Log und Runtime-Status wurden geleert."})
    except Exception as exc:
        return jsonify({"success": False, "message": f"{exc.__class__.__name__}: {exc}"}), 500


@app.post("/send-test-message")
def send_test_message() -> Any:
    config = load_config()
    payload = build_logger_settings_payload(config)
    errors: list[str] = []
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    text = f"WebUI Testnachricht ({now})"

    telegram_data = payload["telegram"]
    if telegram_data["chat_id"] and telegram_data["token"]:
        try:
            Telegram(
                chat_id=telegram_data["chat_id"],
                token=telegram_data["token"],
                events=telegram_data["events"],
                disable_notification=telegram_data["disable_notification"],
            ).send(text, "WATCH_STREAK")
        except Exception as exc:
            errors.append(f"Telegram Fehler: {exc.__class__.__name__}")
    else:
        errors.append("Telegram nicht konfiguriert (chat_id/token fehlen).")

    discord_data = payload["discord"]
    if discord_data["webhook_api"]:
        try:
            Discord(webhook_api=discord_data["webhook_api"], events=discord_data["events"]).send(text, "WATCH_STREAK")
        except Exception as exc:
            errors.append(f"Discord Fehler: {exc.__class__.__name__}")
    else:
        errors.append("Discord nicht konfiguriert (webhook_api fehlt).")

    message = "Testnachricht gesendet." if not errors else " | ".join(errors)
    return redirect(url_for("index", miner_message=message))


@app.get("/api/config/logger-settings")
def api_logger_settings() -> Any:
    return jsonify(build_logger_settings_payload(load_config()))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("WEBUI_PORT", "8080")), debug=False)
