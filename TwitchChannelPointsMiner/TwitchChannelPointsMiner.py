# -*- coding: utf-8 -*-

import json
import logging
import os
import random
import signal
import sys
import threading
import time
import uuid
from datetime import datetime

from requests.exceptions import RequestException
from pathlib import Path

from TwitchChannelPointsMiner.classes.AnalyticsServer import AnalyticsServer
from TwitchChannelPointsMiner.classes.Chat import ChatPresence, ThreadChat
from TwitchChannelPointsMiner.classes.entities.PubsubTopic import PubsubTopic
from TwitchChannelPointsMiner.classes.entities.Streamer import (
    Streamer,
    StreamerSettings,
)
from TwitchChannelPointsMiner.classes.Exceptions import (
    BadCredentialsException,
    LoginResponseParseException,
    StreamerDoesNotExistException,
)
from TwitchChannelPointsMiner.classes.Settings import FollowersOrder, Priority, Settings
from TwitchChannelPointsMiner.classes.Twitch import Twitch
from TwitchChannelPointsMiner.classes.WebSocketsPool import WebSocketsPool
from TwitchChannelPointsMiner.logger import LoggerSettings, configure_loggers
from TwitchChannelPointsMiner.utils import (
    _millify,
    at_least_one_value_in_settings_is,
    check_versions,
    get_user_agent,
    internet_connection_available,
    set_default_settings,
)

# Suppress:
#   - chardet.charsetprober - [feed]
#   - chardet.charsetprober - [get_confidence]
#   - requests - [Starting new HTTPS connection (1)]
#   - Flask (werkzeug) logs
#   - irc.client - [process_data]
#   - irc.client - [_dispatcher]
#   - irc.client - [_handle_message]
logging.getLogger("chardet.charsetprober").setLevel(logging.ERROR)
logging.getLogger("requests").setLevel(logging.ERROR)
logging.getLogger("werkzeug").setLevel(logging.ERROR)
logging.getLogger("irc.client").setLevel(logging.ERROR)

logger = logging.getLogger(__name__)


def _resolve_login_mode(login_mode, auto_login, prefer_token_login):
    if login_mode is not None:
        normalized_login_mode = str(login_mode).strip().lower()
        if normalized_login_mode in {"none", "token", "credentials"}:
            return normalized_login_mode

        raise ValueError(
            "Unsupported login_mode '{}'. Use 'none', 'token', or 'credentials'.".format(
                login_mode
            )
        )

    if auto_login is False:
        return "none"

    if prefer_token_login is True:
        return "token"

    return "credentials"


LOGIN_ERROR_CATEGORY_MISSING_COOKIES = "missing_cookies"
LOGIN_ERROR_CATEGORY_INVALID_TOKEN = "invalid_token"
LOGIN_ERROR_CATEGORY_HTTP_OR_JSON = "http_or_json_error"
LOGIN_ERROR_CATEGORY_UNKNOWN = "unknown_login_error"

LOGIN_ERROR_EXIT_CODES = {
    LOGIN_ERROR_CATEGORY_MISSING_COOKIES: 20,
    LOGIN_ERROR_CATEGORY_INVALID_TOKEN: 21,
    LOGIN_ERROR_CATEGORY_HTTP_OR_JSON: 22,
    LOGIN_ERROR_CATEGORY_UNKNOWN: 29,
}


def _categorize_login_error(exc):
    message = str(exc).lower()

    if "cookie" in message:
        return LOGIN_ERROR_CATEGORY_MISSING_COOKIES

    if isinstance(exc, BadCredentialsException) or "token" in message or "auth" in message:
        return LOGIN_ERROR_CATEGORY_INVALID_TOKEN

    if isinstance(exc, (RequestException, json.JSONDecodeError, LoginResponseParseException, ValueError)):
        return LOGIN_ERROR_CATEGORY_HTTP_OR_JSON

    return LOGIN_ERROR_CATEGORY_UNKNOWN


