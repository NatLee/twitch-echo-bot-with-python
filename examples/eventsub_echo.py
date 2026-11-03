"""Next step: the echo bot on the stack Twitch recommends today.

Twitch's docs now say: read chat with EventSub (Twitch pushes events to you over a
WebSocket) and send with the Send Chat Message API, instead of IRC
(https://dev.twitch.tv/docs/chat/). This is the same bot built that way. It follows
Twitch's own example chatbot (https://dev.twitch.tv/docs/chat/chatbot-guide/):

    1. check the token and learn who we are ........ GET  id.twitch.tv/oauth2/validate
    2. look up the channel's user id ................ GET  /helix/users?login=...
    3. open the EventSub WebSocket, read "welcome" .. wss://eventsub.wss.twitch.tv/ws
    4. subscribe to chat within 10 seconds .......... POST /helix/eventsub/subscriptions
    5. for every chat message, send it back ......... POST /helix/chat/messages

Run it from the repo root with the same .env as main.py. The token needs the scopes
user:read:chat and user:write:chat (IRC used chat:read and chat:edit instead):

    pip install -r examples/requirements.txt
    python -m examples.eventsub_echo
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from websockets.exceptions import ConnectionClosed, WebSocketException
from websockets.sync.client import connect

from config import ConfigError, Settings, load_env_file
from twitch_chat import DEFAULT_RATE_LIMITS, RateLimiter, reconnect_delay

log = logging.getLogger("eventsub_echo")

TWITCH_ID_URL = "https://id.twitch.tv/oauth2"
TWITCH_API_URL = "https://api.twitch.tv/helix"
EVENTSUB_WS_URL = "wss://eventsub.wss.twitch.tv/ws"
REQUIRED_SCOPES = ("user:read:chat", "user:write:chat")
WELCOME_TIMEOUT = 10.0  # Twitch closes the WebSocket if we have not subscribed 10 s after "welcome"
KEEPALIVE_MARGIN = 5.0  # extra seconds on top of Twitch's keepalive_timeout_seconds
HTTP_TIMEOUT = 15.0


class TwitchAPIError(Exception):
    pass


@dataclass(frozen=True)
class Endpoints:
    """Where the three Twitch services live. Tests point these at fake servers."""

    id_url: str = TWITCH_ID_URL
    api_url: str = TWITCH_API_URL
    eventsub_url: str = EVENTSUB_WS_URL

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> Endpoints:
        return cls(
            id_url=environ.get("TWITCH_ID_URL") or TWITCH_ID_URL,
            api_url=environ.get("TWITCH_API_URL") or TWITCH_API_URL,
            eventsub_url=environ.get("TWITCH_EVENTSUB_URL") or EVENTSUB_WS_URL,
        )


class TwitchAPI:
    """Just the four HTTP calls this bot needs."""

    def __init__(self, token: str, endpoints: Endpoints) -> None:
        self.token = token.removeprefix("oauth:")  # IRC wants "oauth:", the API does not
        self.endpoints = endpoints
        self.client_id = ""
        self.user_id = ""
        self.login = ""

    def validate(self) -> list[str]:
        """Step 1: who does this token belong to, and what may it do? Returns its scopes."""
        info = self._call("GET", f"{self.endpoints.id_url}/validate", auth=f"OAuth {self.token}")
        self.client_id, self.user_id, self.login = info["client_id"], info["user_id"], info["login"]
        return info.get("scopes", [])

    def user_id_of(self, login: str) -> str:
        """Step 2: EventSub and the API use numeric user ids, not names."""
        query = urllib.parse.urlencode({"login": login.lstrip("#").lower()})
        users = self._call("GET", f"{self.endpoints.api_url}/users?{query}")["data"]
        if not users:
            raise TwitchAPIError(f"no Twitch user called {login!r}")
        return users[0]["id"]

    def subscribe_to_chat(self, session_id: str, broadcaster_id: str) -> None:
        """Step 4: ask Twitch to push this channel's chat messages to our WebSocket."""
        self._call("POST", f"{self.endpoints.api_url}/eventsub/subscriptions", expect=202, body={
            "type": "channel.chat.message",
            "version": "1",
            "condition": {"broadcaster_user_id": broadcaster_id, "user_id": self.user_id},
            "transport": {"method": "websocket", "session_id": session_id},
        })

    def send_chat_message(self, broadcaster_id: str, text: str) -> dict[str, Any]:
        """Step 5: say something. Twitch answers whether it really went out (is_sent)."""
        return self._call("POST", f"{self.endpoints.api_url}/chat/messages", body={
            "broadcaster_id": broadcaster_id,
            "sender_id": self.user_id,
            "message": text,
        })["data"][0]

    def _call(self, method: str, url: str, *, expect: int = 200, body: dict | None = None, auth: str | None = None) -> dict:
        headers = {"Authorization": auth or f"Bearer {self.token}", "Client-Id": self.client_id}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
                status, payload = response.status, response.read()
        except urllib.error.HTTPError as error:
            status, payload = error.code, error.read()
        if status != expect:
            raise TwitchAPIError(f"{method} {url} -> HTTP {status}: {payload.decode(errors='replace')}")
        return json.loads(payload or b"{}")


