"""Steam A2S query - the player count without RCON.

The same request the in-game server browser makes, sent to the server's Steam
query port. It needs no password and no BattlEye, which is why the player
count works before RCON exists and keeps working if RCON is switched off.

Only A2S_INFO is implemented: name, map and player counts. That is what the
dashboard shows; A2S_PLAYER (the name list) belongs with the RCON work, where
kicking and messaging live too.
"""

from __future__ import annotations

import logging
import socket
import struct
import threading
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

_HEADER = b"\xff\xff\xff\xff"
_A2S_INFO = _HEADER + b"TSource Engine Query\x00"

TIMEOUT_SECONDS = 1.0

# The server is asked at most this often. Several browser tabs polling the
# dashboard must not turn into a query flood against the game server.
CACHE_SECONDS = 5.0


@dataclass(frozen=True)
class ServerInfo:
    name: str = ""
    map_name: str = ""
    players: int = 0
    max_players: int = 0
    reachable: bool = False

    @property
    def player_label(self) -> str:
        if not self.reachable:
            return "—"
        return f"{self.players} / {self.max_players}"


class QueryService:
    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port
        self._lock = threading.Lock()
        self._cached = ServerInfo()
        self._fetched_at = 0.0

    def info(self, *, force: bool = False) -> ServerInfo:
        with self._lock:
            fresh = time.monotonic() - self._fetched_at < CACHE_SECONDS
            if fresh and not force:
                return self._cached

        info = self._query()
        with self._lock:
            self._cached = info
            self._fetched_at = time.monotonic()
        return info

    def _query(self) -> ServerInfo:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(TIMEOUT_SECONDS)
                sock.sendto(_A2S_INFO, (self._host, self._port))
                data, _ = sock.recvfrom(4096)

                # Modern Steam servers answer the first request with a
                # challenge that has to be echoed back before they reply.
                if len(data) >= 5 and data[4:5] == b"A":
                    sock.sendto(_A2S_INFO + data[5:9], (self._host, self._port))
                    data, _ = sock.recvfrom(4096)
        except (OSError, socket.timeout):
            # Not running, still loading, or the port is closed - all the same
            # to the dashboard: no numbers to show.
            return ServerInfo()

        return _parse_info(data)


def _parse_info(data: bytes) -> ServerInfo:
    if len(data) < 6 or data[4:5] != b"I":
        return ServerInfo()

    try:
        offset = 6  # 4 header bytes, 'I', protocol version
        name, offset = _read_string(data, offset)
        map_name, offset = _read_string(data, offset)
        _folder, offset = _read_string(data, offset)
        _game, offset = _read_string(data, offset)

        offset += 2  # app id, a short we do not need
        players = data[offset]
        max_players = data[offset + 1]
    except (IndexError, struct.error, UnicodeDecodeError):
        log.debug("Unparsable A2S response (%d bytes)", len(data))
        return ServerInfo()

    return ServerInfo(
        name=name,
        map_name=map_name,
        players=players,
        max_players=max_players,
        reachable=True,
    )


def _read_string(data: bytes, offset: int) -> tuple[str, int]:
    end = data.index(b"\x00", offset)
    return data[offset:end].decode("utf-8", errors="replace"), end + 1
