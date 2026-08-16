"""Who did what, when, and whether it worked.

The DayZ server's own logs say what the *game* did. They say nothing about the
panel: who restarted the server at 3 a.m., who changed `verifySignatures`, who
deleted a mod. On a server several people administer, that gap is the one that
turns "the server has been odd since yesterday" into guesswork.

One line of JSON per entry, appended to `/data/panel/audit.log`. JSON lines
rather than a database because the file is meant to be readable with `tail`
and `grep` on the host, and appending is the one operation that cannot leave a
half-written record behind for the next reader.

Nothing here is a security boundary: an operator with panel access can edit the
file through the bind mount. It is a record for people running a server
together, not evidence.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

from flask import g, has_app_context, has_request_context, request
from flask_login import current_user

log = logging.getLogger(__name__)

# Rotated at this size into a single `.1` file. Two files, no schedule: the
# entries are small, and a year of ordinary use does not reach the limit.
MAX_BYTES = 2 * 1024 * 1024

# What the page shows. Older entries stay in the file.
TAIL_LINES = 500

MAX_DETAIL = 300


class AuditLog:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    def record(self, action: str, target: str = "", ok: bool = True,
               detail: str = "") -> None:
        """Append one entry. Never raises - a failed write must not fail the action."""
        entry = {
            "time": time.time(),
            "user": _user(),
            "ip": _ip(),
            "action": action,
            "target": target,
            "ok": bool(ok),
            "detail": (detail or "")[:MAX_DETAIL],
        }
        try:
            with self._lock:
                self._rotate_if_needed()
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:
            log.warning("Could not write the audit log: %s", exc)

    def _rotate_if_needed(self) -> None:
        try:
            if self._path.stat().st_size < MAX_BYTES:
                return
        except OSError:
            return
        self._path.replace(self._path.with_suffix(self._path.suffix + ".1"))

    def tail(self, limit: int = TAIL_LINES) -> list[dict]:
        """The newest entries first, damaged lines skipped rather than fatal."""
        try:
            lines = self._path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []

        entries = []
        for line in lines[-limit:]:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            entry["time_text"] = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(entry.get("time", 0))
            )
            entries.append(entry)
        entries.reverse()
        return entries


def record(action: str, target: str = "", ok: bool = True, detail: str = "") -> None:
    """Record from inside a request, without every route holding the object."""
    from flask import current_app

    audit = current_app.extensions.get("audit")
    if audit is not None:
        audit.record(action, target, ok, detail)


def _user() -> str:
    if has_request_context():
        try:
            return current_user.get_id() or "anonymous"
        except Exception:  # noqa: BLE001 - never let the record break the action
            return "unknown"
    # The scheduler and the watchdog act without anyone asking. They also act
    # from threads of their own, where there is no application context and `g`
    # raises rather than returning the default - which used to take the caller
    # down with it, on the line after its work was already done.
    if not has_app_context():
        return "system"
    return getattr(g, "audit_actor", "") or "system"


def _ip() -> str:
    if not has_request_context():
        return ""
    # remote_addr is already the real client: TrustedProxyFix rewrites it, and
    # only for peers named in TRUSTED_PROXY_IPS.
    return request.remote_addr or ""
