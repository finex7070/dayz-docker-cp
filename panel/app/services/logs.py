"""The server's log files: which ones exist, and what is in them.

DayZ starts a new set of files on every launch
(`DayZServer_<timestamp>.RPT`, `.ADM`, `script_<timestamp>.log`), so the log
page offers a type first and then the individual file. Yesterday's crash is
still there to read - which is the whole point of keeping the old files.

These are read on demand, not streamed: they are already on disk, and a page
that reloads a file when it changes is easier to reason about than a live tail
of a file nobody is writing to. The live view of the running server is the
console on the dashboard, which reads the process output directly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# A single RPT file can grow past 100 MB in a long session. Only the tail is
# ever sent to the browser; the download button serves the whole file.
MAX_VIEW_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class LogType:
    id: str
    label: str
    pattern: str
    detail: str


LOG_TYPES = (
    LogType("adm", "Admin log (.ADM)", "*.ADM",
            "connects, disconnects, deaths and admin actions"),
    LogType("rpt", "Server report (.RPT)", "*.RPT",
            "the engine log - errors, warnings and startup"),
    LogType("net", "Network log", "*.net.log",
            "traffic log, only written with -netLog"),
    LogType("script", "Script log", "script*.log",
            "script errors from the mission and from mods"),
)

_BY_ID = {log_type.id: log_type for log_type in LOG_TYPES}


@dataclass(frozen=True)
class LogFile:
    name: str
    size: int
    modified: float

    def to_dict(self) -> dict:
        return {"name": self.name, "size": self.size, "modified": self.modified}


class LogService:
    def __init__(self, settings) -> None:
        self._directory = settings.paths.profiles

    @property
    def types(self) -> tuple[LogType, ...]:
        return LOG_TYPES

    def files(self, type_id: str) -> list[LogFile]:
        """Newest first - that is the one being written to right now."""
        log_type = _BY_ID.get(type_id)
        if log_type is None:
            return []

        found = []
        for path in self._directory.glob(log_type.pattern):
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            found.append(LogFile(path.name, stat.st_size, stat.st_mtime))

        return sorted(found, key=lambda f: f.modified, reverse=True)

    def resolve(self, type_id: str, name: str) -> Path | None:
        """Turn a type and file name into a path, or refuse.

        The name is compared against the files the pattern actually matches
        instead of being joined onto the directory. A request can therefore
        only ever name a log file that exists, whatever it puts in the
        parameter - no separators, no '..', no symlink to somewhere else.
        """
        if not name:
            return None
        if name not in {f.name for f in self.files(type_id)}:
            return None

        path = self._directory / name
        try:
            resolved = path.resolve()
            resolved.relative_to(self._directory.resolve())
        except (OSError, ValueError):
            return None
        return resolved

    def read(self, path: Path) -> dict:
        """The tail of a file, plus what a client needs to spot a change."""
        try:
            stat = path.stat()
            with path.open("rb") as handle:
                if stat.st_size > MAX_VIEW_BYTES:
                    handle.seek(stat.st_size - MAX_VIEW_BYTES)
                    data = handle.read()
                    # The seek lands mid-line; drop that fragment.
                    data = data.partition(b"\n")[2]
                    truncated = True
                else:
                    data = handle.read()
                    truncated = False
        except OSError as exc:
            log.warning("Could not read %s: %s", path, exc)
            return {"lines": [], "size": 0, "modified": 0.0, "truncated": False}

        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines()
        return {
            "lines": lines,
            "size": stat.st_size,
            "modified": stat.st_mtime,
            "truncated": truncated,
        }