class EchoBot:
    def __init__(self, settings: Settings, endpoints: Endpoints) -> None:
        self.settings = settings
        self.endpoints = endpoints
        self.api = TwitchAPI(settings.token, endpoints)
        self.limiter = RateLimiter(DEFAULT_RATE_LIMITS)
        self.broadcaster_id = ""

    def prepare(self) -> None:
        """Steps 1 and 2, done once."""
        scopes = self.api.validate()
        missing = [scope for scope in REQUIRED_SCOPES if scope not in scopes]
        if missing:
            raise TwitchAPIError(f"the token is missing the scopes {missing}; get a new one (README: Get a token)")
        if self.api.login != self.settings.bot_name.lower():
            log.warning("the token belongs to %s, not TWITCH_BOT_NAME=%s", self.api.login, self.settings.bot_name)
        self.broadcaster_id = self.api.user_id_of(self.settings.channel)

    def listen(self) -> None:
        """Steps 3 to 5. Returns when Twitch closes the connection."""
        url, subscribed = self.endpoints.eventsub_url, False
        try:
            while True:
                with connect(url) as websocket:
                    welcome = json.loads(websocket.recv(timeout=WELCOME_TIMEOUT))
                    session = welcome["payload"]["session"]
                    if not subscribed:  # after a session_reconnect, Twitch moves our subscription over
                        self.api.subscribe_to_chat(session["id"], self.broadcaster_id)
                        subscribed = True
                        print(f"listening to #{self.settings.channel} as {self.api.login}", flush=True)
                    url = self._read_events(websocket, session["keepalive_timeout_seconds"] + KEEPALIVE_MARGIN)
                    # Simplification: Twitch suggests keeping the old socket open until the new one
                    # says "welcome", so that no event is lost in between. We close it first.
        except ConnectionClosed as closed:
            log.warning("Twitch closed the EventSub connection (%s)", closed)

    def _read_events(self, websocket: Any, timeout: float) -> str:
        """Handle messages until Twitch tells us to move to another server (returns its URL)."""
        while True:
            # EventSub's version of PING: if nothing (not even a keepalive) arrives in time,
            # the connection is dead and recv() raises TimeoutError.
            message = json.loads(websocket.recv(timeout=timeout))
            kind = message["metadata"]["message_type"]
            if kind == "notification":
                self.on_chat_message(message["payload"]["event"])
            elif kind == "session_reconnect":
                return message["payload"]["session"]["reconnect_url"]
            elif kind == "revocation":
                raise TwitchAPIError(f"Twitch cancelled the subscription: {message['payload']['subscription']['status']}")
            # "session_keepalive" needs no answer: it only proves the connection is alive.

    def on_chat_message(self, event: dict[str, Any]) -> None:
        if event["chatter_user_id"] == self.api.user_id:
            return  # unlike IRC, EventSub also delivers the bot's own messages; echoing them would never end
        text = event["message"]["text"]
        print(f"#{event['broadcaster_user_login']} {event['chatter_user_name']}: {text}", flush=True)
        self.limiter.wait()
        result = self.api.send_chat_message(self.broadcaster_id, text)
        if not result["is_sent"]:
            log.warning("Twitch did not send it: %s", result.get("drop_reason"))
            return
        print(f"#{event['broadcaster_user_login']} {self.api.login} (bot): {text}", flush=True)


def run(settings: Settings, endpoints: Endpoints, *, sleep: Callable[[float], None] = time.sleep) -> None:
    bot = EchoBot(settings, endpoints)
    bot.prepare()
    failures = 0
    while True:
        try:
            bot.listen()  # returns after a session that worked and then ended
            failures = 0
        except (OSError, WebSocketException) as error:  # timeouts, refused connections, bad handshakes
            log.warning("could not stay connected (%r)", error)
            failures += 1
        delay = reconnect_delay(failures)
        log.info("reconnecting in %s s", delay)
        sleep(delay)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    load_env_file()
    try:
        run(Settings.from_env(), Endpoints.from_env())
    except (ConfigError, TwitchAPIError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("bye", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
