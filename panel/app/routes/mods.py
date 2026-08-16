"""Mod management - install, update and remove Steam Workshop mods.

Downloads are jobs, for the same reason SteamCMD runs are: a mod can be several
gigabytes, and a request that waits for it is a request that times out. The page
follows the job's output the way the dashboard does.
"""

from __future__ import annotations

from flask import (
    Blueprint,
    Response,
    current_app,
    jsonify,
    render_template,
    request,
)
from flask_login import login_required

from ..services.audit import record
from ..services.jobs import MOD_KINDS, JobBusy
from ..services.mods import (
    MOD_TYPES,
    ModError,
    ModService,
    lookup_workshop_item,
    parse_workshop_id,
    search_workshop,
)

bp = Blueprint("mods", __name__, url_prefix="/mods")

# A launcher preset with a hundred mods is under 50 KB. Anything past this is
# the wrong file, and reading it into memory to find that out is the mistake.
MAX_MODLIST_BYTES = 1024 * 1024


def _service() -> ModService:
    return current_app.extensions["mods"]


@bp.get("/")
@login_required
def index():
    settings = current_app.config["SETTINGS"]
    service = _service()

    return render_template(
        "mods.html",
        job_kinds=",".join(sorted(MOD_KINDS)),
        mods=service.list_mods(),
        orphans=service.orphan_dirs(),
        search_enabled=bool(settings.steam_api_key),
        mod_types=MOD_TYPES,
        server_running=current_app.extensions["server"].running,
    )


@bp.get("/list.json")
@login_required
def list_json():
    return jsonify(ok=True, mods=_service().list_mods())


@bp.post("/lookup")
@login_required
def lookup():
    """Resolve an ID or URL to a title before anything is downloaded."""
    settings = current_app.config["SETTINGS"]
    payload = request.get_json(silent=True) or {}

    try:
        workshop_id = parse_workshop_id(payload.get("query", ""))
        item = lookup_workshop_item(workshop_id, settings.WORKSHOP_APP_ID)
    except ModError as exc:
        return jsonify(ok=False, error=str(exc)), 400

    item["installed"] = _service().registry.get(workshop_id) is not None
    return jsonify(ok=True, item=item)


@bp.get("/search")
@login_required
def search():
    settings = current_app.config["SETTINGS"]
    query = (request.args.get("q") or "").strip()
    page = request.args.get("page", type=int, default=1)

    try:
        result = search_workshop(
            query, settings.steam_api_key, settings.WORKSHOP_APP_ID, page=page
        )
    except ModError as exc:
        return jsonify(ok=False, error=str(exc)), 400

    known = {mod["workshop_id"] for mod in _service().list_mods()}
    for item in result["items"]:
        item["installed"] = item["workshop_id"] in known
    return jsonify(ok=True, **result)


@bp.post("/install")
@login_required
def install():
    payload = request.get_json(silent=True) or {}
    mod_type = payload.get("mod_type") or "client"

    try:
        workshop_id = parse_workshop_id(payload.get("query", ""))
        return _job(lambda: _service().install(workshop_id, mod_type))
    except ModError as exc:
        return jsonify(ok=False, error=str(exc)), 400


@bp.post("/import")
@login_required
def import_list():
    """Take over a DayZ Launcher preset, as client mods."""
    uploaded = request.files.get("modlist")
    if uploaded is None or not uploaded.filename:
        return jsonify(ok=False, error="No file was selected."), 400

    raw = uploaded.read(MAX_MODLIST_BYTES + 1)
    if len(raw) > MAX_MODLIST_BYTES:
        return jsonify(
            ok=False,
            error="That file is far larger than a mod list - is it the right one?",
        ), 400

    name = uploaded.filename
    try:
        # utf-8-sig: the launcher writes a byte order mark, and a stray BOM in
        # front of the XML declaration is the kind of thing a parser trips on.
        result = _service().import_modlist(raw.decode("utf-8-sig", errors="replace"))
    except ModError as exc:
        record("mods.import", name, ok=False, detail=str(exc))
        return jsonify(ok=False, error=str(exc)), 400
    except JobBusy as busy:
        record("mods.import", name, ok=False, detail=f"busy: {busy.active.title}")
        return jsonify(
            ok=False,
            error=f"Another job is already running: {busy.active.title}",
            job_id=busy.active.id,
        ), 409

    detail = f"{result['added']} new, {result['skipped']} already installed"
    record("mods.import", name, detail=detail)
    return jsonify(ok=True, job_id=result["job"].id, message=detail)


@bp.get("/export")
@login_required
def export_list():
    """All mods as a launcher preset, for handing to players."""
    service = _service()
    try:
        body = service.export_modlist()
    except ModError as exc:
        return jsonify(ok=False, error=str(exc)), 400

    enabled = [mod for mod in service.registry.all() if mod.enabled]
    record("mods.export", f"{len(enabled)} mods")
    return Response(
        body,
        mimetype="text/html",
        headers={"Content-Disposition": 'attachment; filename="modlist.html"'},
    )


