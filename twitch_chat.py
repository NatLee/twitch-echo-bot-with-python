"""Talk to Twitch chat with nothing but a socket.

Twitch chat is an IRC server: you open a connection, send short text commands,
and Twitch sends text lines back. This file does that by hand, so you can see
every step. Read it from top to bottom; each STEP matches a section of the README.

    STEP 1  open a connection (TLS) ............ TwitchChat._open_socket
    STEP 2  ask for Twitch's extra features .... TwitchChat._request_capabilities
    STEP 3  log in with PASS and NICK .......... TwitchChat._log_in
    STEP 4  join a channel ..................... TwitchChat._join
    STEP 5  cut the byte stream into lines ..... LineBuffer
    STEP 6  split a line into its parts ........ parse_line
    STEP 7  answer PING with PONG .............. TwitchChat._next_message
    STEP 8  send a message, not too fast ....... TwitchChat.send_message, RateLimiter
    STEP 9  notice when the connection ends .... ConnectionClosed

Everything Twitch-specific below comes from Twitch's docs, checked on 2026-11-04:
https://dev.twitch.tv/docs/chat/irc/ and https://dev.twitch.tv/docs/chat/#rate-limits

Only the Python standard library is used.
"""

from __future__ import annotations

import logging
import re
import socket
import ssl
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field

log = logging.getLogger("twitch_chat")

# ----- Where Twitch chat lives -------------------------------------------------
TWITCH_IRC_HOST = "irc.chat.twitch.tv"
TWITCH_IRC_TLS_PORT = 6697  # encrypted, use this one
TWITCH_IRC_PLAIN_PORT = 6667  # not encrypted: your token travels in clear text

# Extra features we ask for in STEP 2. "tags" adds details such as display-name
# to each message; Twitch only gives tags to bots that also ask for "commands".
DEFAULT_CAPABILITIES = ("twitch.tv/tags", "twitch.tv/commands")

# How long to wait for Twitch to answer while logging in and joining (seconds).
HANDSHAKE_TIMEOUT = 15.0

# Twitch's limits for a bot that is not the broadcaster, a moderator or a VIP:
# at most 20 messages in any 30 seconds, and 1 message per second in a channel.
# Go over and Twitch ignores the bot's messages for an hour. Each pair is
# (messages, seconds).
DEFAULT_RATE_LIMITS = ((20, 30.0), (1, 1.0))

# STEP 9: seconds to wait before reconnecting after the 1st, 2nd, ... failure in a row.
RECONNECT_DELAYS = (1, 2, 4, 8, 16, 30)

RECV_SIZE = 4096  # bytes to read per recv() call; any size works, see STEP 5
ENCODING = "utf-8"


# ----- What can go wrong --------------------------------------------------------
class TwitchChatError(Exception):
    """Base class for the errors below."""


class AuthenticationError(TwitchChatError):
    """Twitch refused the login (STEP 3). Usually a wrong or expired token."""


class JoinError(TwitchChatError):
    """Twitch did not let us into the channel (STEP 4)."""


class ConnectionClosed(TwitchChatError, ConnectionError):
    """The connection ended, or Twitch asked us to reconnect (STEP 9). Connect again."""


def reconnect_delay(failures: int) -> int:
    """STEP 9: wait longer after each failure in a row, so a broken network is not hammered."""
    return RECONNECT_DELAYS[min(failures, len(RECONNECT_DELAYS) - 1)]


def irc_password(token: str) -> str:
    """The IRC password is the token with "oauth:" in front. Accept it with or without."""
    return token if token.startswith("oauth:") else "oauth:" + token


def default_port(use_tls: bool) -> int:
    return TWITCH_IRC_TLS_PORT if use_tls else TWITCH_IRC_PLAIN_PORT


