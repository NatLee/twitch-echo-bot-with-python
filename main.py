"""Twitch echo bot: repeats every chat message back into the channel.

    python main.py           # settings come from .env (copy .env.example)
    python main.py --debug   # also print every raw IRC line (the token is hidden)

How it works is explained step by step in twitch_chat.py and the README.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Callable

from config import ConfigError, Settings, load_env_file
from twitch_chat import AuthenticationError, JoinError, TwitchChat, reconnect_delay

log = logging.getLogger("echo_bot")


def echo(chat: TwitchChat) -> None:
    """The bot's logic: read a message, send the same text back."""
    for message in chat.messages():
        print(f"#{message.channel} {message.display_name}: {message.text}", flush=True)
        reply = message.text  # <- change this line to make your own bot
        chat.send_message(reply)
        print(f"#{chat.channel} {chat.nick} (bot): {reply}", flush=True)


def run(settings: Settings, *, sleep: Callable[[float], None] = time.sleep) -> None:
    """Connect, echo, and reconnect whenever the connection ends."""
    failures = 0
    while True:
        chat = TwitchChat(
            channel=settings.channel,
            nick=settings.bot_name,
            token=settings.token,
            host=settings.host,
            port=settings.port,
            use_tls=settings.use_tls,
        )
        try:
            with chat:
                chat.connect()
                failures = 0
                echo(chat)
        except OSError as error:  # includes ConnectionClosed, refused connections, network trouble
            delay = reconnect_delay(failures)
            failures += 1
            log.warning("connection lost (%s); reconnecting in %s s", error, delay)
            sleep(delay)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Twitch echo bot")
    parser.add_argument("--debug", action="store_true", help="print every IRC line sent and received")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    load_env_file()
    try:
        run(Settings.from_env())
    except ConfigError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except (AuthenticationError, JoinError) as error:
        # Retrying will not help here: fix the token or the channel name first.
        print(f"error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("bye", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
