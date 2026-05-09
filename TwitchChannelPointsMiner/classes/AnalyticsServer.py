import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from threading import Thread

import pandas as pd
import requests
from flask import Flask, Response, cli, render_template, request

from TwitchChannelPointsMiner.classes.Settings import Settings
from TwitchChannelPointsMiner.constants import CLIENT_ID, GQLOperations
from TwitchChannelPointsMiner.utils import download_file

cli.show_server_banner = lambda *_: None
logger = logging.getLogger(__name__)

FAVORITES_FILE = "favorites.json"
LIVE_STATUS_CACHE_SECONDS = 60
live_status_cache = {"updated_at": 0, "favorites": tuple(), "data": []}


def streamers_available():
    path = Settings.analytics_path
    return [
        f
        for f in os.listdir(path)
        if os.path.isfile(os.path.join(path, f)) and f.endswith(".json")
    ]


def load_favorites():
    path = os.path.join(Settings.analytics_path, FAVORITES_FILE)
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as favorites_file:
            data = json.load(favorites_file)
            if isinstance(data, list):
                return [str(item).lower() for item in data if str(item).strip()]
    return [streamer.replace(".json", "") for streamer in streamers_available()]


def fetch_streamer_live_status(streamer_name):
    json_data = json.loads(json.dumps(GQLOperations.VideoPlayerStreamInfoOverlayChannel))
    json_data["variables"] = {"channel": streamer_name}
    headers = {
        "Client-Id": CLIENT_ID,
        "Content-Type": "application/json",
        "User-Agent": "Twitch-Channel-Points-Miner-v2 Analytics",
    }
    response = requests.post(
        GQLOperations.url,
        json=json_data,
        headers=headers,
        timeout=10,
    )
    response.raise_for_status()

    user = response.json().get("data", {}).get("user")
    stream = user.get("stream") if user else None
    broadcast = user.get("broadcastSettings", {}) if user else {}

    return {
        "username": streamer_name,
        "online": stream is not None,
        "title": broadcast.get("title", "") if stream else "",
        "viewer_count": stream.get("viewersCount", 0) if stream else 0,
        "url": f"https://www.twitch.tv/{streamer_name}",
    }


def favorites_live_status():
    favorites = tuple(load_favorites())
    now = time.time()
    cache_valid = (
        live_status_cache["favorites"] == favorites
        and now - live_status_cache["updated_at"] < LIVE_STATUS_CACHE_SECONDS
    )
    if cache_valid:
        payload = live_status_cache["data"]
    else:
        payload = []
        for favorite in favorites:
            try:
                payload.append(fetch_streamer_live_status(favorite))
            except requests.RequestException as exc:
                logger.warning(f"Unable to fetch live status for {favorite}: {exc}")
                payload.append(
                    {
                        "username": favorite,
                        "online": False,
                        "title": "",
                        "viewer_count": 0,
                        "url": f"https://www.twitch.tv/{favorite}",
                        "error": "status_unavailable",
                    }
                )

        live_status_cache.update(
            {"updated_at": now, "favorites": favorites, "data": payload}
        )

    return Response(
        json.dumps(
            {
                "cached": cache_valid,
                "cache_ttl_seconds": LIVE_STATUS_CACHE_SECONDS,
                "favorites": payload,
            }
        ),
        status=200,
        mimetype="application/json",
    )


def aggregate(df, freq="30Min"):
    df_base_events = df[(df.z == "Watch") | (df.z == "Claim")]
    df_other_events = df[(df.z != "Watch") & (df.z != "Claim")]

    be = df_base_events.groupby([pd.Grouper(freq=freq, key="datetime"), "z"]).max()
    be = be.reset_index()

    oe = df_other_events.groupby([pd.Grouper(freq=freq, key="datetime"), "z"]).max()
    oe = oe.reset_index()

    result = pd.concat([be, oe])
    return result


