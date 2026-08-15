"""Server control API.

No page of its own: the controls live on the dashboard, next to the status
they act on. What remains here are the actions and the status endpoint they
share with it.
"""

from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request
from flask_login import login_required

from ..services.audit import record
from ..services.jobs import jobs
from ..services.rcon import RconError
from ..services.server import ServerError, ServerManager

bp = Blueprint("server", __name__, url_prefix="/server")


def _manager() -> ServerManager:
    return current_app.extensions["server"]


def _rcon():
    return current_app.extensions["rcon"]


def _sequence():
    return current_app.extensions["startup"]


@bp.get("/status.json")
@login_required
def status_json():
    return jsonify(current_status())


def current_status() -> dict:
    """Server status with the startup sequence folded in.

    The manager knows nothing about updates running before a start, so on its
    own it would report a stopped server as startable while a download for
    that very start is still in flight.
    """
    status = current_app.extensions["server"].status()
    sequence = current_app.extensions["startup"]

    status["startup_step"] = sequence.step
    status["rcon"] = current_app.extensions["rcon"].status()
    # For the Backup button: it does not depend on the server's state but on
    # the job slot, which a SteamCMD run may be holding.
    status["job_busy"] = jobs.busy
    if sequence.busy:
        status["can_start"] = False
        if not status["blocked_reason"]:
            status["blocked_reason"] = f"Start in progress: {sequence.step}"
    return status


@bp.post("/start")
@login_required
def start():
    """Start the server, running the enabled updates first.

    Goes through the startup sequence rather than straight to the manager, so
    that "update on start" means what it says - see services/startup.py.
    """
    return _action(lambda: _sequence().start_server(reason="manual"), "server.start")


@bp.post("/stop")
@login_required
def stop():
    return _action(lambda: _manager().stop(), "server.stop")


@bp.post("/restart")
@login_required
def restart():
    return _action(
        lambda: _sequence().start_server(reason="restart", restart=True), "server.restart"
    )


@bp.post("/lock")
@login_required
def lock():
    """Refuse new connections. Players already on the server stay."""
    return _rcon_action("#lock", "Server locked - no new players can join.")


@bp.post("/unlock")
@login_required
def unlock():
    return _rcon_action("#unlock", "Server unlocked.")


def _rcon_action(command: str, note: str):
    """Run a control command over RCON and report it in the console.

    Lock state is not something the server tells us on request: there is no
    query for it. What the panel can honestly show is that the command went
    out, so it says that in the console instead of tracking a flag that would
    drift the moment anyone used RCON from elsewhere.
    """
    manager = _manager()
    try:
        answer = _rcon().send(command)
    except RconError as exc:
        record(f"server.{command.lstrip('#')}", ok=False, detail=str(exc))
        return jsonify(ok=False, error=str(exc)), 409

    record(f"server.{command.lstrip('#')}")
    manager.console_note(f"[rcon] > {command}")
    for line in answer.splitlines():
        if line.strip():
            manager.console_note(f"[rcon] {line}")
    manager.console_note(f"[panel] {note}")
    return jsonify(ok=True, message=note, status=current_status())


def _action(run, name: str):
    try:
        message = run()
    except ServerError as exc:
        # 409: the request was well formed, the server is just in the wrong
        # state for it. The UI shows the message instead of a generic failure.
        record(name, ok=False, detail=str(exc))
        return jsonify(ok=False, error=str(exc)), 409
    except OSError as exc:
        record(name, ok=False, detail=str(exc))
        return jsonify(ok=False, error=str(exc)), 500

    record(name, detail=message or "")
    return jsonify(ok=True, message=message or "", status=current_status())
