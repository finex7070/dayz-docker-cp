"""Job control: start SteamCMD runs, poll their output, answer Steam Guard."""

from __future__ import annotations

from flask import Blueprint, abort, current_app, jsonify, redirect, request, url_for
from flask_login import login_required

from ..services.audit import record
from ..services.jobs import JobBusy, jobs
from ..services.steamcmd import SteamCmdService

bp = Blueprint("jobs", __name__, url_prefix="/jobs")


def _service() -> SteamCmdService:
    return current_app.extensions["steamcmd"]


@bp.get("/active.json")
@login_required
def active_json():
    """Poll endpoint for the pages that follow a job.

    `after` is a cursor into the job's output buffer, so each poll only
    transfers the lines that appeared since the last one.

    `kinds` restricts the answer to the job family the asking page is about.
    Independently of that, `busy` reports whether *any* job holds the exclusive
    slot - a page has to be able to disable its buttons for a job it does not
    itself display.
    """
    after = request.args.get("after", type=int, default=0)
    kinds = {k for k in (request.args.get("kinds") or "").split(",") if k} or None

    active = jobs.active
    if active is not None and (kinds is None or active.kind in kinds):
        job = active
    else:
        job = jobs.latest(kinds)

    return jsonify(
        job=job.to_dict(after=after) if job else None,
        busy=active is not None,
        busy_title=active.title if active else "",
    )


@bp.get("/<job_id>.json")
@login_required
def job_json(job_id: str):
    job = jobs.get(job_id)
    if job is None:
        abort(404)
    after = request.args.get("after", type=int, default=0)
    return jsonify(job=job.to_dict(after=after))


@bp.post("/install")
@login_required
def install():
    blocked = _running_server_warning()
    if blocked:
        return blocked
    return _start(lambda: _service().install_server())


@bp.post("/update")
@login_required
def update():
    blocked = _running_server_warning()
    if blocked:
        return blocked
    return _start(lambda: _service().update_server(validate=True))


def _running_server_warning():
    """Refuse to rewrite the server files under a running server.

    SteamCMD replaces binaries and PBOs in place. Doing that while the server
    has them mapped gives a half-old, half-new install that usually only shows
    up as a crash much later. The operator can insist - hence the flag - but
    has to see the warning first.
    """
    payload = request.get_json(silent=True) or {}
    if payload.get("force"):
        return None

    manager = current_app.extensions["server"]
    if not manager.running:
        return None

    return jsonify(
        ok=False,
        needs_confirm=True,
        error=(
            "The DayZ server is still running. SteamCMD would overwrite files "
            "it has open. Stop the server first - or continue anyway?"
        ),
    ), 409


@bp.post("/<job_id>/guard")
@login_required
def submit_guard(job_id: str):
    job = jobs.get(job_id)
    if job is None:
        abort(404)

    payload = request.get_json(silent=True) or {}
    code = (request.form.get("code") or payload.get("code") or "").strip()
    if not code:
        return jsonify(ok=False, error="No code supplied."), 400
    if not job.submit_guard_code(code):
        return jsonify(ok=False, error="This job is not waiting for a code."), 409
    return jsonify(ok=True)


@bp.post("/<job_id>/cancel")
@login_required
def cancel(job_id: str):
    job = jobs.get(job_id)
    if job is None:
        abort(404)
    job.cancel()
    record("steamcmd.cancel", job.title)
    return jsonify(ok=True)


def _start(starter):
    """Start a job, translating the busy case into a useful response."""
    action = f"steamcmd.{(request.endpoint or '').rpartition('.')[2]}"
    try:
        job = starter()
    except JobBusy as busy:
        record(action, ok=False, detail=f"busy: {busy.active.title}")
        message = f"Another job is already running: {busy.active.title}"
        if _wants_json():
            return jsonify(ok=False, error=message, job_id=busy.active.id), 409
        return redirect(url_for("dashboard.index"))

    record(action, detail=f"started: {job.title}")
    if _wants_json():
        return jsonify(ok=True, job_id=job.id)
    return redirect(url_for("dashboard.index"))


def _wants_json() -> bool:
    return request.accept_mimetypes.best == "application/json" or request.is_json


# No CSRF handling here on purpose: CSRFProtect is registered app-wide and
# accepts the token either as a form field or in the X-CSRFToken header, which
# is what the dashboard's fetch() calls send.
