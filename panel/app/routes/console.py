"""The live console on the dashboard: server output, and commands back.

Server-Sent Events rather than polling - output arrives when the server
produces it, the browser reconnects on its own, and it needs nothing beyond
plain HTTP, which matters behind the reverse proxy people put in front of
this.

Each open stream holds a worker thread for as long as it is open, so the
number of concurrent streams is capped. Without that, a few forgotten browser
tabs would use up every thread and the panel itself would stop answering.
"""

from __future__ import annotations

import json
import logging
import threading
import time

from flask import (
    Blueprint,
    Response,
    current_app,
    jsonify,
    request,
    stream_with_context,
)
from flask_login import login_required

from ..extensions import limiter
from ..services.audit import record
from ..services.rcon import RconError

log = logging.getLogger(__name__)

bp = Blueprint("console", __name__, url_prefix="/console")

POLL_SECONDS = 1.0

# Without traffic, proxies drop an idle connection after 30-60s. A comment
# line every 15s keeps it open and costs nothing.
HEARTBEAT_SECONDS = 15.0

MAX_CONCURRENT_STREAMS = 4
_slots = threading.Semaphore(MAX_CONCURRENT_STREAMS)

TAIL_LINES = 400


def _manager():
    return current_app.extensions["server"]


@bp.get("/stream")
@login_required
@limiter.exempt
def stream():
    after = request.args.get("after", type=str, default="")

    if not _slots.acquire(blocking=False):
        return (
            jsonify(
                error=(
                    f"Too many console streams are open ({MAX_CONCURRENT_STREAMS}). "
                    "Close another tab and try again."
                )
            ),
            503,
        )

    manager = _manager()

    @stream_with_context
    def events():
        try:
            cursor = after
            first = _read(manager, cursor, initial=not after)
            cursor = first["cursor"]
            yield f"data: {json.dumps(first)}\n\n"
            last_sent = time.monotonic()

            while True:
                time.sleep(POLL_SECONDS)
                payload = _read(manager, cursor)
                cursor = payload["cursor"]

                if payload["lines"] or payload["reset"]:
                    yield f"data: {json.dumps(payload)}\n\n"
                    last_sent = time.monotonic()
                elif time.monotonic() - last_sent >= HEARTBEAT_SECONDS:
                    yield ": ping\n\n"
                    last_sent = time.monotonic()
        except GeneratorExit:
            raise  # the tab was closed, nothing to report
        except Exception:  # noqa: BLE001 - a broken stream must not kill the thread
            log.exception("Console stream failed")
        finally:
            _slots.release()

    return Response(
        events(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            # nginx buffers proxied responses by default, which would hold
            # every event back until the buffer fills - that is, forever.
            "X-Accel-Buffering": "no",
        },
    )


@bp.post("/send")
@login_required
def send():
    """Send one RCON command and put both it and its answer in the console.

    The echo goes into the shared buffer rather than being returned to the one
    browser that asked: a second operator looking at the same console has to
    see that someone just kicked a player, not only the effect of it.
    """
    payload = request.get_json(silent=True) or {}
    command = (payload.get("command") or "").strip()
    if not command:
        return jsonify(ok=False, error="No command given."), 400

    manager = _manager()
    rcon = current_app.extensions["rcon"]

    try:
        answer = rcon.send(command)
    except RconError as exc:
        record("console.command", command, ok=False, detail=str(exc))
        return jsonify(ok=False, error=str(exc)), 409

    # The command itself is the target: "who kicked that player" is the whole
    # reason the record exists.
    record("console.command", command)
    log.info("RCON command from the panel: %s", command)
    manager.console_note(f"[rcon] > {command}")
    for line in answer.splitlines():
        if line.strip():
            manager.console_note(f"[rcon] {line}")

    return jsonify(ok=True, answer=answer)


def _read(manager, cursor: str, initial: bool = False) -> dict:
    """Read the server's output buffer from `cursor`.

    The buffer is replaced whenever the server restarts, so its id travels
    with the cursor: a changed id means the old position is meaningless and
    the client has to replace its view rather than append to it.
    """
    buffer = manager.console_buffer
    buffer_id, _, raw_index = (cursor or "").rpartition(":")

    if initial or buffer_id != buffer.id:
        start = max(buffer.next_index - TAIL_LINES, 0)
        first, lines = buffer.since(start)
        return {
            "cursor": f"{buffer.id}:{first + len(lines)}",
            "lines": lines,
            "reset": True,
            "note": "" if initial else "[panel] --- server restarted ---",
        }

    index = int(raw_index or 0)
    first, lines = buffer.since(index)
    return {
        "cursor": f"{buffer.id}:{first + len(lines)}",
        "lines": lines,
        "reset": first > index,
        "note": "[panel] ... earlier output dropped from the buffer ..."
        if first > index else "",
    }
