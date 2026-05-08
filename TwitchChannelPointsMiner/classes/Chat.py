import logging
import os
import json
import hashlib
import hmac
import time
from enum import Enum, auto
from pathlib import Path
from threading import Thread

from irc.bot import SingleServerIRCBot

from TwitchChannelPointsMiner.constants import IRC, IRC_PORT

logger = logging.getLogger(__name__)


class ChatCapture:
    """
    Optional chat capture with pseudonymized event log + encrypted user mapping.
    Controlled via env vars to avoid breaking existing APIs.
    """

    def __init__(self):
        self.enabled = os.getenv("TWITCH_CHAT_CAPTURE_ENABLED", "false").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self.secret = os.getenv("TWITCH_CHAT_CAPTURE_SECRET", "").encode("utf-8")
        base_path = os.getenv("TWITCH_CHAT_CAPTURE_DIR", "chat_capture")
        self.base_path = Path(base_path)
        self.events_file = self.base_path / "events.jsonl"
        self.mapping_file = self.base_path / "user_mapping.enc"

        self._fernet = None
        if self.enabled and self.secret:
            self.base_path.mkdir(parents=True, exist_ok=True)
            self._init_fernet()

    def _init_fernet(self):
        try:
            from base64 import urlsafe_b64encode
            from cryptography.fernet import Fernet

            key = hashlib.sha256(self.secret).digest()
            self._fernet = Fernet(urlsafe_b64encode(key))
        except Exception as exc:
            logger.warning(
                f"Chat capture mapping encryption unavailable ({exc}). Disable capture or install cryptography."
            )

    def _user_hash(self, username: str) -> str:
        digest = hmac.new(self.secret, username.encode("utf-8"), hashlib.sha256).hexdigest()
        return digest[:24]

    def _read_mapping(self) -> dict:
        if self._fernet is None or not self.mapping_file.exists():
            return {}
        try:
            raw = self.mapping_file.read_bytes()
            data = self._fernet.decrypt(raw)
            return json.loads(data.decode("utf-8"))
        except Exception:
            return {}

    def _write_mapping(self, mapping: dict):
        if self._fernet is None:
            return
        payload = json.dumps(mapping, ensure_ascii=False, sort_keys=True).encode("utf-8")
        encrypted = self._fernet.encrypt(payload)
        self.mapping_file.write_bytes(encrypted)

    def capture(self, channel: str, username: str, message: str):
        if not self.enabled or not self.secret:
            return

        user_hash = self._user_hash(username)
        event = {
            "ts": int(time.time()),
            "channel": channel,
            "user_hash": user_hash,
            "message": message,
        }

        with self.events_file.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(event, ensure_ascii=False) + "\n")

        mapping = self._read_mapping()
        if mapping.get(user_hash) != username:
            mapping[user_hash] = username
            self._write_mapping(mapping)


CHAT_CAPTURE = ChatCapture()


class ChatPresence(Enum):
    ALWAYS = auto()
    NEVER = auto()
    ONLINE = auto()
    OFFLINE = auto()

    def __str__(self):
        return self.name


class ClientIRC(SingleServerIRCBot):
    def __init__(self, username, token, channel):
        self.token = token
        self.channel = "#" + channel
        self.__active = False

        super(ClientIRC, self).__init__(
            [(IRC, IRC_PORT, f"oauth:{token}")], username, username
        )

    def on_welcome(self, client, event):
        client.join(self.channel)

    def start(self):
        self.__active = True
        self._connect()
        while self.__active:
            try:
                self.reactor.process_once(timeout=0.2)
                time.sleep(0.01)
            except Exception as e:
                logger.error(
                    f"Exception raised: {e}. Thread is active: {self.__active}"
                )

    def die(self, msg="Bye, cruel world!"):
        self.connection.disconnect(msg)
        self.__active = False

    def on_pubmsg(self, client, message):
        try:
            tags = message.tags if hasattr(message, "tags") else {}
            username = tags.get("display-name") or message.source.nick
            CHAT_CAPTURE.capture(
                channel=self.channel.lstrip("#"),
                username=username,
                message=message.arguments[0] if message.arguments else "",
            )
        except Exception as exc:
            logger.debug(f"Chat capture failed: {exc}")


class ThreadChat(Thread):
    def __deepcopy__(self, memo):
        return None

    def __init__(self, username, token, channel):
        super(ThreadChat, self).__init__()

        self.username = username
        self.token = token
        self.channel = channel

        self.chat_irc = None

    def run(self):
        self.chat_irc = ClientIRC(self.username, self.token, self.channel)
        logger.info(
            f"Join IRC Chat: {self.channel}", extra={"emoji": ":speech_balloon:"}
        )
        self.chat_irc.start()

    def stop(self):
        if self.chat_irc is not None:
            logger.info(
                f"Leave IRC Chat: {self.channel}", extra={"emoji": ":speech_balloon:"}
            )
            self.chat_irc.die()
