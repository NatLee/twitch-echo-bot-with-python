"""The same echo bot, written with a ready-made IRC package.

Issue #1 asked: why not use an existing IRC package? Fair question. Here is the bot
on top of `irc` (https://pypi.org/project/irc/), the package Twitch's own old Python
sample used. Put it next to main.py + twitch_chat.py and compare:

* the package opens the socket, splits lines, parses them, answers PING and
  reconnects with back-off (STEP 1, 5, 6, 7 and 9 in twitch_chat.py);
* we still do the Twitch-specific parts: the oauth: password, CAP REQ, JOIN,
  reading the display-name tag and the rate limit (STEP 2, 3, 4 and 8).

Run it from the repo root, with the same .env as main.py:

    pip install -r examples/requirements.txt
    python -m examples.irc_library_echo
"""

from __future__ import annotations

import functools
import logging
import ssl
import sys

import irc.bot
import irc.client
import irc.connection
from jaraco.stream import buffer

from config import ConfigError, Settings, load_env_file
from twitch_chat import DEFAULT_CAPABILITIES, DEFAULT_RATE_LIMITS, RateLimiter, default_port, irc_password

# Chat text is not always valid UTF-8; the package's documented way to cope with that.
irc.client.ServerConnection.buffer_class = buffer.LenientDecodingLineBuffer


class EchoBot(irc.bot.SingleServerIRCBot):
    def __init__(self, settings: Settings) -> None:
        self.channel = "#" + settings.channel.lstrip("#").lower()
        self.limiter = RateLimiter(DEFAULT_RATE_LIMITS)
        server = irc.bot.ServerSpec(settings.host, settings.port or default_port(settings.use_tls), irc_password(settings.token))
        super().__init__([server], settings.bot_name.lower(), settings.bot_name, connect_factory=connect_factory(settings))

    def on_welcome(self, connection: irc.client.ServerConnection, event: irc.client.Event) -> None:
        """Logged in (Twitch sent 001): ask for tags, then join."""
        connection.cap("REQ", ":" + " ".join(DEFAULT_CAPABILITIES))
        connection.join(self.channel)

    def on_join(self, connection: irc.client.ServerConnection, event: irc.client.Event) -> None:
        print(f"joined {event.target} as {connection.get_nickname()}", flush=True)

    def on_pubmsg(self, connection: irc.client.ServerConnection, event: irc.client.Event) -> None:
        """A chat message. event.arguments[0] is the text, already parsed for us."""
        text = event.arguments[0]
        tags = {tag["key"]: tag["value"] for tag in event.tags or []}
        print(f"{event.target} {tags.get('display-name') or event.source.nick}: {text}", flush=True)
        self.limiter.wait()
        connection.privmsg(event.target, text)
        print(f"{event.target} {connection.get_nickname()} (bot): {text}", flush=True)


def connect_factory(settings: Settings) -> irc.connection.Factory:
    if not settings.use_tls:
        return irc.connection.Factory()
    context = ssl.create_default_context()
    return irc.connection.Factory(wrapper=functools.partial(context.wrap_socket, server_hostname=settings.host))


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    load_env_file()
    try:
        bot = EchoBot(Settings.from_env())
    except ConfigError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    try:
        bot.start()  # runs forever; the package reconnects on its own
    except KeyboardInterrupt:
        bot.disconnect("bye")
    return 0


if __name__ == "__main__":
    sys.exit(main())
