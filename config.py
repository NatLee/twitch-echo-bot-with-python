"""Settings for the bots in this repo, read from environment variables.

Keep the token out of your code: put it in a file called .env next to this one
(copy .env.example and fill it in). .env is in .gitignore, so git will not pick it up.
Variables that are already set in your shell win over the file.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path

from twitch_chat import TWITCH_IRC_HOST

ENV_FILE = Path(__file__).with_name(".env")

# name of the environment variable -> what goes in it
REQUIRED = {
    "TWITCH_CHANNEL": "the channel to join, e.g. your own login name",
    "TWITCH_BOT_NAME": "the login name of the bot account",
    "TWITCH_OAUTH_TOKEN": "a user access token of the bot account (see README: Get a token)",
}
FALSE_WORDS = {"0", "false", "no", "off"}


class ConfigError(Exception):
    """A required setting is missing or has a bad value."""


@dataclass(frozen=True)
class Settings:
    channel: str
    bot_name: str
    token: str
    host: str = TWITCH_IRC_HOST
    port: int | None = None  # None: the usual port for TLS or plain text
    use_tls: bool = True

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> Settings:
        missing = [name for name in REQUIRED if not environ.get(name, "").strip()]
        if missing:
            lines = [f"  {name}: {REQUIRED[name]}" for name in missing]
            raise ConfigError("missing settings (put them in .env, see .env.example):\n" + "\n".join(lines))
        port = environ.get("TWITCH_IRC_PORT", "").strip()
        if port and not port.isdigit():
            raise ConfigError(f"TWITCH_IRC_PORT must be a number, got {port!r}")
        return cls(
            channel=environ["TWITCH_CHANNEL"].strip(),
            bot_name=environ["TWITCH_BOT_NAME"].strip(),
            token=environ["TWITCH_OAUTH_TOKEN"].strip(),
            host=environ.get("TWITCH_IRC_HOST", "").strip() or TWITCH_IRC_HOST,
            port=int(port) if port else None,
            use_tls=environ.get("TWITCH_IRC_TLS", "true").strip().lower() not in FALSE_WORDS,
        )


def load_env_file(path: Path = ENV_FILE, environ: MutableMapping[str, str] = os.environ) -> None:
    """Copy KEY=value lines from ``path`` into ``environ``, without overwriting.

    A small stand-in for the python-dotenv package, so the bot needs nothing
    outside the standard library. Blank lines and lines starting with # are skipped;
    quotes around a value are removed.
    """
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        environ.setdefault(key.strip(), value.strip().strip("'\""))
