"""TwitchChat against the fake Twitch server: the handshake, reading, PING, sending, endings.

Tests marked "Regression" failed with the 2019 twitchSocket.py; the reproduction
script and its output are kept in the revamp notes.
"""

from __future__ import annotations

import logging
import ssl
import time

import pytest
import trustme

from tests.conftest import BOT, CHANNEL, TOKEN, Background
from tools.fake_twitch_server import FakeTwitchServer
from twitch_chat import (
    DEFAULT_RATE_LIMITS,
    TWITCH_IRC_HOST,
    TWITCH_IRC_PLAIN_PORT,
    TWITCH_IRC_TLS_PORT,
    AuthenticationError,
    ConnectionClosed,
    JoinError,
    TwitchChat,
)

ALICE = ":alice!alice@alice.tmi.twitch.tv PRIVMSG #demo :"


# ----- STEP 1 to 4: connecting --------------------------------------------------------


def test_defaults_are_twitchs_tls_endpoint():
    chat = TwitchChat(channel="demo", nick="bot", token="t")

    assert (chat.host, chat.port, chat.use_tls) == (TWITCH_IRC_HOST, TWITCH_IRC_TLS_PORT, True)
    assert TwitchChat(channel="demo", nick="bot", token="t", use_tls=False).port == TWITCH_IRC_PLAIN_PORT


def test_handshake_sends_cap_pass_nick_join_in_order(server, connect):
    connect()

    assert server.sent_lines() == [
        "CAP REQ :twitch.tv/tags twitch.tv/commands",
        f"PASS {TOKEN}",
        f"NICK {BOT}",
        f"JOIN #{CHANNEL}",
    ]


def test_names_and_token_are_normalised(server, connect):
    connect(channel="#Demo", nick="Echo_Bot", token=TOKEN.removeprefix("oauth:"))

    assert server.sent_lines()[1:] == [f"PASS {TOKEN}", "NICK echo_bot", "JOIN #demo"]


def test_wrong_token_raises_authentication_error_with_twitchs_words(connect):
    # Regression: the 2019 constructor kept looping after Twitch hung up.
    with pytest.raises(AuthenticationError, match="Login authentication failed"):
        connect(token="oauth:wrong")


def test_unknown_channel_times_out_with_a_hint(connect):
    # Regression: Twitch ignores a JOIN for a channel that does not exist; the 2019 code waited forever.
    started = time.monotonic()

    with pytest.raises(JoinError, match="spelled right"):
        connect(channel="no_such_channel", handshake_timeout=0.5)
    assert time.monotonic() - started < 3


def test_suspended_channel_notice_becomes_join_error():
    with FakeTwitchServer(channels=[CHANNEL], suspended_channels=["gone"], accepted_token=TOKEN) as fake:
        chat = TwitchChat(channel="gone", nick=BOT, token=TOKEN, host=fake.host, port=fake.port, use_tls=False)
        with chat, pytest.raises(JoinError, match="has been suspended"):
            chat.connect()


def test_tls_connection_checks_the_certificate():
    ca = trustme.CA()
    server_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ca.issue_cert("127.0.0.1").configure_cert(server_context)
    trusting = ssl.create_default_context()
    ca.configure_trust(trusting)

    with FakeTwitchServer(channels=[CHANNEL], accepted_token=TOKEN, ssl_context=server_context) as tls_server:
        options = dict(channel=CHANNEL, nick=BOT, token=TOKEN, host="127.0.0.1", port=tls_server.port, handshake_timeout=2)
        with TwitchChat(**options, ssl_context=trusting) as chat:
            chat.connect()
            tls_server.say("alice", "over TLS")
            assert chat.read_message().text == "over TLS"

        # The default context does not know our test CA, so it must refuse the server.
        with TwitchChat(**options) as untrusted, pytest.raises(ssl.SSLCertVerificationError):
            untrusted.connect()


def test_token_never_reaches_the_debug_log(connect, caplog):
    caplog.set_level(logging.DEBUG, logger="twitch_chat")

    connect()

    assert "> PASS ***" in caplog.text
    assert TOKEN.removeprefix("oauth:") not in caplog.text
    assert "< :tmi.twitch.tv 001 echo_bot :Welcome, GLHF!" in caplog.text


# ----- STEP 5 to 7: reading ------------------------------------------------------------


def test_message_with_colons_and_tags(server, connect):
    # Regression: the 2019 code returned only "look at this".
    chat = connect()
    server.say("alice", "look at this: https://example.com", display_name="Alice")

    message = chat.read_message()

    assert (message.channel, message.user, message.display_name) == ("demo", "alice", "Alice")
    assert message.text == "look at this: https://example.com"
    assert message.tags["user-id"]


