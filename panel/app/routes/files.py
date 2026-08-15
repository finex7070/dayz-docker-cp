"""File browser for the server directory."""

from __future__ import annotations

import logging

from flask import Blueprint, current_app, jsonify, render_template, request, send_file
from flask_login import login_required

from ..services.audit import record
from ..services.files import FileError

log = logging.getLogger(__name__)

bp = Blueprint("files", __name__, url_prefix="/files")


def _service():
    return current_app.extensions["files"]


@bp.get("/")
@login_required
def index():
    service = _service()
    try:
        listing = service.listing("")
    except FileError as exc:
        listing = {"path": "", "parent": None, "crumbs": [], "entries": [], "error": str(exc)}

    return render_template(
        "files.html",
        listing=listing,
        root=str(service.root),
        max_upload_mb=current_app.config["SETTINGS"].max_upload_mb,
    )


@bp.get("/list.json")
@login_required
def list_json():
    return _answer(lambda: _service().listing(request.args.get("path", "")))


@bp.get("/read.json")
@login_required
def read_json():
    return _answer(lambda: _service().read(request.args.get("path", "")))


@bp.post("/save")
@login_required
def save():
    payload = request.get_json(silent=True) or {}
    path = payload.get("path", "")

    def run():
        _service().write(path, payload.get("text", ""))
        log.info("File saved from the panel: %s", path)
        record("files.save", path)
        return {"path": path}

    return _answer(run, "files.save", path)


@bp.post("/upload")
@login_required
def upload():
    """Upload into the directory currently shown.

    The filename from the browser is treated as a name and nothing else: one
    arriving as `../../serverDZ.cfg` is refused rather than cleaned up, because
    a cleaned-up name is a guess at what the sender meant.
    """
    directory = request.form.get("path", "")
    uploaded = request.files.getlist("files")
    if not uploaded:
        return jsonify(ok=False, error="No file was selected."), 400

    service = _service()
    try:
        service.listing(directory)
    except FileError as exc:
        return jsonify(ok=False, error=str(exc)), 400

    written, failed = [], []
    for item in uploaded:
        try:
            written.append(service.upload(directory, item.filename or "", item.stream))
        except FileError as exc:
            # One bad name must not discard the files that were fine.
            failed.append(f"{item.filename}: {exc}")

    for name in written:
        record("files.upload", name)
    for problem in failed:
        record("files.upload", directory, ok=False, detail=problem)

    if failed and not written:
        return jsonify(ok=False, error="; ".join(failed),
                       listing=service.listing(directory)), 400
    return jsonify(
        ok=True,
        written=written,
        error="; ".join(failed),
        listing=service.listing(directory),
    )


@bp.post("/mkdir")
@login_required
def mkdir():
    payload = request.get_json(silent=True) or {}
    directory = payload.get("path", "")
    def run():
        created = _service().make_directory(directory, payload.get("name", ""))
        record("files.mkdir", created)
        return {"created": created, "listing": _service().listing(directory)}

    return _answer(run, "files.mkdir", directory)


@bp.post("/delete")
@login_required
def delete():
    payload = request.get_json(silent=True) or {}
    path = payload.get("path", "")

    def run():
        kind = _service().delete(path)
        log.info("File deleted from the panel: %s", path)
        record("files.delete", path, detail=kind)
        parent = path.rpartition("/")[0]
        return {"deleted": path, "kind": kind, "listing": _service().listing(parent)}

    return _answer(run, "files.delete", path)


@bp.post("/create")
@login_required
def create():
    payload = request.get_json(silent=True) or {}
    directory = payload.get("path", "")

    def run():
        created = _service().create_file(directory, payload.get("name", ""))
        record("files.create", created)
        return {"created": created, "listing": _service().listing(directory)}

    return _answer(run, "files.create", directory)


@bp.post("/rename")
@login_required
def rename():
    payload = request.get_json(silent=True) or {}
    path = payload.get("path", "")

    def run():
        moved = _service().rename(path, payload.get("name", ""))
        record("files.rename", path, detail=f"-> {moved}")
        return {"path": moved, "listing": _service().listing(path.rpartition("/")[0])}

    return _answer(run, "files.rename", path)


@bp.post("/move")
@login_required
def move():
    """Move one or more entries into another directory.

    Takes a list even for a single entry: the row menu and the bulk bar then
    speak to the same endpoint, and there is no second code path that could
    check the target differently.
    """
    payload = request.get_json(silent=True) or {}
    paths = [str(item) for item in payload.get("paths", []) if str(item).strip()]
    target = payload.get("target", "")
    if not paths:
        return jsonify(ok=False, error="Nothing was selected."), 400

    service = _service()
    moved, failed = [], []
    for path in paths:
        try:
            moved.append(service.move(path, target))
            record("files.move", path, detail=f"-> {target or '/'}")
        except (FileError, OSError) as exc:
            failed.append(f"{path}: {exc}")
            record("files.move", path, ok=False, detail=str(exc))

    return _bulk_answer(moved, failed, paths[0].rpartition("/")[0], "moved")


@bp.post("/bulk-delete")
@login_required
def bulk_delete():
    payload = request.get_json(silent=True) or {}
    paths = [str(item) for item in payload.get("paths", []) if str(item).strip()]
    if not paths:
        return jsonify(ok=False, error="Nothing was selected."), 400

    service = _service()
    done, failed = [], []
    for path in paths:
        try:
            service.delete(path)
            done.append(path)
            record("files.delete", path)
        except (FileError, OSError) as exc:
            failed.append(f"{path}: {exc}")
            record("files.delete", path, ok=False, detail=str(exc))

    return _bulk_answer(done, failed, paths[0].rpartition("/")[0], "deleted")


def _bulk_answer(done: list, failed: list, directory: str, verb: str):
    """One answer shape for operations over a selection.

    Partial success is the normal outcome here - one non-empty directory among
    ten deletions - and reporting it as either "done" or "failed" would be a
    lie in both directions.
    """
    try:
        listing = _service().listing(directory)
    except FileError:
        listing = _service().listing("")

    if failed and not done:
        return jsonify(ok=False, error="; ".join(failed), listing=listing), 400
    return jsonify(ok=True, done=done, verb=verb,
                   error="; ".join(failed), listing=listing)


@bp.get("/download")
@login_required
def download():
    try:
        path = _service().download_path(request.args.get("path", ""))
    except FileError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    return send_file(path, as_attachment=True, download_name=path.name)


def _answer(run, name: str = "", target: str = ""):
    """Run one file operation and report it as JSON.

    400 rather than 403 for a path outside the root: from the browser's point
    of view it is a bad request, and a separate status would tell an attacker
    which of their guesses were paths at all.

    Reads pass no `name` - recording every directory listing would bury the
    handful of entries that say something changed.
    """
    try:
        result = run()
    except FileError as exc:
        if name:
            record(name, target, ok=False, detail=str(exc))
        return jsonify(ok=False, error=str(exc)), 400
    except OSError as exc:
        log.warning("File operation failed: %s", exc)
        if name:
            record(name, target, ok=False, detail=str(exc))
        return jsonify(ok=False, error=f"The filesystem refused that: {exc}"), 500

    payload = {"ok": True}
    payload.update(result if isinstance(result, dict) else {"result": result})
    return jsonify(payload)
