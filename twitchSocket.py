"""The 2019 names, kept so scripts written for the old version keep working.

    from twitchSocket import twitchSocket

    ts = twitchSocket(channelName="...", botName="...", botAuth="oauth:...")
    username, message = ts.getMsg()
    ts.sendMsg(message)

New code should use ``twitch_chat.TwitchChat`` (PEP 8 names, see the README).
This file is the only place that knows the old names; nothing else imports it.

What changed underneath (all bug fixes, same calls):
* it connects to irc.chat.twitch.tv:6697 with TLS instead of irc.twitch.tv:6667 in plain text;
* getMsg() waits for the next real chat message. It used to return (None, None) for
  PINGs and other server lines, and main.py then crashed on sendMsg(None);
* messages that contain ":" come through whole, and the other 2019 bugs listed in the
  README ("What the 2019 version got wrong") are gone too.

When can this file go? Not before 2027-11-04, one year after the new names were
announced in the README. Before deleting it, search GitHub for "from twitchSocket import"
to see whether anyone still uses it, then remove this file and its test
(tests/test_legacy.py) and the "Code from 2019" section of the README together.
"""

from __future__ import annotations

import warnings
from typing import Any

from twitch_chat import TwitchChat


class twitchSocket(TwitchChat):  # noqa: N801 - the 2019 name, on purpose
    def __init__(self, channelName: str, botName: str, botAuth: str, **options: Any) -> None:  # noqa: N803
        """Connects and joins right away, like the 2019 version did.

        ``options`` go to TwitchChat (host, port, use_tls, ...); old code passes none.
        """
        warnings.warn(
            "twitchSocket is the 2019 name; use twitch_chat.TwitchChat (see README)",
            DeprecationWarning,
            stacklevel=2,
        )
        super().__init__(channel=channelName, nick=botName, token=botAuth, **options)
        self._bot_name = botName  # printed as given, like before
        self.connect()

    def getMsg(self) -> tuple[str, str]:  # noqa: N802
        """Wait for the next chat message; print and return (username, message)."""
        message = self.read_message()
        print(message.user + ": " + message.text)
        return message.user, message.text

    def sendMsg(self, message: str) -> None:  # noqa: N802
        """Send ``message`` to the channel and print it."""
        self.send_message(message)
        print(self._bot_name + ": " + message)
