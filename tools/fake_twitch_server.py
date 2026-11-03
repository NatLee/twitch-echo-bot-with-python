"""A pretend Twitch chat server you can run on your own computer.

It speaks just enough of Twitch's IRC dialect for this repo: CAP REQ, PASS, NICK,
JOIN, PRIVMSG, PING/PONG and RECONNECT, with the same reply lines that Twitch's
docs show (https://dev.twitch.tv/docs/chat/irc/). Two uses:

* Practice without a Twitch account. In one terminal:

      python tools/fake_twitch_server.py

  In another one, point the bot at it (the server prints the exact command):

      TWITCH_IRC_HOST=127.0.0.1 TWITCH_IRC_PORT=6667 TWITCH_IRC_TLS=false python main.py

  Then type ``alice: hello`` in the server window to chat as a viewer called alice.
  ``/ping`` sends a PING, ``/reconnect`` asks the bot to reconnect, ``/quit`` stops.

* Tests. ``FakeTwitchServer`` is a context manager; tests start it on a free port,
  make it say things (``say``, ``ping``, ``send_raw``) and check what the bot sent
  (``wait_for``, ``sent_lines``).

This is not Twitch. It does not check tokens against anything real, has no rate
limits and knows only the channels you give it. It is deliberately written without
importing the bot's own code, so a parser bug in the bot cannot hide behind the same
bug in the server.
"""

from __future__ import annotations

import argparse
import itertools
import socket
import ssl
import sys
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

SERVER_NAME = "tmi.twitch.tv"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 6667  # Twitch's plain-text IRC port; the practice server has no TLS
DEFAULT_CHANNELS = ("demo",)
ACCEPT_POLL = 0.1  # seconds between checks whether stop() was called
KNOWN_CAPABILITIES = ("twitch.tv/tags", "twitch.tv/commands", "twitch.tv/membership")

# Exact texts from Twitch's docs, so the bot sees what it would see on Twitch.
AUTH_FAILED = "Login authentication failed"
CHANNEL_SUSPENDED = "This channel does not exist or has been suspended."
BAD_AUTH_ORDER = "Improperly formatted auth"
WELCOME_LINES = (
    ("001", "Welcome, GLHF!"),
    ("002", "Your host is tmi.twitch.tv"),
    ("003", "This server is rather new"),
    ("004", "-"),
    ("375", "-"),
    ("372", "You are in a maze of twisty passages, all alike."),
    ("376", ">"),
)

_TAG_ESCAPES = {";": r"\:", " ": r"\s", "\\": "\\\\", "\r": r"\r", "\n": r"\n"}


def escape_tag_value(value: str) -> str:
    """Escape a tag value the way IRCv3 says (https://ircv3.net/specs/extensions/message-tags)."""
    return "".join(_TAG_ESCAPES.get(char, char) for char in value)


def format_tags(tags: dict[str, str]) -> str:
    return "@" + ";".join(f"{key}={escape_tag_value(value)}" for key, value in tags.items())


