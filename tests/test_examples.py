"""The two examples: the `irc` package version, and the EventSub version.

The EventSub test uses small fakes of Twitch's HTTP API and EventSub WebSocket that
answer with the shapes from Twitch's docs. Like everything here, no real Twitch.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from config import Settings
from tests.conftest import BOT, CHANNEL, TOKEN, Background, StopBot

REPO = Path(__file__).resolve().parents[1]


# ----- examples/irc_library_echo.py ---------------------------------------------------------


def test_irc_library_example_echoes(server):
    pytest.importorskip("irc")
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "TWITCH_CHANNEL": CHANNEL, "TWITCH_BOT_NAME": BOT,
           "TWITCH_OAUTH_TOKEN": TOKEN, "TWITCH_IRC_HOST": server.host, "TWITCH_IRC_PORT": str(server.port),
           "TWITCH_IRC_TLS": "false"}
    with subprocess.Popen([sys.executable, "-m", "examples.irc_library_echo"], cwd=REPO, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as bot:
        try:
            server.wait_for_join(timeout=15)
            server.say("alice", "hello from the irc package: yes", display_name="Alice")
            server.wait_for(lambda line: line == "PRIVMSG #demo :hello from the irc package: yes")
            server.ping("still-there")
            server.wait_for(lambda line: line.startswith("PONG") and "still-there" in line)
        finally:
            bot.terminate()
        output = bot.communicate(timeout=10)[0]

    sent = server.sent_lines()
    assert sent.index(f"PASS {TOKEN}") < sent.index(f"NICK {BOT}")
    assert "CAP REQ :twitch.tv/tags twitch.tv/commands" in sent
    assert "#demo Alice: hello from the irc package: yes" in output


# ----- examples/eventsub_echo.py -------------------------------------------------------------

BOT_ID, CHANNEL_ID, VIEWER_ID = "100", "200", "300"
API_TOKEN = TOKEN.removeprefix("oauth:")


class FakeTwitchAPI(ThreadingHTTPServer):
    """id.twitch.tv/oauth2/validate and the three Helix calls the example makes."""

    def __init__(self, scopes: tuple[str, ...] = ("chat:read", "chat:edit", "user:read:chat", "user:write:chat")) -> None:
        super().__init__(("127.0.0.1", 0), FakeAPIHandler)
        self.scopes = list(scopes)  # what the fake token is allowed to do
        self.requests: list[tuple[str, str, dict, dict]] = []  # method, path, headers, body
        self.subscribed = threading.Event()
        self.chat_sent = threading.Event()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def bodies(self, path: str) -> list[dict]:
        return [body for _, request_path, _, body in self.requests if request_path == path]


class FakeAPIHandler(BaseHTTPRequestHandler):
    server: FakeTwitchAPI

    def log_message(self, *args: object) -> None:  # keep test output quiet
        pass

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        self.server.requests.append(("GET", url.path, dict(self.headers), {}))
        if url.path == "/oauth2/validate" and self.headers["Authorization"] == f"OAuth {API_TOKEN}":
            self._reply(200, {"client_id": "fake-client", "login": BOT, "user_id": BOT_ID,
                              "scopes": self.server.scopes, "expires_in": 3600})
        elif url.path == "/helix/users" and parse_qs(url.query) == {"login": [CHANNEL]}:
            self._reply(200, {"data": [{"id": CHANNEL_ID, "login": CHANNEL, "display_name": "Demo"}]})
        else:
            self._reply(401, {"status": 401, "message": "invalid request"})

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(("POST", self.path, dict(self.headers), body))
        if self.headers["Authorization"] != f"Bearer {API_TOKEN}" or self.headers["Client-Id"] != "fake-client":
            self._reply(401, {"status": 401, "message": "bad token or client id"})
        elif self.path == "/helix/eventsub/subscriptions":
            self.server.subscribed.set()
            self._reply(202, {"data": [{"id": "sub-1", "status": "enabled", **body}]})
        elif self.path == "/helix/chat/messages":
            self.server.chat_sent.set()
            self._reply(200, {"data": [{"message_id": "msg-1", "is_sent": True}]})
        else:
            self._reply(404, {"status": 404})

    def _reply(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def eventsub(kind: str, payload: dict) -> str:
    return json.dumps({"metadata": {"message_id": f"id-{time.monotonic_ns()}", "message_type": kind,
                                    "message_timestamp": "2026-11-04T00:00:00Z"}, "payload": payload})


def welcome(session_id: str) -> str:
    return eventsub("session_welcome", {"session": {"id": session_id, "status": "connected",
                                                    "keepalive_timeout_seconds": 10, "reconnect_url": None}})


def chat_notification(chatter_id: str, name: str, text: str) -> str:
    event = {"broadcaster_user_id": CHANNEL_ID, "broadcaster_user_login": CHANNEL, "broadcaster_user_name": "Demo",
             "chatter_user_id": chatter_id, "chatter_user_login": name.lower(), "chatter_user_name": name,
             "message_id": f"m-{time.monotonic_ns()}", "message": {"text": text, "fragments": []}}
    return eventsub("notification", {"subscription": {"type": "channel.chat.message", "version": "1"}, "event": event})


def test_eventsub_example_subscribes_echoes_and_skips_its_own_messages(capsys):
    websockets_server = pytest.importorskip("websockets.sync.server")
    from examples import eventsub_echo

    api = FakeTwitchAPI()
    threading.Thread(target=api.serve_forever, daemon=True).start()

    def session(websocket) -> None:
        if websocket.request.path == "/ws":
            websocket.send(welcome("session-1"))
            assert api.subscribed.wait(5)
            websocket.send(eventsub("session_keepalive", {}))
            websocket.send(chat_notification(VIEWER_ID, "Alice", "hello: eventsub"))
            websocket.send(eventsub("session_reconnect", {"session": {"id": "session-1", "status": "reconnecting",
                                                                      "reconnect_url": f"{ws_url}/moved"}}))
            time.sleep(0.5)  # Twitch would close the old socket a bit later
        else:  # the reconnect URL: the subscription comes along, no new POST expected
            websocket.send(welcome("session-2"))
            websocket.send(chat_notification(BOT_ID, BOT, "hello: eventsub"))  # the bot's own echo
            websocket.send(chat_notification(VIEWER_ID, "Bob", "after the move"))
            deadline = time.monotonic() + 5
            while len(api.bodies("/helix/chat/messages")) < 2 and time.monotonic() < deadline:
                time.sleep(0.05)
            time.sleep(0.3)  # room for a (wrong) third send
        # returning closes the connection, which ends bot.listen()

    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        raise StopBot

    with websockets_server.serve(session, "127.0.0.1", 0) as ws_server:
        ws_url = f"ws://127.0.0.1:{ws_server.socket.getsockname()[1]}"
        threading.Thread(target=ws_server.serve_forever, daemon=True).start()
        endpoints = eventsub_echo.Endpoints(id_url=f"{api.url}/oauth2", api_url=f"{api.url}/helix", eventsub_url=f"{ws_url}/ws")
        settings = Settings(channel=CHANNEL, bot_name=BOT, token=TOKEN)
        bot = Background(lambda: eventsub_echo.run(settings, endpoints, sleep=fake_sleep))
        with pytest.raises(StopBot):
            bot.join(timeout=20)
        ws_server.shutdown()
    api.shutdown()

    assert api.bodies("/helix/eventsub/subscriptions") == [{
        "type": "channel.chat.message",
        "version": "1",
        "condition": {"broadcaster_user_id": CHANNEL_ID, "user_id": BOT_ID},
        "transport": {"method": "websocket", "session_id": "session-1"},
    }]
    assert api.bodies("/helix/chat/messages") == [
        {"broadcaster_id": CHANNEL_ID, "sender_id": BOT_ID, "message": "hello: eventsub"},
        {"broadcaster_id": CHANNEL_ID, "sender_id": BOT_ID, "message": "after the move"},
    ]
    assert sleeps == [1]
    out = capsys.readouterr().out
    assert "#demo Alice: hello: eventsub\n#demo echo_bot (bot): hello: eventsub\n" in out


def test_eventsub_example_refuses_a_token_without_chat_scopes():
    pytest.importorskip("websockets")
    from examples import eventsub_echo

    api = FakeTwitchAPI(scopes=("chat:read", "chat:edit"))  # an IRC-only token
    threading.Thread(target=api.serve_forever, daemon=True).start()
    try:
        endpoints = eventsub_echo.Endpoints(id_url=f"{api.url}/oauth2", api_url=f"{api.url}/helix", eventsub_url="ws://unused")
        with pytest.raises(eventsub_echo.TwitchAPIError, match="user:read:chat"):
            eventsub_echo.run(Settings(channel=CHANNEL, bot_name=BOT, token=TOKEN), endpoints)
    finally:
        api.shutdown()
