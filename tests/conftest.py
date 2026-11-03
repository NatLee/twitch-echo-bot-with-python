"""Shared test helpers: a fake Twitch server per test, and a way to connect to it.

Nothing here talks to the real Twitch. Servers listen on 127.0.0.1 with port 0,
so the operating system picks a free port for every test.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from tools.fake_twitch_server import FakeTwitchServer
from twitch_chat import TwitchChat

TOKEN = "oauth:test-token-not-real"
CHANNEL = "demo"
BOT = "echo_bot"


@pytest.fixture
def server() -> Iterator[FakeTwitchServer]:
    with FakeTwitchServer(channels=[CHANNEL], accepted_token=TOKEN) as fake:
        yield fake


@pytest.fixture
def chat_options(server: FakeTwitchServer) -> dict[str, Any]:
    """TwitchChat arguments that point at the fake server (plain TCP, short timeouts)."""
    return {
        "channel": CHANNEL,
        "nick": BOT,
        "token": TOKEN,
        "host": server.host,
        "port": server.port,
        "use_tls": False,
        "handshake_timeout": 2.0,
    }


@pytest.fixture
def connect(chat_options: dict[str, Any]) -> Iterator[Callable[..., TwitchChat]]:
    """connect(**overrides) -> a connected TwitchChat; closed after the test."""
    opened: list[TwitchChat] = []

    def _connect(**overrides: Any) -> TwitchChat:
        chat = TwitchChat(**{**chat_options, **overrides})
        opened.append(chat)
        chat.connect()
        return chat

    yield _connect
    for chat in opened:
        chat.close()


class Background:
    """Run a blocking call in a thread and collect its result or exception."""

    def __init__(self, target: Callable[[], Any]) -> None:
        self.result: Any = None
        self.error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, args=(target,), daemon=True)
        self._thread.start()

    def _run(self, target: Callable[[], Any]) -> None:
        try:
            self.result = target()
        except BaseException as error:  # noqa: B036 - hand every failure back to the test
            self.error = error

    def join(self, timeout: float = 5.0) -> Any:
        self._thread.join(timeout)
        assert not self._thread.is_alive(), "the call is still running"
        if self.error is not None:
            raise self.error
        return self.result


class StopBot(Exception):
    """Raised by a fake sleep() to end a bot's reconnect loop inside a test."""