# ----- The client ------------------------------------------------------------------
class TwitchChat:
    """One bot account in one Twitch channel.

    >>> chat = TwitchChat(channel="your_channel", nick="your_bot", token="oauth:...")
    >>> chat.connect()
    >>> for message in chat.messages():
    ...     chat.send_message(message.text)
    """

    def __init__(
        self,
        channel: str,
        nick: str,
        token: str,
        *,
        host: str = TWITCH_IRC_HOST,
        port: int | None = None,
        use_tls: bool = True,
        capabilities: Iterable[str] = DEFAULT_CAPABILITIES,
        rate_limits: Iterable[tuple[int, float]] = DEFAULT_RATE_LIMITS,
        handshake_timeout: float = HANDSHAKE_TIMEOUT,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        # Twitch wants lowercase names. Channels are written "#name" in IRC,
        # so we accept both "name" and "#name" and store the bare name.
        self.channel = channel.strip().lstrip("#").lower()
        self.nick = nick.strip().lower()
        self._password = irc_password(token)
        self.host = host
        self.port = port or default_port(use_tls)
        self.use_tls = use_tls
        self.capabilities = tuple(capabilities)
        self.handshake_timeout = handshake_timeout
        self._ssl_context = ssl_context
        self._limiter = RateLimiter(rate_limits)
        self._sock: socket.socket | None = None
        self._buffer = LineBuffer()
        self._lines: deque[str] = deque()

    # STEP 1 to 4 ------------------------------------------------------------------
    def connect(self) -> None:
        """Open the connection, log in and join the channel. Returns once we are in.
        Calling it again after ConnectionClosed starts over with a fresh connection."""
        self.close()
        self._buffer = LineBuffer()  # leftovers from an old connection mean nothing now
        self._lines.clear()
        self._sock = self._open_socket()  # STEP 1
        try:
            self._request_capabilities()  # STEP 2
            self._log_in()  # STEP 3
            self._join()  # STEP 4
        except BaseException:
            self.close()  # don't leave a half-open connection behind
            raise
        self._sock.settimeout(None)  # from now on, wait as long as chat is quiet
        log.info("joined #%s as %s", self.channel, self.nick)

    def _open_socket(self) -> socket.socket:
        """STEP 1: a TCP connection, wrapped in TLS so nobody can read the token."""
        log.info("connecting to %s:%s (%s)", self.host, self.port, "TLS" if self.use_tls else "plain text")
        sock = socket.create_connection((self.host, self.port), timeout=self.handshake_timeout)
        if not self.use_tls:
            return sock
        context = self._ssl_context or ssl.create_default_context()
        return context.wrap_socket(sock, server_hostname=self.host)

    def _request_capabilities(self) -> None:
        """STEP 2: CAP REQ asks for Twitch's extras. Twitch answers "CAP * ACK" (yes)
        or "CAP * NAK" (no); we read that answer in STEP 3 together with the login reply.
        """
        if self.capabilities:
            self._send_line("CAP REQ :" + " ".join(self.capabilities))

    def _log_in(self) -> None:
        """STEP 3: PASS (the token) must come before NICK (the bot's login name).

        Success is a burst of numbered lines starting with "001". A wrong token gets
        "NOTICE * :Login authentication failed" and Twitch hangs up.
        """
        self._send_line("PASS " + self._password, secret=True)
        self._send_line("NICK " + self.nick)
        self._wait_for("001", error=AuthenticationError, waiting_for="the login reply")

    def _join(self) -> None:
        """STEP 4: JOIN #channel. Twitch answers with JOIN, a member list (353)
        and "366 ... :End of /NAMES list". For a channel that does not exist Twitch
        answers nothing at all, which is why we wait with a timeout.
        """
        self._send_line(f"JOIN #{self.channel}")
        self._wait_for(
            "366",
            error=JoinError,
            waiting_for=f"the JOIN reply (is #{self.channel} spelled right? Twitch ignores unknown channels)",
        )

    def _wait_for(self, command: str, *, error: type[TwitchChatError], waiting_for: str) -> IrcMessage:
        """Read lines until Twitch sends ``command``. During login and join, a NOTICE
        means Twitch said no, so it becomes ``error`` with Twitch's own words."""
        try:
            while True:
                message = self._next_message()
                if message.command == command:
                    return message
                if message.command == "NOTICE":
                    raise error(f"Twitch said: {message.trailing}")
                if message.command == "CAP" and "NAK" in message.params:
                    log.warning("Twitch refused capabilities: %s", message.trailing)
        except TimeoutError:
            raise error(f"no answer from Twitch within {self.handshake_timeout:g} s while waiting for {waiting_for}") from None

    # STEP 5 to 7: reading ----------------------------------------------------------------
    def messages(self) -> Iterator[ChatMessage]:
        """Chat messages, one after another, forever (until the connection ends)."""
        while True:
            yield self.read_message()

    def read_message(self) -> ChatMessage:
        """Wait for the next chat message. PING and other server lines are handled here."""
        while True:
            message = self._next_message()
            if message.command == "PRIVMSG":
                return ChatMessage.from_irc(message)
            if message.command == "NOTICE":
                # e.g. "Your message was not sent because you are sending messages too quickly."
                log.warning("Twitch notice: %s", message.trailing)

    def _next_message(self) -> IrcMessage:
        """Read one line (STEP 5), parse it (STEP 6) and handle the lines that are about
        the connection itself (STEP 7 and STEP 9)."""
        line = self._next_line()
        log.debug("< %s", line)
        message = parse_line(line)
        if message.command == "PING":
            # STEP 7: Twitch checks that we are still here. Answer with the same text
            # it sent, or Twitch closes the connection.
            self._send_line("PONG :" + message.trailing)
        elif message.command == "RECONNECT":
            # STEP 9: Twitch is about to restart this server; we should come back.
            raise ConnectionClosed("Twitch asked us to reconnect (server maintenance)")
        return message

    def _next_line(self) -> str:
        """STEP 5: one complete line. recv() returns whatever bytes have arrived:
        maybe half a line, maybe five lines. LineBuffer keeps the leftovers."""
        while not self._lines:
            data = self._require_socket().recv(RECV_SIZE)
            if not data:
                # STEP 9: recv() returns b"" only when the other side closed the connection.
                raise ConnectionClosed("Twitch closed the connection")
            self._lines.extend(self._buffer.feed(data))
        return self._lines.popleft()

    # STEP 8: sending ----------------------------------------------------------------------
    def send_message(self, text: str) -> None:
        """Say ``text`` in the channel. Waits first if we would go over the rate limit."""
        # One IRC line is one command, so a line break would end the message and
        # start a new (unwanted) command. Twitch shows messages on one line anyway.
        text = " ".join(text.splitlines())
        waited = self._limiter.wait()
        if waited:
            log.info("waited %.1f s to stay under Twitch's rate limit", waited)
        self._send_line(f"PRIVMSG #{self.channel} :{text}")

    def _send_line(self, line: str, *, secret: bool = False) -> None:
        """Every command goes out through here: text + CRLF, encoded as UTF-8."""
        if "\r" in line or "\n" in line:
            raise ValueError("an IRC line cannot contain CR or LF")
        log.debug("> %s", line.split(" ", 1)[0] + " ***" if secret else line)
        self._require_socket().sendall((line + "\r\n").encode(ENCODING))  # sendall: send() may send only part

    # Housekeeping ---------------------------------------------------------------------------
    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def __enter__(self) -> TwitchChat:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _require_socket(self) -> socket.socket:
        if self._sock is None:
            raise ConnectionClosed("not connected; call connect() first")
        return self._sock


# ----- STEP 5: from a byte stream to lines --------------------------------------------
class LineBuffer:
    """TCP delivers a stream of bytes, not messages. Twitch ends every line with
    CRLF ("\\r\\n"), so we collect bytes and hand out only complete lines.

    We split the *bytes* and decode each finished line. Decoding chunk by chunk
    would break when a character such as "你" (3 bytes in UTF-8) is cut in two.
    """

    def __init__(self) -> None:
        self._pending = b""

    def feed(self, data: bytes) -> list[str]:
        *lines, self._pending = (self._pending + data).split(b"\n")
        # rstrip("\r") also accepts a bare "\n"; errors="replace" keeps one bad byte from killing the bot
        return [line.rstrip(b"\r").decode(ENCODING, errors="replace") for line in lines if line.strip()]


# ----- STEP 6: the parts of one line --------------------------------------------------
@dataclass(frozen=True)
class IrcMessage:
    """One IRC line, split into its four parts. See the picture in the README.

    @badge-info=;display-name=Alice;... :alice!alice@alice.tmi.twitch.tv PRIVMSG #demo :hi: there
    |------------- tags --------------| |------------ prefix ------------| |command| |params----|
    """

    tags: dict[str, str]
    prefix: str | None  # who sent it: "nick!user@host", or a server name
    command: str  # PRIVMSG, PING, JOIN, NOTICE, or a number such as 001
    params: list[str] = field(default_factory=list)

    @property
    def trailing(self) -> str:
        """The last parameter, which is the only one that may contain spaces."""
        return self.params[-1] if self.params else ""

    @property
    def nick(self) -> str | None:
        """The sender's login name: the part of the prefix before "!"."""
        return self.prefix.split("!", 1)[0] if self.prefix else None


def parse_line(line: str) -> IrcMessage:
    """Split one IRC line (without its CRLF) into tags, prefix, command and params.

    Never use line.split(":") for this: the chat text itself may contain ":".
    The rule is: the first " :" starts the last parameter, and everything after it,
    colons and spaces included, belongs to that parameter.
    """
    rest = line
    tags: dict[str, str] = {}
    if rest.startswith("@"):  # IRCv3 tags: "@key=value;key2=value2 "
        raw_tags, _, rest = rest[1:].partition(" ")
        tags = parse_tags(raw_tags)
    prefix = None
    if rest.startswith(":"):  # prefix: ":nick!user@host "
        prefix, _, rest = rest[1:].partition(" ")
    middle, has_trailing, trailing = rest.partition(" :")
    command, *params = middle.split()
    if has_trailing:
        params.append(trailing)
    return IrcMessage(tags=tags, prefix=prefix, command=command.upper(), params=params)


def parse_tags(raw: str) -> dict[str, str]:
    """"badges=;display-name=Alice" -> {"badges": "", "display-name": "Alice"}"""
    tags = {}
    for item in filter(None, raw.split(";")):
        key, _, value = item.partition("=")
        tags[key] = unescape_tag_value(value)
    return tags


# Tag values cannot contain ";" or spaces, so IRCv3 writes them as escapes.
# https://ircv3.net/specs/extensions/message-tags#escaping-values
_TAG_UNESCAPES = {":": ";", "s": " ", "\\": "\\", "r": "\r", "n": "\n"}


def unescape_tag_value(value: str) -> str:
    r""""Hello\sthere" -> "Hello there". An unknown escape "\x" becomes "x", and a
    lone "\" at the very end is dropped, as the spec says."""
    return re.sub(r"\\(.?)", lambda match: _TAG_UNESCAPES.get(match[1], match[1]), value)


@dataclass(frozen=True)
class ChatMessage:
    """A chat message, the part of a PRIVMSG line a bot usually cares about."""

    channel: str  # without "#"
    user: str  # login name, lowercase
    text: str
    tags: dict[str, str] = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        """How the name looks in chat (may have capitals or CJK); the login if not set."""
        return self.tags.get("display-name") or self.user

    @classmethod
    def from_irc(cls, message: IrcMessage) -> ChatMessage:
        # PRIVMSG params are [#channel, text]
        return cls(
            channel=message.params[0].lstrip("#"),
            user=message.nick or "",
            text=message.trailing,
            tags=message.tags,
        )


# ----- STEP 8: staying under the rate limit ---------------------------------------------
class RateLimiter:
    """Remembers when we sent recently, and waits when one more message would break
    a rule such as "20 messages in any 30 seconds".

    ``rules`` is a list of (messages, seconds) pairs; all of them must hold.
    ``clock`` and ``sleep`` can be swapped out in tests.
    """

    def __init__(
        self,
        rules: Iterable[tuple[int, float]],
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.rules = tuple(rules)
        self._clock = clock
        self._sleep = sleep
        self._sent: deque[float] = deque(maxlen=max((limit for limit, _ in self.rules), default=0))

    def wait(self) -> float:
        """Block until sending is allowed, record the send, and return the seconds waited."""
        now = self._clock()
        # For "N messages per T seconds": the N-th most recent send must be at least T seconds ago.
        ready_at = max(
            (self._sent[-limit] + period for limit, period in self.rules if len(self._sent) >= limit),
            default=now,
        )
        delay = max(0.0, ready_at - now)
        if delay:
            self._sleep(delay)
        self._sent.append(now + delay)
        return delay
