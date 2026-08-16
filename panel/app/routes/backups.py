"""Backups - snapshots of /data/server, and what can be done with them.

Everything that changes the repository goes through the job manager, so the
page follows a backup the same way the mods page follows a download. Reading -
the snapshot list, the repository size - happens in the request: restic answers
those in milliseconds from its cache.
"""

from __future__ import annotations

from flask import (
    Blueprint,
    Response,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import login_required

from ..services.audit import record
from ..services.backup import (
    BACKUP_KINDS,
    DOWNLOAD_PARTS,
    BackupError,
    BackupService,
    download_part,
    parse_settings,
)
from ..services.jobs import JobBusy

bp = Blueprint("backups", __name__, url_prefix="/backups")

# Read in 256 KB pieces: large enough that a multi-gigabyte download is not
# millions of iterations, small enough that a client hanging up is noticed.
CHUNK = 256 * 1024


def _service() -> BackupService:
    return current_app.extensions["backup"]


@bp.get("/")
@login_required
def index():
    service = _service()
    # The key file is deliberately not mentioned here. "Keep a copy of this or
    # lose your backups" is something one reads once, while setting the server
    # up - the README is where that belongs, not a line above every snapshot
    # list for the rest of the server's life.
    return render_template(
        "backups.html",
        available=service.available,
        snapshots=service.snapshots(),
        stats=service.stats(),
        error=service.error(),
        current=service.store.current,
        job_kinds=",".join(sorted(BACKUP_KINDS)),
        download_parts=DOWNLOAD_PARTS,
    )


@bp.post("/run")
@login_required
def run():
    """Start a backup. Also the endpoint behind the dashboard button."""
    return _job(lambda: _service().start_backup("manual"), "backups.run")


@bp.post("/<snapshot_id>/restore")
@login_required
def restore(snapshot_id: str):
    return _job(
        lambda: _service().start_restore(snapshot_id), "backups.restore", snapshot_id
    )


@bp.post("/<snapshot_id>/delete")
@login_required
def delete(snapshot_id: str):
    return _job(
        lambda: _service().start_delete(snapshot_id), "backups.delete", snapshot_id
    )


@bp.post("/retention")
@login_required
def retention():
    return _job(lambda: _service().start_retention(), "backups.retention")


@bp.get("/<snapshot_id>/download")
@login_required
def download(snapshot_id: str):
    """Stream a snapshot out as a tar, straight from restic.

    No temporary archive: a snapshot is gigabytes, and building one on disk to
    hand it out would need that space a second time at exactly the moment
    someone is short of it.
    """
    service = _service()
    subpath = (request.args.get("path") or "").strip()

    # Whether this comes out as a tar is decided by the table of offered parts,
    # not by what the query string claims: a single file is handed over as
    # itself, everything else is packed.
    part = download_part(subpath)
    archive = part["archive"] if part else True

    try:
        proc = service.dump(snapshot_id, subpath, archive=archive)
    except BackupError as exc:
        return jsonify(ok=False, error=str(exc)), 400

    def generate():
        try:
            while True:
                chunk = proc.stdout.read(CHUNK)
                if not chunk:
                    break
                yield chunk
        finally:
            # A browser that cancels leaves restic reading a pipe nobody
            # empties - it has to be killed, not waited for.
            proc.stdout.close()
            if proc.poll() is None:
                proc.kill()
            proc.wait()

    stem = f"dayz-{snapshot_id[:8]}"
    if subpath:
        stem += "-" + subpath.strip("/").replace("/", "-")
    filename = f"{stem}.tar" if archive else subpath.strip("/").rpartition("/")[2]

    record("backups.download", snapshot_id[:8], detail=subpath or "everything")
    return Response(
        generate(),
        mimetype="application/x-tar" if archive else "application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            # The size is unknown before restic has finished, so the browser
            # shows a running total instead of a progress bar.
            "X-Accel-Buffering": "no",
        },
    )


@bp.post("/settings")
@login_required
def save_settings():
    """Retention rules and the exclude list. A plain form, not JSON."""
    service = _service()
    try:
        settings = parse_settings(request.form)
    except BackupError as exc:
        record("backups.settings", ok=False, detail=str(exc))
        flash(str(exc), "danger")
        return redirect(url_for("backups.index"))

    service.store.save(settings)
    record("backups.settings", detail=", ".join(
        f"{key}={value}" for key, value in settings.to_dict().items()
    ))

    if settings.has_retention:
        flash(
            "Retention saved. It is applied after the next backup - or now, "
            "with 'Apply retention'.",
            "success",
        )
    else:
        flash("Saved. Without a retention rule no snapshot is ever deleted.", "success")
    return redirect(url_for("backups.index"))


def _job(starter, action: str, target: str = ""):
    try:
        job = starter()
    except BackupError as exc:
        record(action, target, ok=False, detail=str(exc))
        return jsonify(ok=False, error=str(exc)), 400
    except JobBusy as busy:
        record(action, target, ok=False, detail=f"busy: {busy.active.title}")
        return jsonify(
            ok=False,
            error=f"Another job is already running: {busy.active.title}",
            job_id=busy.active.id,
        ), 409

    record(action, target, detail=f"started: {job.title}")
    return jsonify(ok=True, job_id=job.id)
