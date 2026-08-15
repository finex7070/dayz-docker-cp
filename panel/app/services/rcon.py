"""BattlEye RCON client.

The remote console of a DayZ server is not the Source RCON everyone knows: it
is BattlEye's own protocol, and it runs over **UDP**. That has consequences
this module has to carry rather than hide:

* There is no connection. "Connected" means the server accepted a login and has
  heard from us within the last 45 seconds - stay quiet longer and it forgets
  us without saying so. Hence the keepalive thread.
* Datagrams can be lost, duplicated or overtake each other. Every command
  carries a sequence number, and answers are matched against it instead of
  being read in order.
* Long answers (`players` on a full server) arrive split across several
  datagrams that have to be reassembled by index.

One receiver thread reads everything and dispatches it; request threads wait on
an event for their sequence number. The alternative - each caller reading from
the socket itself - would have every thread swallowing the answers meant for
the others, and server messages would only surface when someone happened to be
listening.

Packet layout: ``'B' 'E'`` + CRC32 of the payload (4 bytes, little endian) +
payload. The payload is ``0xFF``, a type byte, and the type's own data.
"""

from __future__ import annotations

import logging
import socket
import struct
import threading
import time
import zlib
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

HEADER = b"BE"
PREFIX = 0xFF

TYPE_LOGIN = 0x00
TYPE_COMMAND = 0x01
TYPE_MESSAGE = 0x02

# BattlEye drops a client that has been silent for 45 s. Sending at less than
# half that leaves room for a lost datagram before the server gives up on us.
KEEPALIVE_SECONDS = 18.0
LOGIN_TIMEOUT = 6.0
COMMAND_TIMEOUT = 10.0
RECEIVE_SIZE = 8192

# Backoff for reconnects. BattlEye needs a while after the server process
# starts before it answers at all, so the first retries are quick and the last
# one is slow enough not to fill the log while a server is down for good.
RETRY_DELAYS = (2.0, 5.0, 10.0, 30.0)

SUPERVISOR_TICK = 2.0


class RconError(RuntimeError):
    """Something went wrong that the operator should read as a sentence."""


@dataclass
class _Pending:
    """One command waiting for its answer, possibly in several parts."""

    done: threading.Event = field(default_factory=threading.Event)
    parts: dict[int, str] = field(default_factory=dict)
    expected: int = 1

    def add(self, index: int, total: int, text: str) -> None:
        self.expected = max(total, 1)
        self.parts[index] = text
        if len(self.parts) >= self.expected:
            self.done.set()

    def text(self) -> str:
        return "".join(self.parts[index] for index in sorted(self.parts))


def encode(payload: bytes) -> bytes:
    """Wrap a payload in the BattlEye envelope."""
    body = bytes([PREFIX]) + payload
    return HEADER + struct.pack("<I", zlib.crc32(body) & 0xFFFFFFFF) + body


def decode(data: bytes) -> tuple[int, bytes]:
    """Unwrap a datagram into (type, data), or raise ValueError.

    The checksum is verified rather than skipped: on UDP a corrupted datagram
    is a normal event, and a mangled sequence byte would silently deliver one
    command's answer to another command's caller.
    """
    if len(data) < 8 or data[:2] != HEADER:
        raise ValueError("not a BattlEye packet")
    body = data[6:]
    if body[0] != PREFIX:
        raise ValueError("bad payload prefix")
    if struct.unpack("<I", data[2:6])[0] != zlib.crc32(body) & 0xFFFFFFFF:
        raise ValueError("checksum mismatch")
    return body[1], body[2:]