@bp.post("/<int:workshop_id>/update")
@login_required
def update(workshop_id: int):
    blocked = _running_server_warning("update")
    if blocked:
        return blocked
    return _job(lambda: _service().update([workshop_id]))


@bp.post("/update-all")
@login_required
def update_all():
    blocked = _running_server_warning("update")
    if blocked:
        return blocked

    service = _service()
    if not service.registry.all():
        return jsonify(ok=False, error="No mods are installed."), 400
    return _job(service.update_all)


@bp.post("/<int:workshop_id>/reinstall")
@login_required
def reinstall(workshop_id: int):
    blocked = _running_server_warning("reinstall")
    if blocked:
        return blocked
    return _job(lambda: _service().reinstall(workshop_id))


@bp.post("/<int:workshop_id>/delete")
@login_required
def delete(workshop_id: int):
    blocked = _running_server_warning("delete")
    if blocked:
        return blocked

    keep = bool((request.get_json(silent=True) or {}).get("keep_download", True))
    try:
        mod = _service().delete(workshop_id, keep_download=keep)
    except ModError as exc:
        record("mods.delete", str(workshop_id), ok=False, detail=str(exc))
        return jsonify(ok=False, error=str(exc)), 404

    record("mods.delete", f"{workshop_id} {mod.name}")
    return jsonify(ok=True, message=f"{mod.name} removed.")


@bp.post("/<int:workshop_id>/type")
@login_required
def set_type(workshop_id: int):
    payload = request.get_json(silent=True) or {}
    try:
        mod = _service().set_type(workshop_id, payload.get("mod_type", ""))
    except ModError as exc:
        record("mods.type", str(workshop_id), ok=False, detail=str(exc))
        return jsonify(ok=False, error=str(exc)), 400

    record("mods.type", f"{workshop_id} {mod.name}", detail=mod.mod_type)
    return jsonify(
        ok=True,
        mod_type=mod.mod_type,
        message=(
            f"{mod.name} is now a client mod - its key was installed."
            if mod.is_client
            else f"{mod.name} is now a server mod - its key was removed."
        ),
    )


@bp.post("/<int:workshop_id>/enabled")
@login_required
def set_enabled(workshop_id: int):
    payload = request.get_json(silent=True) or {}
    try:
        mod = _service().set_enabled(workshop_id, bool(payload.get("enabled")))
    except ModError as exc:
        record("mods.enabled", str(workshop_id), ok=False, detail=str(exc))
        return jsonify(ok=False, error=str(exc)), 400

    record("mods.enabled", f"{workshop_id} {mod.name}",
           detail="enabled" if mod.enabled else "disabled")
    return jsonify(ok=True, enabled=mod.enabled)


@bp.post("/<int:workshop_id>/move")
@login_required
def move(workshop_id: int):
    """Shift a mod up or down the load order."""
    payload = request.get_json(silent=True) or {}
    offset = -1 if payload.get("direction") == "up" else 1
    try:
        _service().move(workshop_id, offset)
    except ModError as exc:
        return jsonify(ok=False, error=str(exc)), 404
    return jsonify(ok=True)


def _job(starter):
    # The endpoint name is the action name: install, update, reinstall,
    # update_all. Naming them twice would be one more thing to keep in step.
    action = f"mods.{(request.endpoint or '').rpartition('.')[2]}"
    target = str(request.view_args.get("workshop_id", "")) if request.view_args else ""

    try:
        job = starter()
    except ModError as exc:
        record(action, target, ok=False, detail=str(exc))
        return jsonify(ok=False, error=str(exc)), 400
    except JobBusy as busy:
        record(action, target, ok=False, detail=f"busy: {busy.active.title}")
        return jsonify(
            ok=False,
            error=f"Another job is already running: {busy.active.title}",
            job_id=busy.active.id,
        ), 409

    if job is None:
        return jsonify(ok=False, error="Nothing to do."), 400

    # Recorded as started, not as finished: the job runs in the background, and
    # its outcome is in the job output.
    record(action, target, detail=f"started: {job.title}")
    return jsonify(ok=True, job_id=job.id)


def _running_server_warning(action: str):
    """Refuse to change mod files under a running server, unless forced.

    The server keeps the PBOs of a loaded mod open. Replacing them underneath it
    is the kind of fault that shows up much later as a crash, so the operator
    has to see the warning before insisting.
    """
    payload = request.get_json(silent=True) or {}
    if payload.get("force"):
        return None

    if not current_app.extensions["server"].running:
        return None

    return jsonify(
        ok=False,
        needs_confirm=True,
        error=(
            f"The DayZ server is running and has these files open. A mod "
            f"{action} while it runs can corrupt the load. Stop the server "
            f"first - or continue anyway?"
        ),
    ), 409
