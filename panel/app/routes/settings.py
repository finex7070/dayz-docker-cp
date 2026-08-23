"""Settings - General for the launch parameters, serverDZ for the config file.

Everything that comes from the environment (ports, admin login, proxy trust,
paths) is deliberately not shown here. It cannot be changed without recreating
the container, so a page displaying it would only be a second place to read
values that already live in .env - and a second place to keep in sync.
"""

from __future__ import annotations

import os

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import login_required

from ..services import server_config, serverdz
from ..services.audit import record
from ..services.server_settings import (
    STOP_TIMEOUT_MAX,
    STOP_TIMEOUT_MIN,
    SettingsError,
)

bp = Blueprint("settings", __name__, url_prefix="/settings")

# Launch switches that are on when the checkbox is ticked and off when the
# browser leaves them out of the submission entirely.
_SWITCHES = (
    "do_logs", "admin_log", "net_log", "freeze_check", "file_patching",
    "auto_restart", "auto_update", "auto_mod_update", "rcon_restrict",
)


@bp.get("/")
@login_required
def index():
    settings = current_app.config["SETTINGS"]
    store = current_app.extensions["server_settings"]

    return render_template(
        "settings.html",
        tab=request.args.get("tab", "general"),
        current=store.current,
        missions=settings.paths.available_missions(),
        max_cpu=os.cpu_count() or 1,
        cfg_sections=serverdz.sections_with_values(settings.paths.server_config),
        cfg_exists=settings.paths.server_config.is_file(),
        cfg_form_key=serverdz.FORM_FIELDS_KEY,
        cfg_all_keys=serverdz.ALL_KEYS,
        mod_summary=_mod_summary(store.current),
        stop_timeout_min=STOP_TIMEOUT_MIN,
        stop_timeout_max=STOP_TIMEOUT_MAX,
    )


@bp.post("/general")
@login_required
def save_general():
    """Save the launch parameters.

    Only the fields on this form are touched; the mod lists come from the mods
    page and are written there.
    """
    form = request.form
    changes = {
        "mission": form.get("mission", ""),
        "cpu_count": form.get("cpu_count", ""),
        "limit_fps": form.get("limit_fps", ""),
        "rcon_password": form.get("rcon_password", ""),
        "stop_timeout_seconds": form.get("stop_timeout_seconds", ""),
        **{name: name in form for name in _SWITCHES},
    }

    try:
        saved = current_app.extensions["server_settings"].update(**changes)
    except SettingsError as exc:
        record("settings.general", ok=False, detail=str(exc))
        flash(str(exc), "danger")
        return redirect(url_for("settings.index", tab="general"))

    # The CPU count is two settings in one: -cpuCount on the command line and
    # maxcores in dayzsetting.xml. Written here rather than only before the
    # next start so that the file on disk matches the form the moment it is
    # saved - the operator can open it in the file editor and see it.
    paths = current_app.config["SETTINGS"].paths
    cores_written = server_config.set_max_cores(paths.dayzsetting, saved.cpu_count)

    # The values themselves are not recorded: one of them is the RCON password,
    # and the record is a plain file on the bind mount.
    record("settings.general", detail=", ".join(sorted(changes)))
    message = "Launch parameters saved. They take effect the next time the server starts."
    if cores_written:
        message += f" maxcores in dayzsetting.xml is now {saved.cpu_count}."
    flash(message, "success")
    return redirect(url_for("settings.index", tab="general"))


@bp.post("/serverdz")
@login_required
def save_serverdz():
    settings = current_app.config["SETTINGS"]

    try:
        changed = serverdz.apply(settings.paths.server_config, request.form)
    except serverdz.CfgError as exc:
        record("settings.serverdz", ok=False, detail=str(exc))
        flash(str(exc), "danger")
        return redirect(url_for("settings.index", tab="serverdz"))

    record("settings.serverdz", detail=f"{changed} value(s) written")
    if not changed:
        flash("Nothing changed.", "secondary")
    else:
        flash(
            f"{changed} value(s) written to serverDZ.cfg. "
            "They take effect the next time the server starts.",
            "success",
        )
    return redirect(url_for("settings.index", tab="serverdz"))


def _mod_summary(s) -> dict:
    """What the mod lists currently contribute to the command line."""
    return {
        "client": list(s.client_mods),
        "server": list(s.server_mods),
    }
