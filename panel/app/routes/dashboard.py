"""Dashboard - server files, status, controls and the live console."""

from __future__ import annotations

import time

from flask import Blueprint, current_app, jsonify, render_template
from flask_login import login_required

from ..services.jobs import SERVER_FILE_KINDS, jobs
from ..services import server_files
from ..services.server import host_capacity

bp = Blueprint("dashboard", __name__)


@bp.get("/")
@login_required
def index():
    from .. import environment_checks  # local import avoids a cycle

    from .server import current_status  # local import avoids a cycle

    settings = current_app.config["SETTINGS"]

    # Only server file jobs: a mod download belongs under the mods page, not
    # under a card headed "Server files".
    active = jobs.active
    if active is None or active.kind not in SERVER_FILE_KINDS:
        active = jobs.latest(SERVER_FILE_KINDS)

    return render_template(
        "dashboard.html",
        checks=environment_checks(settings),
        files=server_files.summary(settings.paths, settings.SERVER_APP_ID),
        job=active.to_dict(after=0) if active else None,
        job_kinds=",".join(sorted(SERVER_FILE_KINDS)),
        status=current_status(),
        live=_live_values(),
    )


@bp.get("/status.json")
@login_required
def status_json():
    """Values that go stale while the dashboard sits open.

    The same dict feeds the first render and every refresh, so a value can
    never be formatted one way on load and another way afterwards. Raw numbers
    rather than finished strings: the client formats them, which lets a
    duration keep counting between two polls.
    """
    return jsonify(_live_values())


def _live_values() -> dict[str, object]:
    from .. import STARTED_AT  # local import avoids a cycle

    from .server import current_status  # local import avoids a cycle

    query = current_app.extensions["query"]
    status = current_status()

    # Only ask the game server when it could plausibly answer - a query
    # against a stopped server just waits for the timeout.
    info = query.info() if status["state"] == "running" else None

    capacity = host_capacity()

    return {
        "panel_uptime_seconds": int(time.time() - STARTED_AT),
        "server_state": status["label"],
        "server_state_class": _STATE_TEXT_CLASS.get(status["state"], "text-secondary"),
        "server_note": _server_note(status),
        # None, not 0: the client advances every duration it is given once a
        # second, so a "0" here would tick upwards next to a stopped server.
        # No process, no uptime.
        "server_uptime_seconds": None if status["pid"] is None else status["uptime_seconds"],
        "server_started_note": _started_note(status),
        "server_players": info.player_label if info else "—",
        "server_players_note": _peak_note(status, info),
        "server_cpu": "—" if status["cpu_percent"] is None
        else f"{status['cpu_percent']} %",
        "server_cpu_note": _capacity_note(capacity["cores"], "cpu"),
        "server_memory": "—" if status["rss_mb"] is None
        else f"{status['rss_mb']:.0f} MB",
        "server_memory_note": _capacity_note(capacity["memory_mb"], "memory"),
    }


def _started_note(status: dict) -> str:
    started = status.get("started_at")
    if not started:
        return NO_VALUE
    return "Started " + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started))


# Peak players since the current server process started. Kept here rather than
# in the query service because it is a property of this run of the server: the
# key is the start timestamp, so a restart begins a fresh count without anyone
# having to remember to reset it.
_peak: dict[str, object] = {"since": None, "value": 0}


def _peak_note(status: dict, info) -> str:
    started = status.get("started_at")
    if not started:
        return NO_VALUE

    if _peak["since"] != started:
        _peak.update(since=started, value=0)
    if info is not None and info.players > _peak["value"]:
        _peak["value"] = info.players

    return f"peak {_peak['value']} since the start"


def _capacity_note(value: float | None, kind: str) -> str:
    if not value:
        return "of an unknown total"      # no cgroup, no /proc - say so
    if kind == "cpu":
        cores = round(value, 1)
        cores = int(cores) if float(cores).is_integer() else cores
        return f"of {cores * 100:.0f} % ({cores} core{'s' if cores != 1 else ''})"
    if value >= 1024:
        return f"of {value / 1024:.1f} GB"
    return f"of {value:.0f} MB"


# What every tile shows where there is nothing to show - the same character the
# duration filter uses, so a stopped server reads as one row of dashes rather
# than three different ways of saying the same thing.
NO_VALUE = "—"

_STATE_TEXT_CLASS = {
    "running": "text-success",
    "starting": "text-info",
    "stopping": "text-warning",
    "crashed": "text-danger",
    "stopped": "text-secondary",
}


def _server_note(status: dict) -> str:
    if status["state"] == "running":
        # The pid is of no use from a browser - it names a process nobody here
        # can reach. The mission is what tells you which server this is.
        return f"mission {status['mission']}"
    if status["state"] == "crashed":
        return f"exit code {status['exit_code']}"
    if status.get("startup_step"):
        return f"{status['startup_step']} before start"
    if status["blocked_reason"]:
        return status["blocked_reason"]
    # The tile above already says "Stopped". Repeating it in smaller type adds
    # nothing, and the dash matches the value the other tiles show.
    return NO_VALUE


@bp.app_template_filter("duration")
def _format_uptime(seconds: float | None) -> str:
    if seconds is None:
        return "—"  # matches the dash the other tiles show when there is no value
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)

    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"