class RconService:
    """Keeps one RCON session alive for as long as the server is running.

    The session is not opened on demand: server messages (connects, kicks,
    chat) are only sent to clients that are logged in, so a panel that only
    connected when someone typed a command would miss everything in between -
    which is most of what makes the console worth watching.
    """

    def __init__(self, host: str, port: int, store, is_up, on_message) -> None:
        self._host = host
        self._port = port
        self._store = store
        self._is_up = is_up
        self._on_message = on_message

        self._lock = threading.RLock()
        # Held for the length of a login attempt. The supervisor and a request
        # thread can both decide to connect at the same moment; without this,
        # the second one would open a socket the first is already logging in
        # on and the answers would go to whichever won the race.
        self._connect_lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._seq = 0
        self._pending: dict[int, _Pending] = {}
        self._login_ok = threading.Event()
        self._login_failed = threading.Event()

        self._state = "idle"
        self._error = ""
        self._since: float | None = None
        self._last_attempt = 0.0
        self._session_password = ""
        # A rejected password is not retried until it changes: a wrong password
        # would otherwise produce a login attempt every few seconds forever,
        # and BattlEye bans an address after enough of them.
        self._rejected_password: str | None = None
        self._attempts = 0
        self._last_sent = 0.0

        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        thread = threading.Thread(target=self._supervise, name="rcon-supervisor", daemon=True)
        self._threads.append(thread)
        thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        self._disconnect("panel shutting down")

    @property
    def configured(self) -> bool:
        return bool(self._password())

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._state == "connected"

    def status(self) -> dict:
        with self._lock:
            return {
                "state": self._state,
                "connected": self._state == "connected",
                "configured": bool(self._password()),
                "error": self._error,
                "port": self._port,
                "uptime_seconds": int(time.monotonic() - self._since) if self._since else 0,
            }

    # --- commands ----------------------------------------------------------

    def send(self, command: str, timeout: float = COMMAND_TIMEOUT) -> str:
        """Run one command and return the server's answer.

        An empty answer is normal - most commands acknowledge by doing the
        thing and saying nothing.
        """
        command = command.strip()
        if not command:
            raise RconError("No command given.")

        self._require_session()

        with self._lock:
            sock = self._sock
            if sock is None:
                raise RconError("Not connected to RCON.")
            seq = self._seq
            self._seq = (self._seq + 1) % 256
            pending = _Pending()
            self._pending[seq] = pending

        try:
            self._transmit(bytes([TYPE_COMMAND, seq]) + command.encode("ascii", "replace"))
            if not pending.done.wait(timeout):
                raise RconError(
                    f"No answer from RCON within {timeout:.0f}s - the command may "
                    "still have run."
                )
            return pending.text()
        finally:
            with self._lock:
                self._pending.pop(seq, None)

    def _require_session(self) -> None:
        """Explain why a command cannot be sent, in the operator's terms."""
        if not self._password():
            raise RconError(
                "No RCON password is set. Add one under Settings - General - "
                "BattlEye and restart the server."
            )
        if not self._is_up():
            raise RconError("The server is not running.")
        if self.connected:
            return

        # The supervisor reconnects on its own, but a click should not have to
        # wait for the next tick to find that out.
        self._connect()
        if not self.connected:
            with self._lock:
                reason = self._error or "not connected yet"
            raise RconError(f"RCON is not available: {reason}")

    # --- connection --------------------------------------------------------

    def _password(self) -> str:
        return self._store.current.rcon_password or ""

    def _supervise(self) -> None:
        """Open the session when the server is up, close it when it is not."""
        while not self._stop.wait(SUPERVISOR_TICK):
            try:
                self._tick()
            except Exception:  # noqa: BLE001 - a dead supervisor is worse
                log.exception("RCON supervisor")

    def _tick(self) -> None:
        password = self._password()
        wanted = bool(password) and self._is_up()

        if not wanted:
            if self._state != "idle":
                self._disconnect("the server is not running" if password
                                 else "no RCON password is set")
            self._rejected_password = None
            self._attempts = 0
            return

        # A changed password does not invalidate a live session: the running
        # server still uses the password from its own start, so the session we
        # have is the one that works until it restarts.
        if self._rejected_password is not None and password != self._rejected_password:
            self._rejected_password = None
            self._attempts = 0

        if self.connected:
            self._keepalive()
            return

        if self._rejected_password == password:
            return

        delay = RETRY_DELAYS[min(self._attempts, len(RETRY_DELAYS) - 1)]
        if time.monotonic() - self._last_attempt < delay:
            return
        self._connect()

    def _connect(self) -> None:
        with self._connect_lock:
            self._connect_once()

    def _connect_once(self) -> None:
        password = self._password()
        with self._lock:
            if self._state == "connected" or not password:
                return
            if self._rejected_password == password:
                return
            self._state = "connecting"
            self._last_attempt = time.monotonic()
            self._attempts += 1

        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(1.0)
            # connect() on UDP only fixes the peer, which is what we want: it
            # filters datagrams from anywhere else and lets send() be used.
            sock.connect((self._host, self._port))

            with self._lock:
                self._sock = sock
                self._session_password = password
                self._pending.clear()
                self._login_ok.clear()
                self._login_failed.clear()

            receiver = threading.Thread(target=self._receive, args=(sock,),
                                        name="rcon-receiver", daemon=True)
            receiver.start()

            self._transmit(bytes([TYPE_LOGIN]) + password.encode("ascii", "replace"))
            if not self._await_login():
                return
        except OSError as exc:
            self._fail(f"{exc}", sock)
            return

        with self._lock:
            self._state = "connected"
            self._error = ""
            self._since = time.monotonic()
            self._attempts = 0
        log.info("RCON connected on port %s", self._port)
        self._note("[panel] RCON connected.")

    def _await_login(self) -> bool:
        deadline = time.monotonic() + LOGIN_TIMEOUT
        while time.monotonic() < deadline:
            if self._login_ok.wait(0.2):
                return True
            if self._login_failed.is_set():
                with self._lock:
                    self._rejected_password = self._session_password
                self._fail(
                    "the server rejected the RCON password - it uses the "
                    "password from its last start"
                )
                self._note("[panel] RCON login rejected: wrong password.")
                return False
        self._fail("no answer on the RCON port")
        return False

    def _fail(self, reason: str, sock: socket.socket | None = None) -> None:
        with self._lock:
            self._state = "error"
            self._error = reason
            target = sock or self._sock
            self._sock = None
            self._since = None
        _close(target)
        log.info("RCON not available: %s", reason)

    def _disconnect(self, reason: str) -> None:
        with self._lock:
            sock, was = self._sock, self._state
            self._sock = None
            self._state = "idle"
            self._error = ""
            self._since = None
            self._pending.clear()
        _close(sock)
        if was == "connected":
            log.info("RCON disconnected: %s", reason)
            self._note(f"[panel] RCON disconnected: {reason}.")

    # --- wire --------------------------------------------------------------

    def _transmit(self, payload: bytes) -> None:
        with self._lock:
            sock = self._sock
            if sock is None:
                raise RconError("Not connected to RCON.")
            self._last_sent = time.monotonic()
        try:
            sock.send(encode(payload))
        except OSError as exc:
            raise RconError(f"Could not reach RCON: {exc}") from exc

    def _keepalive(self) -> None:
        """An empty command packet, purely so the server keeps us on its list."""
        with self._lock:
            due = time.monotonic() - self._last_sent >= KEEPALIVE_SECONDS
            if not due:
                return
            seq = self._seq
            self._seq = (self._seq + 1) % 256
            pending = _Pending()
            self._pending[seq] = pending

        try:
            self._transmit(bytes([TYPE_COMMAND, seq]))
        except RconError:
            self._disconnect("send failed")
            return

        # No answer means the server forgot us (or is gone). Drop the session so
        # the next tick builds a new one instead of talking into the void.
        if not pending.done.wait(LOGIN_TIMEOUT):
            self._disconnect("no answer to the keepalive")
        with self._lock:
            self._pending.pop(seq, None)

    def _receive(self, sock: socket.socket) -> None:
        while not self._stop.is_set():
            with self._lock:
                if self._sock is not sock:
                    return
            try:
                data = sock.recv(RECEIVE_SIZE)
            except socket.timeout:
                continue
            except OSError:
                return

            try:
                kind, body = decode(data)
            except (ValueError, IndexError) as exc:
                log.debug("Dropped an RCON datagram: %s", exc)
                continue
            self._dispatch(kind, body)

    def _dispatch(self, kind: int, body: bytes) -> None:
        if kind == TYPE_LOGIN:
            (self._login_ok if body[:1] == b"\x01" else self._login_failed).set()
            return

        if kind == TYPE_COMMAND and body:
            seq, rest = body[0], body[1:]
            index, total, text = _part(rest)
            with self._lock:
                pending = self._pending.get(seq)
            if pending is not None:
                pending.add(index, total, text)
            return

        if kind == TYPE_MESSAGE and body:
            seq, text = body[0], _text(body[1:])
            # The acknowledgement is not optional: without it the server keeps
            # resending the same message until it gives up on the client.
            try:
                self._transmit(bytes([TYPE_MESSAGE, seq]))
            except RconError:
                pass
            for line in text.splitlines():
                if line.strip():
                    self._note(f"[rcon] {line}")

    def _note(self, line: str) -> None:
        try:
            self._on_message(line)
        except Exception:  # noqa: BLE001 - the console is not worth a dead thread
            log.exception("RCON message sink")


def _part(rest: bytes) -> tuple[int, int, str]:
    """Split a command answer into (index, total, text).

    A multipart answer starts with a 0x00 marker followed by the number of
    parts and this part's index. Answers are plain text, so a leading 0x00
    cannot be anything else.
    """
    if len(rest) >= 3 and rest[0] == 0x00:
        return rest[2], rest[1], _text(rest[3:])
    return 0, 1, _text(rest)


def _text(data: bytes) -> str:
    return data.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")


def _close(sock: socket.socket | None) -> None:
    if sock is None:
        return
    try:
        sock.close()
    except OSError:
        pass