def filter_datas(start_date, end_date, datas):
    # Note: https://stackoverflow.com/questions/4676195/why-do-i-need-to-multiply-unix-timestamps-by-1000-in-javascript
    start_date = (
        datetime.strptime(start_date, "%Y-%m-%d").timestamp() * 1000
        if start_date is not None
        else 0
    )
    end_date = (
        datetime.strptime(end_date, "%Y-%m-%d")
        if end_date is not None
        else datetime.now()
    ).replace(hour=23, minute=59, second=59).timestamp() * 1000

    if "series" in datas:
        df = pd.DataFrame(datas["series"])
        df["datetime"] = pd.to_datetime(df.x // 1000, unit="s")

        df = df[(df.x >= start_date) & (df.x <= end_date)]
        df = aggregate(df)

        datas["series"] = (
            df.drop(columns="datetime")
            .sort_values(by="x", ascending=True)
            .to_dict("records")
        )
    else:
        datas["series"] = []

    if "annotations" in datas:
        df = pd.DataFrame(datas["annotations"])
        df["datetime"] = pd.to_datetime(df.x // 1000, unit="s")

        df = df[(df.x >= start_date) & (df.x <= end_date)]

        datas["annotations"] = (
            df.drop(columns="datetime")
            .sort_values(by="x", ascending=True)
            .to_dict("records")
        )
    else:
        datas["annotations"] = []

    return datas


def read_json(streamer, return_response=True):
    start_date = request.args.get("startDate", type=str)
    end_date = request.args.get("endDate", type=str)

    path = Settings.analytics_path
    streamer = streamer if streamer.endswith(".json") else f"{streamer}.json"
    if return_response is True:
        return Response(
            json.dumps(
                filter_datas(
                    start_date, end_date, json.load(open(os.path.join(path, streamer)))
                )
            )
            if streamer in streamers_available()
            else [],
            status=200,
            mimetype="application/json",
        )
    return (
        json.load(open(os.path.join(path, streamer)))
        if streamer in streamers_available()
        else []
    )


def get_challenge_points(streamer):
    datas = read_json(streamer, return_response=False)
    if datas != {}:
        return datas["series"][-1]["y"]
    return 0


def json_all():
    return Response(
        json.dumps(
            [
                {
                    "name": streamer.strip(".json"),
                    "data": read_json(streamer, return_response=False),
                }
                for streamer in streamers_available()
            ]
        ),
        status=200,
        mimetype="application/json",
    )


def index(refresh=5, days_ago=7):
    return render_template(
        "charts.html",
        refresh=(refresh * 60 * 1000),
        daysAgo=days_ago,
    )


def streamers():
    return Response(
        json.dumps(
            [
                {"name": s, "points": get_challenge_points(s)}
                for s in sorted(streamers_available())
            ]
        ),
        status=200,
        mimetype="application/json",
    )


def download_assets(assets_folder, required_files):
    Path(assets_folder).mkdir(parents=True, exist_ok=True)
    logger.info(f"Downloading assets to {assets_folder}")

    for f in required_files:
        if os.path.isfile(os.path.join(assets_folder, f)) is False:
            if (
                download_file(os.path.join("assets", f), os.path.join(assets_folder, f))
                is True
            ):
                logger.info(f"Downloaded {f}")


def check_assets():
    required_files = [
        "banner.png",
        "charts.html",
        "script.js",
        "style.css",
        "dark-theme.css",
    ]
    assets_folder = os.path.join(Path().absolute(), "assets")
    if os.path.isdir(assets_folder) is False:
        logger.info(f"Assets folder not found at {assets_folder}")
        download_assets(assets_folder, required_files)
    else:
        for f in required_files:
            if os.path.isfile(os.path.join(assets_folder, f)) is False:
                logger.info(f"Missing file {f} in {assets_folder}")
                download_assets(assets_folder, required_files)
                break


class AnalyticsServer(Thread):
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 5000,
        refresh: int = 5,
        days_ago: int = 7,
    ):
        super(AnalyticsServer, self).__init__()

        check_assets()

        self.host = host
        self.port = port
        self.refresh = refresh
        self.days_ago = days_ago

        self.app = Flask(
            __name__,
            template_folder=os.path.join(Path().absolute(), "assets"),
            static_folder=os.path.join(Path().absolute(), "assets"),
        )
        self.app.add_url_rule(
            "/",
            "index",
            index,
            defaults={"refresh": refresh, "days_ago": days_ago},
            methods=["GET"],
        )
        self.app.add_url_rule("/streamers", "streamers", streamers, methods=["GET"])
        self.app.add_url_rule(
            "/json/<string:streamer>", "json", read_json, methods=["GET"]
        )
        self.app.add_url_rule("/json_all", "json_all", json_all, methods=["GET"])
        self.app.add_url_rule(
            "/api/favorites/live-status",
            "favorites_live_status",
            favorites_live_status,
            methods=["GET"],
        )

    def run(self):
        logger.info(
            f"Analytics running on http://{self.host}:{self.port}/",
            extra={"emoji": ":globe_with_meridians:"},
        )
        self.app.run(host=self.host, port=self.port, threaded=True, debug=False)
