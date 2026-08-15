"""When the server files were last updated, and when we last looked.

Two different questions, and only one of them has an answer on disk already.

*Updated* comes from SteamCMD's own `appmanifest_223350.acf`: `LastUpdated` is
the moment Steam last wrote files into the install directory, and the build id
next to it says which version that produced. Nothing the panel records could be
more accurate - a job that downloaded nothing must not move that date.

*Checked* is the panel's own note: an `app_update` that finds everything
current writes no files and leaves the manifest untouched, so without this the
dashboard could only ever say "updated three weeks ago" and never "and it was
still current an hour ago". Those are the two halves an operator is asking
about when they look at that line.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

log = logging.getLogger(__name__)

STATE_FILE = "server_files.json"


def read_manifest(paths, app_id: int) -> dict:
    """Build id and update time from the Steam manifest, empty if unreadable."""
    path = paths.server / "steamapps" / f"appmanifest_{app_id}.acf"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}

    def field(name: str) -> str | None:
        # The .acf format is Valve's KeyValues: "key" <tabs> "value".
        match = re.search(rf'"{name}"\s+"([^"]*)"', text)
        return match.group(1) if match else None

    updated = field("LastUpdated")
    size = field("SizeOnDisk")
    return {
        "build_id": field("buildid") or "",
        "last_updated": float(updated) if updated and updated.isdigit() else None,
        "size_bytes": int(size) if size and size.isdigit() else None,
    }


def read_checked(paths) -> float | None:
    try:
        data = json.loads((paths.panel / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    value = data.get("last_checked")
    return float(value) if isinstance(value, (int, float)) else None


def mark_checked(paths) -> None:
    """Record that SteamCMD just confirmed the files, whatever it changed."""
    path = paths.panel / STATE_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"last_checked": time.time()}), encoding="utf-8")
    except OSError as exc:
        # Never fatal: this is a note about a job that already succeeded.
        log.warning("Could not record the update check: %s", exc)


def summary(paths, app_id: int) -> dict:
    """What the dashboard prints under "Server files"."""
    manifest = read_manifest(paths, app_id)
    return {
        "build_id": manifest.get("build_id", ""),
        "last_updated": manifest.get("last_updated"),
        "last_updated_text": _stamp(manifest.get("last_updated")),
        "last_checked": read_checked(paths),
        "last_checked_text": _stamp(read_checked(paths)),
    }


def _stamp(value: float | None) -> str:
    if not value:
        return "never"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(value))
