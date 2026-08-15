"""Log files: pick a type, pick a file, read it."""

from __future__ import annotations

from flask import Blueprint, Response, current_app, jsonify, render_template, request
from flask_login import login_required

from ..services.logs import LogService

bp = Blueprint("logs", __name__, url_prefix="/logs")


def _service() -> LogService:
    return current_app.extensions["logs"]


@bp.get("/")
@login_required
def index():
    service = _service()
    return render_template(
        "logs.html",
        log_types=service.types,
        current_type=request.args.get("type", service.types[0].id),
        audit=current_app.extensions["audit"].tail(),
    )


@bp.get("/files.json")
@login_required
def files_json():
    files = _service().files(request.args.get("type", ""))
    return jsonify(files=[f.to_dict() for f in files])


@bp.get("/content.json")
@login_required
def content_json():
    """The file's contents, plus size and mtime for change detection.

    The client compares those two against what it already has, so the auto
    reload only transfers a file that actually grew.
    """
    service = _service()
    path = service.resolve(request.args.get("type", ""), request.args.get("name", ""))
    if path is None:
        return jsonify(error="Unknown log file."), 404
    return jsonify(name=path.name, **service.read(path))


@bp.get("/stat.json")
@login_required
def stat_json():
    """Size and mtime only - what the auto reload polls."""
    service = _service()
    path = service.resolve(request.args.get("type", ""), request.args.get("name", ""))
    if path is None:
        return jsonify(error="Unknown log file."), 404
    try:
        stat = path.stat()
    except OSError:
        return jsonify(error="The file disappeared."), 404
    return jsonify(size=stat.st_size, modified=stat.st_mtime)


@bp.get("/download")
@login_required
def download():
    """The complete file, not the capped view."""
    service = _service()
    path = service.resolve(request.args.get("type", ""), request.args.get("name", ""))
    if path is None:
        return jsonify(error="Unknown log file."), 404

    def chunks():
        with path.open("rb") as handle:
            while True:
                data = handle.read(64 * 1024)
                if not data:
                    return
                yield data

    return Response(
        chunks(),
        mimetype="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{path.name}"'},
    )
