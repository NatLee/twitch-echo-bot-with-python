"""STEP 5 and STEP 6: turning bytes into lines, and lines into their parts."""

import pytest

from twitch_chat import ChatMessage, LineBuffer, parse_line, unescape_tag_value

# Modelled on the PRIVMSG example in https://dev.twitch.tv/docs/chat/irc/ (checked 2026-11-04)
TWITCH_DOCS_EXAMPLE = (
    "@badge-info=;badges=turbo/1;color=#0D4200;display-name=ronni;emotes=25:0-4,12-16/1902:6-10;"
    "id=b34ccfc7-4977-403a-8a94-33c6bac34fb8;mod=0;room-id=1337;subscriber=0;tmi-sent-ts=1507246572675;"
    "turbo=1;user-id=1337;user-type=global_mod :ronni!ronni@ronni.tmi.twitch.tv PRIVMSG #ronni :Kappa Keepo Kappa"
)


def test_twitch_docs_example_splits_into_tags_prefix_command_params():
    message = parse_line(TWITCH_DOCS_EXAMPLE)

    assert message.command == "PRIVMSG"
    assert message.prefix == "ronni!ronni@ronni.tmi.twitch.tv"
    assert message.nick == "ronni"
    assert message.params == ["#ronni", "Kappa Keepo Kappa"]
    assert message.tags["display-name"] == "ronni"
    assert message.tags["emotes"] == "25:0-4,12-16/1902:6-10"
    assert message.tags["badge-info"] == ""
    assert len(message.tags) == 13


def test_chat_text_with_colons_stays_whole():
    # Regression: the 2019 code used split(":") and kept only "look at this".
    message = parse_line(":alice!alice@alice.tmi.twitch.tv PRIVMSG #demo :look at this: https://example.com :)")

    assert message.trailing == "look at this: https://example.com :)"


def test_text_that_starts_with_a_colon():
    assert parse_line(":a!a@a.tmi.twitch.tv PRIVMSG #demo ::)").trailing == ":)"


def test_ping_has_no_prefix():
    message = parse_line("PING :tmi.twitch.tv")

    assert (message.prefix, message.command, message.params) == (None, "PING", ["tmi.twitch.tv"])


def test_numeric_reply():
    message = parse_line(":tmi.twitch.tv 001 echo_bot :Welcome, GLHF!")

    assert message.command == "001"
    assert message.params == ["echo_bot", "Welcome, GLHF!"]


def test_line_without_trailing_parameter():
    assert parse_line(":echo_bot!echo_bot@echo_bot.tmi.twitch.tv JOIN #demo").params == ["#demo"]


def test_empty_trailing_parameter_is_kept():
    assert parse_line("PRIVMSG #demo :").params == ["#demo", ""]


def test_cap_nak():
    assert parse_line(":tmi.twitch.tv CAP * NAK :twitch.tv/foo").params == ["*", "NAK", "twitch.tv/foo"]


def test_tag_without_value():
    assert parse_line("@flag;key=1 PING :x").tags == {"flag": "", "key": "1"}


@pytest.mark.parametrize(
    ("escaped", "plain"),
    [
        (r"Hello\sthere", "Hello there"),
        (r"a\:b", "a;b"),
        (r"back\\slash", "back\\slash"),
        (r"line\rbreak\n", "line\rbreak\n"),
        (r"\b", "b"),  # unknown escape: drop the backslash
        ("test\\", "test"),  # lone backslash at the end: drop it
        ("", ""),
    ],
)
def test_tag_values_are_unescaped_like_ircv3_says(escaped, plain):
    assert unescape_tag_value(escaped) == plain


def test_display_name_comes_from_tags_and_falls_back_to_login():
    tagged = ChatMessage.from_irc(parse_line(r"@display-name=Alice\sW :alice!alice@alice.tmi.twitch.tv PRIVMSG #demo :hi"))
    untagged = ChatMessage.from_irc(parse_line(":alice!alice@alice.tmi.twitch.tv PRIVMSG #demo :hi"))

    assert (tagged.channel, tagged.user, tagged.text, tagged.display_name) == ("demo", "alice", "hi", "Alice W")
    assert untagged.display_name == "alice"


# ----- LineBuffer -------------------------------------------------------------------


def test_two_lines_in_one_chunk():
    # Regression: the 2019 code treated one recv() as one line and lost the second.
    assert LineBuffer().feed(b"PING :a\r\nPING :b\r\n") == ["PING :a", "PING :b"]


def test_half_a_line_waits_for_the_rest():
    buffer = LineBuffer()

    assert buffer.feed(b"PRIVMSG #demo :hel") == []
    assert buffer.feed(b"lo\r\nPING") == ["PRIVMSG #demo :hello"]
    assert buffer.feed(b" :x\r\n") == ["PING :x"]


def test_character_cut_between_two_chunks():
    # Regression: decoding each recv() on its own crashed on a split UTF-8 character.
    data = "PRIVMSG #demo :你好\r\n".encode()
    cut = data.index("你".encode()) + 1
    buffer = LineBuffer()

    assert buffer.feed(data[:cut]) == []
    assert buffer.feed(data[cut:]) == ["PRIVMSG #demo :你好"]


def test_bare_newline_and_blank_lines():
    assert LineBuffer().feed(b"PING :a\n\r\n\r\nPING :b\n") == ["PING :a", "PING :b"]


def test_invalid_utf8_does_not_crash():
    assert LineBuffer().feed(b"PRIVMSG #demo :\xff\r\n") == ["PRIVMSG #demo :�"]
