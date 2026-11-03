"""config.py and main.py: settings, the echo loop, reconnecting, and the real command line."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import main
from config import REQUIRED, ConfigError, Settings, load_env_file
from tests.conftest import BOT, CHANNEL, TOKEN, Background, StopBot
from twitch_chat import TWITCH_IRC_HOST

REPO = Path(__file__).resolve().parents[1]
GOOD_ENV = {"TWITCH_CHANNEL": CHANNEL, "TWITCH_BOT_NAME": BOT, "TWITCH_OAUTH_TOKEN": TOKEN}


def fake_server_env(server) -> dict[str, str]:
    return {**GOOD_ENV, "TWITCH_IRC_HOST": server.host, "TWITCH_IRC_PORT": str(server.port), "TWITCH_IRC_TLS": "false"}


# ----- config.py ----------------------------------------------------------------------


def test_missing_settings_are_all_named():
    with pytest.raises(ConfigError) as error:
        Settings.from_env({"TWITCH_CHANNEL": "  "})

    for name in REQUIRED:
        assert name in str(error.value)


def test_settings_default_to_real_twitch_over_tls():
    settings = Settings.from_env(GOOD_ENV)

    assert (settings.host, settings.port, settings.use_tls) == (TWITCH_IRC_HOST, None, True)


@pytest.mark.parametrize("word", ["false", "0", "No", "OFF"])
def test_settings_for_the_practice_server(word):
    settings = Settings.from_env({**GOOD_ENV, "TWITCH_IRC_HOST": "127.0.0.1", "TWITCH_IRC_PORT": "6667", "TWITCH_IRC_TLS": word})

    assert (settings.host, settings.port, settings.use_tls) == ("127.0.0.1", 6667, False)


def test_bad_port_is_a_config_error():
    with pytest.raises(ConfigError, match="TWITCH_IRC_PORT"):
        Settings.from_env({**GOOD_ENV, "TWITCH_IRC_PORT": "six"})


def test_env_file_is_read_without_overwriting(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# a comment\n\nTWITCH_CHANNEL = demo\nTWITCH_BOT_NAME='echo_bot'\n"
        "TWITCH_OAUTH_TOKEN=\"oauth:from-file\"\nnot a setting\n",
        encoding="utf-8",
    )
    environ = {"TWITCH_OAUTH_TOKEN": "oauth:from-shell"}

    load_env_file(env_file, environ)

    assert environ == {"TWITCH_CHANNEL": "demo", "TWITCH_BOT_NAME": "echo_bot", "TWITCH_OAUTH_TOKEN": "oauth:from-shell"}


def test_missing_env_file_is_fine(tmp_path):
    environ: dict[str, str] = {}
    load_env_file(tmp_path / "nope.env", environ)
    assert environ == {}


def test_env_example_lists_every_setting():
    example = (REPO / ".env.example").read_text(encoding="utf-8")

    for name in [*REQUIRED, "TWITCH_IRC_HOST", "TWITCH_IRC_PORT", "TWITCH_IRC_TLS"]:
        assert name in example


# ----- main.py ------------------------------------------------------------------------


def test_run_echoes_and_reconnects(server, capsys):
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise StopBot

    bot = Background(lambda: main.run(Settings.from_env(fake_server_env(server)), sleep=fake_sleep))

    server.wait_for_join()
    server.say("alice", "hello: bot", display_name="Alice")
    server.wait_for(lambda line: line == "PRIVMSG #demo :hello: bot")

    server.request_reconnect()  # Twitch's "please come back" -> the bot reconnects after 1 s
    server.wait_for_join(count=2)
    server.say("bob", "again")
    server.wait_for(lambda line: line == "PRIVMSG #demo :again")

    server.disconnect_all()  # second loss: the fake sleep ends the test
    with pytest.raises(StopBot):
        bot.join()
    assert sleeps == [1, 1]  # the failure count starts over after each good connection
    out = capsys.readouterr().out
    assert "#demo Alice: hello: bot\n#demo echo_bot (bot): hello: bot\n" in out


def test_main_stops_on_a_wrong_token(server, monkeypatch, capsys):
    monkeypatch.setattr(main, "load_env_file", lambda: None)
    for name, value in {**fake_server_env(server), "TWITCH_OAUTH_TOKEN": "oauth:wrong"}.items():
        monkeypatch.setenv(name, value)

    assert main.main([]) == 1
    assert "Login authentication failed" in capsys.readouterr().err


def test_main_explains_missing_settings(monkeypatch, capsys):
    monkeypatch.setattr(main, "load_env_file", lambda: None)
    for name in REQUIRED:
        monkeypatch.delenv(name, raising=False)

    assert main.main([]) == 2
    assert ".env.example" in capsys.readouterr().err


def test_python_main_py_end_to_end(server):
    """The command from the README, as a real process."""
    env = {**os.environ, **fake_server_env(server), "PYTHONUNBUFFERED": "1"}
    with subprocess.Popen([sys.executable, "main.py", "--debug"], cwd=REPO, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as bot:
        try:
            server.wait_for_join(timeout=10)
            server.say("alice", "end to end")
            server.wait_for(lambda line: line == "PRIVMSG #demo :end to end")
        finally:
            bot.terminate()
        output = bot.communicate(timeout=10)[0]

    assert "> PASS ***" in output
    assert TOKEN.removeprefix("oauth:") not in output
    assert "#demo alice: end to end" in output