def test_two_messages_in_one_packet(server, connect):
    # Regression: the 2019 code returned the first and silently dropped the second.
    chat = connect()
    server.send_raw(f"{ALICE}first\r\n{ALICE}second\r\n".encode())

    assert [chat.read_message().text, chat.read_message().text] == ["first", "second"]


def test_line_and_character_split_across_packets(server, connect):
    # Regression: the 2019 code crashed (UnicodeDecodeError, then UnboundLocalError).
    chat = connect()
    data = f"{ALICE}你好，世界\r\n".encode()
    cut = data.index("你".encode()) + 1  # inside the 3 bytes of 你
    reader = Background(chat.read_message)  # already waiting in recv() when the first half arrives
    time.sleep(0.1)
    server.send_raw(data[:cut])
    time.sleep(0.1)
    server.send_raw(data[cut:])

    assert reader.join().text == "你好，世界"


def test_ping_gets_the_same_text_back_and_is_not_a_chat_message(server, connect):
    # Regression: the 2019 code returned (None, None) for PING, and main.py crashed on it.
    chat = connect()
    server.ping("custom-ping-text")
    server.say("alice", "after the ping")

    assert chat.read_message().text == "after the ping"
    assert server.wait_for(lambda line: line.startswith("PONG")) == "PONG :custom-ping-text"


def test_ping_and_chat_in_the_same_packet(server, connect):
    # Regression: the 2019 code reported user "tmi.twitch.tv\r\n" and echoed IRC protocol text.
    chat = connect()
    server.send_raw(f"PING :tmi.twitch.tv\r\n{ALICE}hello\r\n".encode())

    message = chat.read_message()

    assert (message.user, message.text) == ("alice", "hello")
    server.wait_for(lambda line: line == "PONG :tmi.twitch.tv")


def test_chat_text_that_says_ping_is_just_text(server, connect):
    # Regression: the 2019 code answered PONG to any packet containing "PING :tmi.twitch.tv".
    chat = connect()
    server.say("alice", "PING :tmi.twitch.tv")

    assert chat.read_message().text == "PING :tmi.twitch.tv"
    assert not [line for line in server.sent_lines() if line.startswith("PONG")]


def test_notices_are_logged(server, connect, caplog):
    chat = connect()
    server.send_line_to_all("@msg-id=msg_duplicate :tmi.twitch.tv NOTICE #demo :Your message is identical to the one you sent.")
    server.say("alice", "next")

    assert chat.read_message().text == "next"
    assert "Twitch notice: Your message is identical" in caplog.text


# ----- STEP 8: sending ------------------------------------------------------------------


def test_send_message(server, connect):
    connect().send_message("hello: chat")

    assert server.wait_for(lambda line: line.startswith("PRIVMSG")) == "PRIVMSG #demo :hello: chat"


def test_line_breaks_cannot_smuggle_in_another_command(server, connect):
    chat = connect()
    chat.send_message("one\r\nJOIN #someone_else\ntwo")
    chat.send_message("done")

    server.wait_for(lambda line: line == "PRIVMSG #demo :done")
    assert "PRIVMSG #demo :one JOIN #someone_else two" in server.sent_lines()
    assert "JOIN #someone_else" not in server.sent_lines()


def test_sending_waits_for_the_rate_limit(server, connect):
    assert DEFAULT_RATE_LIMITS == ((20, 30.0), (1, 1.0))  # Twitch's numbers for a normal bot
    chat = connect(rate_limits=[(1, 0.3)])
    started = time.monotonic()

    chat.send_message("a")
    chat.send_message("b")

    assert time.monotonic() - started >= 0.29
    server.wait_for(lambda line: line == "PRIVMSG #demo :b")


# ----- STEP 9: endings -------------------------------------------------------------------


def test_server_hanging_up_raises_connection_closed(server, connect):
    # Regression: the 2019 code returned (None, None) in a busy loop forever.
    chat = connect()
    server.disconnect_all()

    with pytest.raises(ConnectionClosed, match="closed the connection"):
        chat.read_message()


def test_reconnect_request_raises_connection_closed_and_connect_starts_over(server, connect):
    chat = connect()
    server.request_reconnect()

    with pytest.raises(ConnectionClosed, match="reconnect"):
        chat.read_message()

    chat.connect()
    server.wait_for(lambda line: line == f"JOIN #{CHANNEL}", count=2)
    server.say("alice", "welcome back")
    assert chat.read_message().text == "welcome back"


def test_using_a_closed_chat_says_so(connect):
    chat = connect()
    chat.close()

    with pytest.raises(ConnectionClosed, match="not connected"):
        chat.send_message("hi")
