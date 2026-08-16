"""Scheduled actions - cron style."""

from __future__ import annotations

import logging
import os
import time

from flask import Blueprint, current_app, jsonify, render_template, request
from flask_login import login_required

from ..services.audit import record
from ..services.schedules import (
    ACTIONS,
    MAX_ACTIONS,
    MAX_DELAY_SECONDS,
    NEEDS_COMMAND,
    ScheduleError,
)

log = logging.getLogger(__name__)

bp = Blueprint("schedules", __name__, url_prefix="/schedules")

# Offered under the time field. Not a substitute for the expression - it fills
# it in, and stays visible, so nobody has to look up the field order to make a
# small change.
PRESETS = (
    {"label": "Every day at 04:00", "cron": "0 4 * * *"},
    {"label": "Every 6 hours", "cron": "0 */6 * * *"},
    {"label": "Every hour", "cron": "0 * * * *"},
    {"label": "Mondays at 05:00", "cron": "0 5 * * 1"},
)


def _service():
    return current_app.extensions["schedules"]


@bp.get("/")
@login_required
def index():
    return render_template(
        "schedules.html",
        schedules=_service().list(),
        action_kinds=ACTIONS,
        needs_command=sorted(NEEDS_COMMAND),
        max_delay=MAX_DELAY_SECONDS,
        max_actions=MAX_ACTIONS,
        presets=PRESETS,
        # TZ names the zone the operator set; tzname is what the process
        # actually resolved it to, which is the one cron will use.
        timezone=os.environ.get("TZ") or time.tzname[0],
    )


@bp.get("/list.json")
@login_required
def list_json():
    return jsonify(schedules=_service().list())


@bp.post("/")
@login_required
def create():
    return _write(lambda: _service().create(request.get_json(silent=True) or {}))


@bp.post("/<schedule_id>")
@login_required
def update(schedule_id: str):
    payload = request.get_json(silent=True) or {}
    return _write(lambda: _service().update(schedule_id, payload))


@bp.post("/<schedule_id>/duplicate")
@login_required
def duplicate(schedule_id: str):
    return _write(lambda: _service().duplicate(schedule_id))


@bp.post("/<schedule_id>/delete")
@login_required
def delete(schedule_id: str):
    return _write(lambda: _service().delete(schedule_id))


@bp.post("/<schedule_id>/run")
@login_required
def run_now(schedule_id: str):
    """Run an entry now. The only way to find out what it really does."""
    return _write(lambda: _service().run_now(schedule_id))


def _write(action):
    name = f"schedules.{(request.endpoint or '').rpartition('.')[2]}"
    target = (request.view_args or {}).get("schedule_id", "")
    try:
        action()
    except ScheduleError as exc:
        # 400: the entry is wrong, and the message says how. The list is sent
        # back either way so the page never shows a state that no longer holds.
        record(name, target, ok=False, detail=str(exc))
        return jsonify(ok=False, error=str(exc), schedules=_service().list()), 400

    record(name, target)
    return jsonify(ok=True, schedules=_service().list())
