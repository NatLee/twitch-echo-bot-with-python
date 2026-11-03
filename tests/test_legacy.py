"""The 2019 names in twitchSocket.py must keep working.

Delete this file together with twitchSocket.py (not before 2027-11-04, see that file).
"""

from __future__ import annotations

import pytest

from tests.conftest import CHANNEL, TOKEN
from twitchSocket import twitchSocket


@pytest.fixture
def legacy(server):
    """The 2019 call, plus the options that point it at the fake server instead of Twitch."""

    def _open(*args, **kwargs):
        with pytest.warns(DeprecationWarning, match="twitch_chat.TwitchChat"):
            return twitchSocket(*args, **kwargs, host=server.host, port=server.port, use_tls=False)

    return _open


def test_old_keyword_call_joins_in_the_constructor(server, legacy):
    ts = legacy(channelName=CHANNEL, botName="Echo_Bot", botAuth=TOKEN)

    server.wait_for_join()
    assert server.sent_lines()[-1] == f"JOIN #{CHANNEL}"
    ts.close()


def test_old_positional_call(server, legacy):
    legacy(CHANNEL, "echo_bot", TOKEN).close()


def test_getmsg_and_sendmsg_print_like_before(server, legacy, capsys):
    ts = legacy(channelName=CHANNEL, botName="Echo_Bot", botAuth=TOKEN)
    server.say("alice", "hi: there")

    assert ts.getMsg() == ("alice", "hi: there")
    ts.sendMsg("hello")

    server.wait_for(lambda line: line == "PRIVMSG #demo :hello")
    assert capsys.readouterr().out == "alice: hi: there\nEcho_Bot: hello\n"
    ts.close()


def test_the_2019_main_loop_survives_a_ping(server, legacy):
    # Regression: in 2019, getMsg() returned (None, None) for a PING and the next line,
    # ts.sendMsg(message), raised TypeError, which main.py did not catch.
    ts = legacy(channelName=CHANNEL, botName="echo_bot", botAuth=TOKEN)
    server.ping()
    server.say("alice", "still here?")

    username, message = ts.getMsg()  # the body of the 2019 main.py loop
    ts.sendMsg(message)

    assert (username, message) == ("alice", "still here?")
    server.wait_for(lambda line: line == "PRIVMSG #demo :still here?")
    assert "PONG :tmi.twitch.tv" in server.sent_lines()
    ts.close()