@dataclass
class _Client:
    """One connected bot."""

    number: int
    sock: socket.socket
    caps: set[str] = field(default_factory=set)
    password: str | None = None
    nick: str | None = None
    channels: set[str] = field(default_factory=set)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def send(self, data: bytes) -> None:
        with self.lock:
            try:
                self.sock.sendall(data)
            except OSError:
                pass  # the bot already hung up; nothing to deliver

    def send_line(self, line: str) -> None:
        self.send((line + "\r\n").encode("utf-8"))

    def close(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


class FakeTwitchServer:
    """A threaded IRC server that behaves like Twitch chat for the parts this repo uses."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = 0,
        *,
        channels: Iterable[str] = DEFAULT_CHANNELS,
        suspended_channels: Iterable[str] = (),
        accepted_token: str | None = None,
        ssl_context: ssl.SSLContext | None = None,
        on_event: Callable[[str], None] | None = None,
    ) -> None:
        names = [name.lstrip("#").lower() for name in channels]
        self.channels = set(names)
        self.default_channel = names[0]  # where say() and the console send chat by default
        self.suspended_channels = {name.lstrip("#").lower() for name in suspended_channels}
        self.accepted_token = accepted_token  # None: any "oauth:..." password logs in
        self.ssl_context = ssl_context
        self.on_event = on_event or (lambda text: None)
        self._listener = socket.create_server((host, port))
        self.address = self._listener.getsockname()[:2]
        self._clients: list[_Client] = []
        self._sent: list[tuple[int, str]] = []  # (client number, line the bot sent)
        self._joins: dict[str, int] = {}  # channel -> how many successful JOINs so far
        self._changed = threading.Condition()
        self._numbers = itertools.count(1)
        self._message_ids = itertools.count(1)
        self._running = False
        self._threads: list[threading.Thread] = []

    # ----- start / stop ---------------------------------------------------------

    def start(self) -> FakeTwitchServer:
        self._running = True
        self._spawn(self._accept_loop)
        return self

    def stop(self) -> None:
        self._running = False
        self._listener.close()
        self.disconnect_all()
        for thread in self._threads:
            thread.join(timeout=2)

    def __enter__(self) -> FakeTwitchServer:
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    @property
    def host(self) -> str:
        return self.address[0]

    @property
    def port(self) -> int:
        return self.address[1]

    # ----- things a test (or the console) can make the server do ---------------

    def say(self, user: str, text: str, *, channel: str | None = None, display_name: str | None = None) -> None:
        """Send a chat message from viewer ``user`` to every bot in ``channel``."""
        channel = (channel or self.default_channel).lower()
        login = user.lower()
        tags = {
            "badge-info": "",
            "badges": "",
            "color": "#1E90FF",
            "display-name": display_name or user,
            "emotes": "",
            "first-msg": "0",
            "flags": "",
            "id": f"00000000-0000-0000-0000-{next(self._message_ids):012d}",
            "mod": "0",
            "room-id": "1000",
            "subscriber": "0",
            "tmi-sent-ts": str(int(time.time() * 1000)),
            "turbo": "0",
            "user-id": str(2000 + sum(map(ord, login))),
            "user-type": "",
        }
        line = f":{login}!{login}@{login}.{SERVER_NAME} PRIVMSG #{channel} :{text}"
        for client in self._clients_in(channel):
            client.send_line(self._with_tags(client, tags, line))

    def ping(self, text: str = SERVER_NAME) -> None:
        self.send_line_to_all(f"PING :{text}")

    def request_reconnect(self) -> None:
        """Twitch sends RECONNECT before maintenance. Only bots with twitch.tv/commands get it."""
        for client in self._snapshot_clients():
            if "twitch.tv/commands" in client.caps:
                client.send_line(f":{SERVER_NAME} RECONNECT")

    def send_line_to_all(self, line: str) -> None:
        for client in self._snapshot_clients():
            client.send_line(line)

    def send_raw(self, data: bytes) -> None:
        """Send exact bytes, e.g. two lines in one packet or half a line."""
        for client in self._snapshot_clients():
            client.send(data)

    def disconnect_all(self) -> None:
        for client in self._snapshot_clients():
            client.close()

    # ----- things a test can check ----------------------------------------------

    def sent_lines(self) -> list[str]:
        """Every line the bots sent, oldest first."""
        with self._changed:
            return [line for _, line in self._sent]

    def wait_for(self, predicate: Callable[[str], bool], timeout: float | None = 5.0, count: int = 1) -> str:
        """Wait until bots have sent ``count`` lines that match ``predicate``; return the last of them.
        ``timeout=None`` waits forever."""
        def matches() -> list[str]:
            return [line for _, line in self._sent if predicate(line)]

        with self._changed:
            if not self._changed.wait_for(lambda: len(matches()) >= count, timeout):
                sent = [line for _, line in self._sent]
                raise TimeoutError(f"wanted {count} matching line(s) within {timeout} s; bots sent {sent}")
            return matches()[count - 1]

    def wait_for_join(self, channel: str | None = None, timeout: float | None = 5.0, count: int = 1) -> None:
        """Wait until a bot is in ``channel`` and there have been ``count`` JOINs to it so far.
        ``timeout=None`` waits forever."""
        wanted = (channel or self.default_channel).lower()
        with self._changed:
            joined = self._changed.wait_for(lambda: self._clients_in(wanted) and self._joins.get(wanted, 0) >= count, timeout)
        if not joined:
            raise TimeoutError(f"no bot joined #{wanted} {count} time(s) within {timeout} s")

    # ----- the server itself ----------------------------------------------------

    def _spawn(self, target: Callable[..., None], *args: object) -> None:
        thread = threading.Thread(target=target, args=args, daemon=True)
        thread.start()
        self._threads.append(thread)

    def _accept_loop(self) -> None:
        # On Linux, closing a socket does not wake up accept(); a short timeout lets stop() end this loop.
        self._listener.settimeout(ACCEPT_POLL)
        while self._running:
            try:
                sock, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return  # listener closed by stop()
            sock.settimeout(None)
            self._spawn(self._serve, sock)

    def _serve(self, sock: socket.socket) -> None:
        if self.ssl_context is not None:
            try:
                sock = self.ssl_context.wrap_socket(sock, server_side=True)
            except (OSError, ssl.SSLError):
                sock.close()
                return
        client = _Client(next(self._numbers), sock)
        with self._changed:
            self._clients.append(client)
        try:
            self._read_lines(client)
        finally:
            with self._changed:
                self._clients.remove(client)
                self._changed.notify_all()
            client.close()
            self.on_event(f"bot #{client.number} disconnected")

    def _read_lines(self, client: _Client) -> None:
        buffer = b""
        while True:
            try:
                data = client.sock.recv(4096)
            except OSError:
                return
            if not data:
                return
            buffer += data
            *lines, buffer = buffer.split(b"\n")
            for raw in lines:
                line = raw.rstrip(b"\r").decode("utf-8", errors="replace")
                with self._changed:
                    self._sent.append((client.number, line))
                    self._changed.notify_all()
                if not self._handle(client, line):
                    return

    def _handle(self, client: _Client, line: str) -> bool:
        """React to one line from a bot. Returns False when the connection should end."""
        command, _, rest = line.partition(" ")
        handler = getattr(self, f"_on_{command.lower()}", self._on_unknown)
        return handler(client, rest, command)

    def _on_cap(self, client: _Client, rest: str, command: str) -> bool:
        wanted = rest.removeprefix("REQ").strip().lstrip(":").split()
        if all(cap in KNOWN_CAPABILITIES for cap in wanted):
            client.caps.update(wanted)
            client.send_line(f":{SERVER_NAME} CAP * ACK :{' '.join(wanted)}")
        else:
            client.send_line(f":{SERVER_NAME} CAP * NAK :{' '.join(wanted)}")
        return True

    def _on_pass(self, client: _Client, rest: str, command: str) -> bool:
        client.password = rest.strip()
        return True

    def _on_nick(self, client: _Client, rest: str, command: str) -> bool:
        if client.password is None:
            return self._refuse(client, BAD_AUTH_ORDER)
        token_ok = client.password.startswith("oauth:") and client.password != "oauth:"
        if self.accepted_token is not None:
            token_ok = client.password == self.accepted_token
        if not token_ok:
            return self._refuse(client, AUTH_FAILED)
        client.nick = rest.strip().lower()
        for number, text in WELCOME_LINES:
            client.send_line(f":{SERVER_NAME} {number} {client.nick} :{text}")
        if "twitch.tv/commands" in client.caps:
            tags = {"badge-info": "", "badges": "", "color": "", "display-name": client.nick,
                    "emote-sets": "0", "user-id": "1", "user-type": ""}
            client.send_line(self._with_tags(client, tags, f":{SERVER_NAME} GLOBALUSERSTATE"))
        self.on_event(f"bot #{client.number} logged in as {client.nick}")
        return True

    def _on_user(self, client: _Client, rest: str, command: str) -> bool:
        return True  # IRC libraries send USER; Twitch does not need it

    def _on_join(self, client: _Client, rest: str, command: str) -> bool:
        nick = client.nick
        for name in rest.strip().split(","):
            channel = name.strip().lstrip("#").lower()
            if channel in self.suspended_channels:
                client.send_line(self._with_tags(client, {"msg-id": "msg_channel_suspended"},
                                                 f":{SERVER_NAME} NOTICE #{channel} :{CHANNEL_SUSPENDED}"))
                continue
            if channel not in self.channels:
                continue  # Twitch quietly drops JOINs for channels that do not exist
            with self._changed:  # count the bot in before it hears 366, so chat can reach it right away
                client.channels.add(channel)
                self._joins[channel] = self._joins.get(channel, 0) + 1
                self._changed.notify_all()
            self.on_event(f"bot #{client.number} joined #{channel}")
            client.send_line(f":{nick}!{nick}@{nick}.{SERVER_NAME} JOIN #{channel}")
            client.send_line(f":{nick}.{SERVER_NAME} 353 {nick} = #{channel} :{nick}")
            client.send_line(f":{nick}.{SERVER_NAME} 366 {nick} #{channel} :End of /NAMES list")
            if "twitch.tv/commands" in client.caps:
                client.send_line(self._with_tags(client, self._userstate(nick), f":{SERVER_NAME} USERSTATE #{channel}"))
                roomstate = {"room-id": "1000", "slow": "0"}
                client.send_line(self._with_tags(client, roomstate, f":{SERVER_NAME} ROOMSTATE #{channel}"))
        return True

    def _on_part(self, client: _Client, rest: str, command: str) -> bool:
        channel = rest.strip().lstrip("#").lower()
        client.channels.discard(channel)
        client.send_line(f":{client.nick}!{client.nick}@{client.nick}.{SERVER_NAME} PART #{channel}")
        return True

    def _on_privmsg(self, client: _Client, rest: str, command: str) -> bool:
        target, _, text = rest.partition(" :")
        self.on_event(f"{client.nick} -> {target}: {text}")
        if "twitch.tv/commands" in client.caps:
            # Twitch does not echo the bot's own PRIVMSG back; it sends USERSTATE instead.
            client.send_line(self._with_tags(client, self._userstate(client.nick), f":{SERVER_NAME} USERSTATE {target}"))
        return True

    def _on_pong(self, client: _Client, rest: str, command: str) -> bool:
        self.on_event(f"bot #{client.number} answered: PONG {rest}")
        return True

    def _on_quit(self, client: _Client, rest: str, command: str) -> bool:
        return False

    def _on_unknown(self, client: _Client, rest: str, command: str) -> bool:
        client.send_line(f":{SERVER_NAME} 421 {client.nick or '*'} {command} :Unknown command")
        return True

    def _refuse(self, client: _Client, notice: str) -> bool:
        client.send_line(f":{SERVER_NAME} NOTICE * :{notice}")
        self.on_event(f"bot #{client.number} refused: {notice}")
        return False  # Twitch closes the connection after a failed login

    def _userstate(self, nick: str | None) -> dict[str, str]:
        return {"badge-info": "", "badges": "", "color": "", "display-name": nick or "",
                "emote-sets": "0", "mod": "0", "subscriber": "0", "user-type": ""}

    @staticmethod
    def _with_tags(client: _Client, tags: dict[str, str], line: str) -> str:
        return f"{format_tags(tags)} {line}" if "twitch.tv/tags" in client.caps else line

    def _snapshot_clients(self) -> list[_Client]:
        with self._changed:
            return list(self._clients)

    def _clients_in(self, channel: str) -> list[_Client]:
        return [client for client in self._snapshot_clients() if channel in client.channels]


# ----- console mode -------------------------------------------------------------

CONSOLE_HELP = """\
Type a chat line as  name: message   (example:  alice: hello there)
Commands:            /ping   /reconnect   /quit
Lines you type before a bot joins wait until one does."""


def run_console(server: FakeTwitchServer, lines: Iterable[str], delay: float) -> None:
    """Read lines from the keyboard (or a pipe) and play them into the chat."""
    channel = server.default_channel
    commands = {
        "/ping": ("PING sent", server.ping),
        "/reconnect": ("RECONNECT sent", server.request_reconnect),
    }
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line == "/quit":
            return
        server.wait_for_join(channel, timeout=None)
        if line in commands:
            note, action = commands[line]
            print(f"[server] {note}", flush=True)
            action()
        else:
            user, separator, text = (part.strip() for part in line.partition(":"))
            if not separator or not user:
                print("[server] write it as  name: message", flush=True)
                continue
            print(f"[chat]   {user}: {text}", flush=True)
            server.say(user, text, channel=channel)
        time.sleep(delay)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Pretend Twitch chat server for practice (plain TCP, no TLS).")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"address to listen on (default {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port to listen on (default {DEFAULT_PORT})")
    parser.add_argument("--channel", action="append",
                        help=f"channel that exists (default {DEFAULT_CHANNELS[0]}); repeat for more")
    parser.add_argument("--token", help="only accept this PASS value (default: any oauth:... value)")
    parser.add_argument("--delay", type=float, default=0.3, help="seconds to wait after each typed line (default 0.3)")
    args = parser.parse_args(argv)

    server = FakeTwitchServer(
        args.host, args.port,
        channels=args.channel or DEFAULT_CHANNELS,
        accepted_token=args.token,
        on_event=lambda text: print(f"[server] {text}", flush=True),
    )
    with server:
        host, port = server.address
        channel = server.default_channel
        print(f"[server] fake Twitch chat listening on {host}:{port} (plain TCP, not the real Twitch)", flush=True)
        print("[server] point the bot here, for example with these lines in .env:", flush=True)
        print(f"           TWITCH_IRC_HOST={host}  TWITCH_IRC_PORT={port}  TWITCH_IRC_TLS=false", flush=True)
        print(f"           TWITCH_CHANNEL={channel}  TWITCH_BOT_NAME=any_name  "
              f"TWITCH_OAUTH_TOKEN={args.token or 'oauth:anything'}", flush=True)
        print(CONSOLE_HELP, flush=True)
        try:
            run_console(server, sys.stdin, args.delay)
            if not sys.stdin.isatty():
                # input came from a pipe: keep serving until Ctrl+C so the bot can finish
                threading.Event().wait()
        except KeyboardInterrupt:
            pass
    print("[server] stopped", flush=True)


if __name__ == "__main__":
    main()
