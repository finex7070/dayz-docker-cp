"""BattlEye RCON client.

The remote console of a DayZ server is not the Source RCON everyone knows: it
is BattlEye's own protocol, and it runs over **UDP**. That has consequences
this module has to carry rather than hide:

* There is no connection. "Logged in" means the server accepted a password and
  has heard from us within the last 45 seconds - stay quiet longer and it
  forgets us without saying so.
* Datagrams can be lost, duplicated or overtake each other. Every command
  carries a sequence number, and the answer is matched against it.
* Long answers (`players` on a full server) arrive split across several
  datagrams that have to be reassembled by index.

The session lasts one command: log in, ask, read the answer, close. A session
held open would have to be kept alive every 18 seconds forever, and every lost
keepalive - a normal event on UDP, and a certainty in the two minutes a server
spends loading its mission - reads as a disconnect in the console. What that
bought was a permanent listener for server messages, which the panel does not
depend on: the server's own output goes to the console through the process, not
through RCON.

All of those short sessions speak from **one** source port. BattlEye counts a
client per address and port and keeps it for 45 seconds, so a port per command
becomes ten clients in ten seconds - and it answers the eleventh with silence.

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

LOGIN_TIMEOUT = 4.0
# A lost login datagram would otherwise cost the whole timeout for nothing.
# Only the login is repeated: a command sent twice may well run twice.
LOGIN_RETRY = 1.0
COMMAND_TIMEOUT = 10.0
RECEIVE_SIZE = 8192

# How long a single recv waits before the loop checks its deadline again.
POLL_SECONDS = 0.25


class RconError(RuntimeError):
    """Something went wrong that the operator should read as a sentence."""


@dataclass
class _Pending:
    """One answer, possibly arriving in several parts."""

    parts: dict[int, str] = field(default_factory=dict)
    expected: int = 1

    def add(self, index: int, total: int, text: str) -> None:
        self.expected = max(total, 1)
        self.parts[index] = text

    @property
    def complete(self) -> bool:
        return len(self.parts) >= self.expected

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


class _Session:
    """One socket, one login, one command. Everything here is synchronous.

    A receiver thread would only be needed to serve several callers from one
    socket - with a session per command there is exactly one caller, and the
    reading loop is the caller's own.
    """

    def __init__(self, host: str, port: int, note, bind_port: int = 0) -> None:
        self._note = note
        self._port = port
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.settimeout(POLL_SECONDS)
        if bind_port:
            # BattlEye remembers a client by address *and* port, and it keeps
            # that entry for 45 s after the last word from it. Twenty commands
            # from twenty ports are twenty clients to it, and it stops
            # answering; from one port they are the same client, logging in
            # again. Hence the fixed port - see RconService._bind_port.
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind(("", bind_port))
        # connect() on UDP only fixes the peer, which is what we want: it
        # filters datagrams from anywhere else and lets send() be used.
        self._sock.connect((host, port))
        self._drain()

    def _drain(self) -> None:
        """Throw away what is already queued on the port.

        Sequence numbers start over with every login, so a very late answer to
        the previous command carries this one's number. Reading it out before
        the login is what keeps the two apart.
        """
        self._sock.settimeout(0)
        try:
            while True:
                self._sock.recv(RECEIVE_SIZE)
        except OSError:
            pass
        finally:
            self._sock.settimeout(POLL_SECONDS)

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def login(self, password: str) -> None:
        payload = bytes([TYPE_LOGIN]) + password.encode("ascii", "replace")
        deadline = time.monotonic() + LOGIN_TIMEOUT
        next_send = 0.0

        while time.monotonic() < deadline:
            if time.monotonic() >= next_send:
                self._transmit(payload)
                next_send = time.monotonic() + LOGIN_RETRY

            kind, body = self._read()
            if kind != TYPE_LOGIN:
                continue
            if body[:1] == b"\x01":
                return
            raise RconError(
                "The server rejected the RCON password - it uses the password "
                "from its last start."
            )

        raise RconError("No answer on the RCON port - BattlEye may still be starting.")

    def command(self, command: str, timeout: float) -> str:
        """Ask, and reassemble the answer.

        Sequence 0, because a login starts the count over: the server answers
        0 and ignores anything else until it has seen it. With one command per
        session there is never a second number.
        """
        seq = 0
        self._transmit(bytes([TYPE_COMMAND, seq]) + command.encode("ascii", "replace"))

        pending = _Pending()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            kind, body = self._read()
            if kind == TYPE_COMMAND and body and body[0] == seq:
                index, total, text = _part(body[1:])
                pending.add(index, total, text)
                if pending.complete:
                    return pending.text()
            elif kind == TYPE_MESSAGE and body:
                self._message(body)

        raise RconError(
            f"No answer from RCON within {timeout:.0f}s - the command may still "
            "have run."
        )

    def _message(self, body: bytes) -> None:
        """A server message that arrived while we were listening.

        The acknowledgement is not optional: without it the server keeps
        resending the same message until it gives up on the client.
        """
        seq, text = body[0], _text(body[1:])
        try:
            self._transmit(bytes([TYPE_MESSAGE, seq]))
        except RconError:
            pass
        for line in text.splitlines():
            if line.strip():
                self._note(f"[rcon] {line}")

    def _transmit(self, payload: bytes) -> None:
        try:
            self._sock.send(encode(payload))
        except ConnectionRefusedError as exc:
            raise RconError(self._closed_port()) from exc
        except OSError as exc:
            raise RconError(f"Could not reach RCON: {exc}") from exc

    def _read(self) -> tuple[int | None, bytes]:
        """The next packet, or (None, b"") when nothing arrived in time."""
        try:
            data = self._sock.recv(RECEIVE_SIZE)
        except socket.timeout:
            return None, b""
        except ConnectionRefusedError as exc:
            # UDP has no connection to refuse - this is the ICMP answer of a
            # host with nothing bound to the port, and it is the normal reply
            # for the minute between the server process and BattlEye starting.
            raise RconError(self._closed_port()) from exc
        except OSError as exc:
            raise RconError(f"Could not read from RCON: {exc}") from exc

        try:
            return decode(data)
        except (ValueError, IndexError) as exc:
            log.debug("Dropped an RCON datagram: %s", exc)
            return None, b""

    def _closed_port(self) -> str:
        return (
            f"Nothing is listening on RCON port {self._port} - BattlEye comes "
            "up a moment after the server does."
        )


class RconService:
    """Connects for the length of one command and closes again.

    Commands are serialised. Sessions share one source port, so two at once
    would be two sockets on it, each reading datagrams meant for the other.
    """

    def __init__(self, host: str, port: int, store, is_up, on_message) -> None:
        self._host = host
        self._port = port
        self._store = store
        self._is_up = is_up
        self._on_message = on_message

        self._turn = threading.Lock()
        self._lock = threading.Lock()
        # What went wrong last time, so the dashboard can show it next to the
        # console instead of only in the alert of whoever clicked.
        self._error = ""
        self._local_port = 0

    @property
    def configured(self) -> bool:
        return bool(self._password())

    @property
    def ready(self) -> bool:
        """A command sent now has a chance of arriving."""
        return bool(self._password()) and self._is_up()

    def status(self) -> dict:
        with self._lock:
            error = self._error
        return {
            "configured": self.configured,
            "ready": self.ready,
            "error": error,
            "port": self._port,
        }

    def send(self, command: str, timeout: float = COMMAND_TIMEOUT) -> str:
        """Log in, run one command, return the server's answer, hang up.

        An empty answer is normal - most commands acknowledge by doing the
        thing and saying nothing.
        """
        command = command.strip()
        if not command:
            raise RconError("No command given.")

        password = self._password()
        if not password:
            raise RconError(
                "No RCON password is set. Add one under Settings - General - "
                "BattlEye and restart the server."
            )
        if not self._is_up():
            raise RconError("The server is not running.")

        with self._turn:
            session = None
            try:
                session = self._open()
                session.login(password)
                answer = session.command(command, timeout)
            except RconError as exc:
                self._remember(str(exc))
                raise
            except OSError as exc:
                self._remember(str(exc))
                raise RconError(f"Could not reach RCON: {exc}") from exc
            finally:
                if session is not None:
                    session.close()

        self._remember("")
        return answer

    # --- internals ---------------------------------------------------------

    def _open(self) -> _Session:
        port = self._bind_port()
        try:
            return _Session(self._host, self._port, self._note, port)
        except OSError:
            # Something else took the port while we were not using it. An
            # ephemeral one still works - it only costs a client entry on the
            # server side.
            return _Session(self._host, self._port, self._note)

    def _bind_port(self) -> int:
        """The source port every session speaks from, picked once by the OS.

        A fixed one is what keeps BattlEye seeing one client instead of a new
        one per command: it holds an entry for 45 s after the last packet, and
        once ten of them are open it simply stops answering the eleventh.
        """
        with self._lock:
            if self._local_port:
                return self._local_port

        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.bind(("", 0))
            port = probe.getsockname()[1]
        except OSError:
            return 0
        finally:
            probe.close()

        with self._lock:
            self._local_port = self._local_port or port
            return self._local_port

    def _password(self) -> str:
        return self._store.current.rcon_password or ""

    def _remember(self, error: str) -> None:
        with self._lock:
            self._error = error
        if error:
            log.info("RCON not available: %s", error)

    def _note(self, line: str) -> None:
        try:
            self._on_message(line)
        except Exception:  # noqa: BLE001 - the console is not worth a dead call
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