def _log_operator_image_commit_hint():
    image_ref = os.getenv("MINER_IMAGE", "unknown-image")
    expected_commit = os.getenv("EXPECTED_COMMIT", "unknown-commit")
    logger.info(
        "Operator hint: Container läuft mit Image %s, erwarteter Commit %s",
        image_ref,
        expected_commit,
        extra={"emoji": ":information_source:"},
    )


def _exit_on_fatal_login_if_enabled(category):
    if os.getenv("TCPM_EXIT_ON_FATAL_LOGIN", "0").strip().lower() not in {"1", "true", "yes", "on"}:
        return

    exit_code = LOGIN_ERROR_EXIT_CODES.get(category, LOGIN_ERROR_EXIT_CODES[LOGIN_ERROR_CATEGORY_UNKNOWN])
    logger.error(
        "Stopping process due to non-recoverable login error category '%s' (exit code: %s).",
        category,
        exit_code,
        extra={"emoji": ":octagonal_sign:"},
    )
    raise SystemExit(exit_code)


class TwitchChannelPointsMiner:
    __slots__ = [
        "username",
        "twitch",
        "claim_drops_startup",
        "priority",
        "streamers",
        "events_predictions",
        "minute_watcher_thread",
        "sync_campaigns_thread",
        "ws_pool",
        "session_id",
        "running",
        "start_datetime",
        "original_streamers",
        "logs_file",
        "queue_listener",
        "login_mode",
        "_is_shutting_down",
    ]

    def __init__(
        self,
        username: str,
        password: str = None,
        claim_drops_startup: bool = False,
        login_mode: str = None,
        auto_login: bool = True,
        prefer_token_login: bool = True,
        allow_credential_login: bool = False,
        # Settings for logging and selenium as you can see.
        priority: list = [Priority.STREAK, Priority.DROPS, Priority.ORDER],
        # This settings will be global shared trought Settings class
        logger_settings: LoggerSettings = LoggerSettings(),
        # Default values for all streamers
        streamer_settings: StreamerSettings = StreamerSettings(),
    ):
        Settings.analytics_path = os.path.join(Path().absolute(), "analytics", username)
        Path(Settings.analytics_path).mkdir(parents=True, exist_ok=True)

        self.username = username

        # Set as global config
        Settings.logger = logger_settings

        # Init as default all the missing values
        streamer_settings.default()
        streamer_settings.bet.default()
        Settings.streamer_settings = streamer_settings

        user_agent = get_user_agent("FIREFOX")
        self.login_mode = _resolve_login_mode(login_mode, auto_login, prefer_token_login)
        if login_mode is None and (auto_login is not True or prefer_token_login is not True):
            logger.warning(
                "Parameters 'auto_login' and 'prefer_token_login' are deprecated. "
                "Use 'login_mode' ('none'|'token'|'credentials') instead."
            )
        self.twitch = Twitch(
            self.username,
            user_agent,
            password,
            login_mode=self.login_mode,
            allow_credential_login=allow_credential_login,
        )

        self.claim_drops_startup = claim_drops_startup
        self.priority = priority if isinstance(priority, list) else [priority]

        self.streamers = []
        self.events_predictions = {}
        self.minute_watcher_thread = None
        self.sync_campaigns_thread = None
        self.ws_pool = None

        self.session_id = str(uuid.uuid4())
        self.running = False
        self.start_datetime = None
        self.original_streamers = []

        self.logs_file, self.queue_listener = configure_loggers(
            self.username, logger_settings
        )
        self._is_shutting_down = False

        # Check for the latest version of the script
        current_version, github_version = check_versions()
        if github_version == "0.0.0":
            logger.error(
                "Unable to detect if you have the latest version of this script"
            )
        elif current_version != github_version:
            logger.info(f"You are running the version {current_version} of this script")
            logger.info(f"The latest version on GitHub is: {github_version}")

        for sign in [signal.SIGINT, signal.SIGSEGV, signal.SIGTERM]:
            signal.signal(sign, self.end)

    def analytics(
        self,
        host: str = "127.0.0.1",
        port: int = 5000,
        refresh: int = 5,
        days_ago: int = 7,
    ):
        http_server = AnalyticsServer(
            host=host, port=port, refresh=refresh, days_ago=days_ago
        )
        http_server.daemon = True
        http_server.name = "Analytics Thread"
        http_server.start()

    def mine(
        self,
        streamers: list = [],
        blacklist: list = [],
        followers: bool = False,
        followers_order: FollowersOrder = FollowersOrder.ASC,
    ):
        self.run(streamers=streamers, blacklist=blacklist, followers=followers)

    def run(
        self,
        streamers: list = [],
        blacklist: list = [],
        followers: bool = False,
        followers_order: FollowersOrder = FollowersOrder.ASC,
    ):
        if self.running:
            logger.error("You can't start multiple sessions of this instance!")
        else:
            logger.info(
                f"Start session: '{self.session_id}'", extra={"emoji": ":bomb:"}
            )
            self.running = True
            self.start_datetime = datetime.now()
            _log_operator_image_commit_hint()

            max_login_tries = max(1, int(os.getenv("TCPM_LOGIN_MAX_TRIES", "1")))
            retry_delay_seconds = max(1, int(os.getenv("TCPM_LOGIN_RETRY_DELAY", "20")))
            keep_alive_on_login_failure = os.getenv("TCPM_KEEP_ALIVE_ON_LOGIN_FAILURE", "0").strip().lower() in {"1", "true", "yes", "on"}
            login_exception = None

            for attempt in range(1, max_login_tries + 1):
                try:
                    self.twitch.login()
                    login_exception = None
                    break
                except Exception as exc:
                    login_exception = exc
                    category = _categorize_login_error(exc)
                    logger.error(
                        "Login failed [%s] (attempt %s/%s): %s",
                        category,
                        attempt,
                        max_login_tries,
                        exc,
                        extra={"emoji": ":no_entry_sign:"},
                    )
                    if category == LOGIN_ERROR_CATEGORY_MISSING_COOKIES:
                        logger.error(
                            "Missing cookies detected. Please provide a valid cookie file before restarting.",
                            extra={"emoji": ":cookie:"},
                        )
                    if attempt < max_login_tries:
                        logger.info("Retrying login in %s seconds...", retry_delay_seconds)
                        time.sleep(retry_delay_seconds)

            if login_exception is not None:
                category = _categorize_login_error(login_exception)
                if keep_alive_on_login_failure:
                    logger.error(
                        "Max login tries exceeded (%s). Keeping process alive for WebUI/manual recovery.",
                        max_login_tries,
                        extra={"emoji": ":warning:"},
                    )
                    while True:
                        time.sleep(30)
                self.end(signum=None, frame=None, exit_code=LOGIN_ERROR_EXIT_CODES.get(category, 0))
                _exit_on_fatal_login_if_enabled(category)
                return

            auth_token = self.twitch.twitch_login.get_auth_token()
            if not auth_token:
                category = LOGIN_ERROR_CATEGORY_INVALID_TOKEN
                logger.error(
                    "No Twitch auth token available. Start aborted because login did not complete. [%s]",
                    category,
                    extra={"emoji": ":no_entry_sign:"},
                )
                self.end(signum=None, frame=None)
                _exit_on_fatal_login_if_enabled(category)
                return

            if self.claim_drops_startup is True:
                self.twitch.claim_all_drops_from_inventory()

            streamers_name: list = []
            streamers_dict: dict = {}

            for streamer in streamers:
                username = (
                    streamer.username
                    if isinstance(streamer, Streamer)
                    else streamer.lower().strip()
                )
                if username not in blacklist:
                    streamers_name.append(username)
                    streamers_dict[username] = streamer

            if followers is True:
                followers_array = self.twitch.get_followers(order=followers_order)
                logger.info(
                    f"Load {len(followers_array)} followers from your profile!",
                    extra={"emoji": ":clipboard:"},
                )
                for username in followers_array:
                    if username not in streamers_dict and username not in blacklist:
                        streamers_name.append(username)
                        streamers_dict[username] = username.lower().strip()

            logger.info(
                f"Loading data for {len(streamers_name)} streamers. Please wait...",
                extra={"emoji": ":nerd_face:"},
            )
            for username in streamers_name:
                if username in streamers_name:
                    time.sleep(random.uniform(0.3, 0.7))
                    try:
                        streamer = (
                            streamers_dict[username]
                            if isinstance(streamers_dict[username], Streamer) is True
                            else Streamer(username)
                        )
                        streamer.channel_id = self.twitch.get_channel_id(username)
                        streamer.settings = set_default_settings(
                            streamer.settings, Settings.streamer_settings
                        )
                        streamer.settings.bet = set_default_settings(
                            streamer.settings.bet, Settings.streamer_settings.bet
                        )
                        if streamer.settings.chat != ChatPresence.NEVER:
                            streamer.irc_chat = ThreadChat(
                                self.username,
                                self.twitch.twitch_login.get_auth_token(),
                                streamer.username,
                            )
                        self.streamers.append(streamer)
                    except StreamerDoesNotExistException:
                        logger.info(
                            f"Streamer {username} does not exist",
                            extra={"emoji": ":cry:"},
                        )

            # Populate the streamers with default values.
            # 1. Load channel points and auto-claim bonus
            # 2. Check if streamers are online
            # 3. DEACTIVATED: Check if the user is a moderator. (was used before the 5th of April 2021 to deactivate predictions)
            for streamer in self.streamers:
                time.sleep(random.uniform(0.3, 0.7))
                self.twitch.load_channel_points_context(streamer)
                self.twitch.check_streamer_online(streamer)
                # self.twitch.viewer_is_mod(streamer)

            self.original_streamers = [
                streamer.channel_points for streamer in self.streamers
            ]

            # If we have at least one streamer with settings = make_predictions True
            make_predictions = at_least_one_value_in_settings_is(
                self.streamers, "make_predictions", True
            )

            # If we have at least one streamer with settings = claim_drops True
            # Spawn a thread for sync inventory and dashboard
            if (
                at_least_one_value_in_settings_is(self.streamers, "claim_drops", True)
                is True
            ):
                self.sync_campaigns_thread = threading.Thread(
                    target=self.twitch.sync_campaigns,
                    args=(self.streamers,),
                )
                self.sync_campaigns_thread.name = "Sync campaigns/inventory"
                self.sync_campaigns_thread.start()
                time.sleep(30)

            self.minute_watcher_thread = threading.Thread(
                target=self.twitch.send_minute_watched_events,
                args=(self.streamers, self.priority),
            )
            self.minute_watcher_thread.name = "Minute watcher"
            self.minute_watcher_thread.start()

            self.ws_pool = WebSocketsPool(
                twitch=self.twitch,
                streamers=self.streamers,
                events_predictions=self.events_predictions,
            )

            # Subscribe to community-points-user. Get update for points spent or gains
            user_id = self.twitch.twitch_login.get_user_id()
            self.ws_pool.submit(
                PubsubTopic(
                    "community-points-user-v1",
                    user_id=user_id,
                )
            )

            # Going to subscribe to predictions-user-v1. Get update when we place a new prediction (confirm)
            if make_predictions is True:
                self.ws_pool.submit(
                    PubsubTopic(
                        "predictions-user-v1",
                        user_id=user_id,
                    )
                )

            for streamer in self.streamers:
                self.ws_pool.submit(
                    PubsubTopic("video-playback-by-id", streamer=streamer)
                )

                if streamer.settings.follow_raid is True:
                    self.ws_pool.submit(PubsubTopic("raid", streamer=streamer))

                if streamer.settings.make_predictions is True:
                    self.ws_pool.submit(
                        PubsubTopic("predictions-channel-v1", streamer=streamer)
                    )

            refresh_context = time.time()
            while self.running:
                time.sleep(random.uniform(20, 60))
                # Do an external control for WebSocket. Check if the thread is running
                # Check if is not None because maybe we have already created a new connection on array+1 and now index is None
                for index in range(0, len(self.ws_pool.ws)):
                    if (
                        self.ws_pool.ws[index].is_reconneting is False
                        and self.ws_pool.ws[index].elapsed_last_ping() > 10
                        and internet_connection_available() is True
                    ):
                        logger.info(
                            f"#{index} - The last PING was sent more than 10 minutes ago. Reconnecting to the WebSocket..."
                        )
                        WebSocketsPool.handle_reconnection(self.ws_pool.ws[index])

                if ((time.time() - refresh_context) // 60) >= 30:
                    refresh_context = time.time()
                    for index in range(0, len(self.streamers)):
                        if self.streamers[index].is_online:
                            self.twitch.load_channel_points_context(
                                self.streamers[index]
                            )

    def end(self, signum=None, frame=None, exit_code=0):
        if self._is_shutting_down:
            logger.debug("Shutdown already in progress; skipping duplicate end() call.")
            return

        self._is_shutting_down = True
        if signum is not None:
            logger.info("Signal %s detected. Please wait just a moment!", signum)
        else:
            logger.info("Shutdown requested. Please wait just a moment!")

        for streamer in self.streamers:
            if (
                streamer.irc_chat is not None
                and streamer.settings.chat != ChatPresence.NEVER
            ):
                streamer.leave_chat()
                if streamer.irc_chat.is_alive() is True:
                    streamer.irc_chat.join()

        self.running = self.twitch.running = False
        if self.ws_pool is not None:
            self.ws_pool.end()

        if self.minute_watcher_thread is not None:
            self.minute_watcher_thread.join()

        if self.sync_campaigns_thread is not None:
            self.sync_campaigns_thread.join()

        # Check if all the mutex are unlocked.
        # Prevent breaks of .json file
        for streamer in self.streamers:
            if streamer.mutex.locked():
                streamer.mutex.acquire()
                streamer.mutex.release()

        self.__print_report()

        # Stop the queue listener to make sure all messages have been logged
        self.queue_listener.stop()

        raise SystemExit(exit_code)

    def __print_report(self):
        print("\n")
        logger.info(
            f"Ending session: '{self.session_id}'", extra={"emoji": ":stop_sign:"}
        )
        if self.logs_file is not None:
            logger.info(
                f"Logs file: {self.logs_file}", extra={"emoji": ":page_facing_up:"}
            )
        logger.info(
            f"Duration {datetime.now() - self.start_datetime}",
            extra={"emoji": ":hourglass:"},
        )

        if self.events_predictions != {}:
            print("")
            for event_id in self.events_predictions:
                event = self.events_predictions[event_id]
                if (
                    event.bet_confirmed is True
                    and event.streamer.settings.make_predictions is True
                ):
                    logger.info(
                        f"{event.streamer.settings.bet}",
                        extra={"emoji": ":wrench:"},
                    )
                    if event.streamer.settings.bet.filter_condition is not None:
                        logger.info(
                            f"{event.streamer.settings.bet.filter_condition}",
                            extra={"emoji": ":pushpin:"},
                        )
                    logger.info(
                        f"{event.print_recap()}",
                        extra={"emoji": ":bar_chart:"},
                    )

        print("")
        for streamer_index in range(0, len(self.streamers)):
            if self.streamers[streamer_index].history != {}:
                gained = (
                    self.streamers[streamer_index].channel_points
                    - self.original_streamers[streamer_index]
                )
                logger.info(
                    f"{repr(self.streamers[streamer_index])}, Total Points Gained (after farming - before farming): {_millify(gained)}",
                    extra={"emoji": ":robot:"},
                )
                if self.streamers[streamer_index].history != {}:
                    logger.info(
                        f"{self.streamers[streamer_index].print_history()}",
                        extra={"emoji": ":moneybag:"},
                    )
