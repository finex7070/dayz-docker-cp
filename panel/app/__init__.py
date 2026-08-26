"""Flask application factory.

Seven pages: dashboard (status, controls, live console), logs, settings, mods,
schedules, files and backups. The services behind them are module level
singletons, which only holds because the panel runs a single gunicorn worker -
see gunicorn.conf.py.
"""

from __future__ import annotations

import atexit
import logging
import os
import platform
import shutil
import threading
import time
from datetime import timedelta

from flask import Flask, has_request_context, jsonify, render_template, request
from flask.sessions import SecureCookieSessionInterface

from . import auth
from .config import Settings
from .extensions import csrf, limiter, login_manager
from .proxy import TrustedProxyFix, parse_trusted
from .routes import backups, console, dashboard, files, jobs as jobs_routes, logs, mods
from .routes import schedules, server
from .routes import settings as settings_routes
from .services.jobs import JobState
from .services.audit import AuditLog
from .services.backup import BackupService, BackupStore
from .services.files import FileService
from .services.logs import LogService
from .services.mods import ModService
from .services.query import QueryService
from .services.rcon import RconService
from .services.schedules import ScheduleService, ScheduleStore, make_runner
from .services.server import ServerError, ServerManager, ServerState
from .services.server_settings import SettingsStore
from .services.startup import StartupSequence
from .services.steamcmd import SteamCmdService

__version__ = "1.2.4"

STARTED_AT = time.time()

log = logging.getLogger(__name__)

_BLUEPRINTS = (
    auth.bp,
    dashboard.bp,
    jobs_routes.bp,
    server.bp,
    console.bp,
    logs.bp,
    settings_routes.bp,
    mods.bp,
    schedules.bp,
    files.bp,
    backups.bp,
)


class AdaptiveSessionInterface(SecureCookieSessionInterface):
    """Session cookie whose Secure flag can follow the actual request scheme.

    With a hard-coded Secure=True the cookie is dropped over plain HTTP, which
    breaks sign-in on a local `http://host:8080` setup. With a hard-coded False
    the cookie travels unprotected behind a TLS proxy. "auto" resolves this per
    request -- correct in both deployments, provided TRUSTED_PROXY_IPS names the
    proxy so that request.is_secure reflects X-Forwarded-Proto.
    """

    def get_cookie_secure(self, app: Flask) -> bool:
        mode = app.config.get("SESSION_COOKIE_SECURE_MODE", "auto")
        if mode == "auto":
            return has_request_context() and request.is_secure
        return mode == "true"


def create_app() -> Flask:
    logging.basicConfig(
        level=os.environ.get("PANEL_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    settings = Settings.load()

    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=settings.secret_key,
        SETTINGS=settings,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE_MODE=settings.session_cookie_secure,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=settings.session_lifetime_hours),
        WTF_CSRF_TIME_LIMIT=None,  # tie CSRF validity to the session, not a timer
        # Bounds every request body, not just the files page - which is the
        # point: it is the only cap on what a logged-in session can push into
        # the container.
        MAX_CONTENT_LENGTH=settings.max_upload_mb * 1024 * 1024,
    )
    app.session_interface = AdaptiveSessionInterface()

    _apply_proxy_fix(app, settings.trusted_proxy_ips)

    csrf.init_app(app)
    limiter.init_app(app)
    login_manager.init_app(app)
    auth.init_credentials(app, settings.admin_username, settings.admin_password)

    audit = AuditLog(settings.paths.panel / "audit.log")
    app.extensions["audit"] = audit

    store = SettingsStore(settings.paths.panel / "server_settings.json")
    manager = ServerManager(settings, store)
    steamcmd = SteamCmdService(settings)
    app.extensions["steamcmd"] = steamcmd
    app.extensions["server_settings"] = store
    app.extensions["server"] = manager
    # Writes the mod directories into the launch parameters as it starts, so a
    # mod removed from /data by hand cannot leave a dangling -mod= entry.
    mods = ModService(settings, steamcmd, store)
    app.extensions["mods"] = mods
    app.extensions["startup"] = StartupSequence(settings, store, steamcmd, mods, manager)
    app.extensions["logs"] = LogService(settings)
    # Rooted at the server directory: everything worth editing is below it, and
    # a root any higher would put the panel's own settings and the Steam
    # sentry file within reach of a text editor in a browser.
    app.extensions["files"] = FileService(settings.paths.server)
    # 127.0.0.1: the query goes to the server in this very container, never out.
    app.extensions["query"] = QueryService("127.0.0.1", settings.steam_query_port)

    # Only a running server answers on the RCON port - BattlEye is not up while
    # the process is still starting, and taking it down is the first thing a
    # stop does. `manager.running` covers all three states, hence the narrower
    # test here: it is what decides whether the dashboard offers the buttons.
    rcon = RconService(
        "127.0.0.1",
        settings.rcon_port,
        store,
        is_up=lambda: manager.state is ServerState.RUNNING,
        # Anything the server says while a command is in flight lands in the
        # same buffer as its own output: for the operator watching the console,
        # a kick and the line that caused it are one story.
        on_message=manager.console_note,
    )
    app.extensions["rcon"] = rcon

    # The manager is handed in so a backup can tell a running server (the
    # snapshot is then tagged "hot") and a restore can stop it first.
    backup = BackupService(
        settings, BackupStore(settings.paths.panel / "backup.json"), manager=manager
    )
    app.extensions["backup"] = backup
    # Reading the repository costs about a second (restic derives its key with
    # scrypt on every call), so it happens once here instead of in the first
    # request for the Backups page.
    backup.warm()

    sequence = app.extensions["startup"]
    schedules_service = ScheduleService(
        ScheduleStore(settings.paths.panel / "schedules.json"),
        make_runner(sequence, manager, rcon, backup),
        audit=audit,
        # A server on its way up or down counts as started: an entry that skips
        # while the server is stopped is about there being nothing to act on,
        # and during those two states there is.
        server_up=lambda: manager.running,
    )
    app.extensions["schedules"] = schedules_service
    schedules_service.start()

    # The container stopping must not leave the DayZ process for Docker to
    # kill: it writes persistence on the way out. Gunicorn's worker exits
    # normally on SIGTERM, which is what runs this.
    atexit.register(manager.shutdown)
    atexit.register(schedules_service.shutdown)

    for blueprint in _BLUEPRINTS:
        app.register_blueprint(blueprint)

    _register_healthz(app)
    _register_error_handlers(app)
    _register_template_globals(app, settings)

    log_boot_summary(settings)
    _run_startup_automation(app, settings)
    return app


def _run_startup_automation(app: Flask, settings: Settings) -> None:
    """What the container does on boot: install the files, then maybe start.

    Only these two come from the environment, because both describe how this
    container is meant to come up. Whether server files and mods are updated
    is decided per server start and lives in StartupSequence - see
    services/startup.py.

    The install is deliberately a job rather than something the entrypoint
    does: a failing download must not turn into a container boot loop, and the
    operator needs to see the output and answer a Steam Guard prompt.
    """
    sequence: StartupSequence = app.extensions["startup"]

    if not (settings.auto_install or settings.auto_start):
        return

    def boot() -> None:
        job = sequence.install_if_missing()
        if job is not None:
            job.done.wait()
            if job.state is not JobState.SUCCESS:
                log.warning("AUTO_INSTALL ended as %s - not starting", job.state.value)
                return

        if not settings.auto_start:
            return

        try:
            log.info("AUTO_START: %s", sequence.start_server(reason="AUTO_START"))
        except (ServerError, OSError) as exc:
            # Never fatal: the panel is the tool for fixing whatever went wrong.
            log.warning("AUTO_START could not start the server: %s", exc)

    threading.Thread(target=boot, name="startup-automation", daemon=True).start()


def _apply_proxy_fix(app: Flask, trusted_ips: tuple[str, ...]) -> None:
    """Honour X-Forwarded-* headers, but only from the configured proxies."""
    networks, trust_all = parse_trusted(trusted_ips)

    middleware = TrustedProxyFix(app.wsgi_app, networks, trust_all)
    if not middleware.enabled:
        log.info("Reverse proxy: forwarded headers ignored (TRUSTED_PROXY_IPS is empty)")
        return

    app.wsgi_app = middleware
    if trust_all:
        log.warning(
            "Reverse proxy: trusting forwarded headers from ANY peer - only safe "
            "when nothing but the proxy can reach the panel's port"
        )
    else:
        log.info(
            "Reverse proxy: trusting forwarded headers from %s",
            ", ".join(str(network) for network in networks),
        )


def _register_healthz(app: Flask) -> None:
    @app.get("/healthz")
    @limiter.exempt
    def healthz():
        """Polled by the Docker HEALTHCHECK - deliberately unauthenticated.

        Independent of the DayZ server: a stopped server is a valid state and
        must not mark the container as unhealthy.
        """
        return jsonify(
            status="ok",
            version=__version__,
            uptime_seconds=round(time.time() - STARTED_AT, 1),
        )


def _register_error_handlers(app: Flask) -> None:
    @app.errorhandler(404)
    def not_found(_error):
        return render_template("error.html", code=404, message="Page not found."), 404

    @app.errorhandler(429)
    def too_many_requests(_error):
        return (
            render_template(
                "error.html",
                code=429,
                message="Too many attempts. Please wait a moment and try again.",
            ),
            429,
        )

    @app.errorhandler(413)
    def too_large(_error):
        """Uploads are the only way to hit this, and they expect JSON."""
        limit = app.config["SETTINGS"].max_upload_mb
        message = f"That upload is larger than {limit} MB (MAX_UPLOAD_MB in .env)."
        if request.accept_mimetypes.best == "application/json":
            return jsonify(ok=False, error=message), 413
        return render_template("error.html", code=413, message=message), 413

    @app.errorhandler(500)
    def internal_error(error):
        log.exception("Unhandled error: %s", error)
        return render_template("error.html", code=500, message="Internal server error."), 500


def _register_template_globals(app: Flask, settings: Settings) -> None:
    @app.template_filter("timestamp")
    def format_timestamp(value: float) -> str:
        """Unix time as local time. The container's clock is the server's clock."""
        if not value:
            return "-"
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(value))

    @app.template_filter("filesize")
    def format_filesize(value: float | None) -> str:
        """Bytes in the unit a person would use. None reads as a dash."""
        if value is None:
            return "-"
        size = float(value)
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024:
                return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"

    @app.context_processor
    def inject_globals():
        return {
            "panel_version": __version__,
            "settings": settings,
            "nav_items": NAV_ITEMS,
        }


# Navigation shown in the sidebar. `phase` marks entries whose page is still a
# placeholder, so the UI can say so instead of pretending to work.
NAV_ITEMS = (
    {"endpoint": "dashboard.index", "label": "Dashboard", "phase": None},
    {"endpoint": "logs.index", "label": "Logs", "phase": None},
    {"endpoint": "settings.index", "label": "Settings", "phase": None},
    {"endpoint": "mods.index", "label": "Mods", "phase": None},
    {"endpoint": "schedules.index", "label": "Schedules", "phase": None},
    {"endpoint": "files.index", "label": "Files", "phase": None},
    {"endpoint": "backups.index", "label": "Backups", "phase": None},
)


def environment_checks(settings: Settings) -> list[dict]:
    """Environment overview for the dashboard and the startup log."""
    steamcmd_path = shutil.which("steamcmd")
    sdk_lib = settings.paths.steam / ".steam/sdk64/steamclient.so"

    return [
        {
            "name": "SteamCMD available",
            "ok": bool(steamcmd_path),
            "detail": steamcmd_path or "not found",
        },
        {
            "name": "Steam SDK (sdk64/steamclient.so)",
            "ok": sdk_lib.exists(),
            "detail": "present" if sdk_lib.exists() else "created after the first SteamCMD run",
        },
        {
            "name": "Steam account",
            "ok": settings.steam_credentials_set,
            "detail": "set" if settings.steam_credentials_set
            else "STEAM_USERNAME missing",
        },
        {
            "name": "Server files installed",
            "ok": settings.server_installed,
            "detail": str(settings.paths.server_binary) if settings.server_installed
            else "not installed yet (phase 3: install via the panel)",
        },
        {
            "name": "Data directory writable",
            "ok": os.access(settings.paths.data, os.W_OK),
            "detail": str(settings.paths.data),
        },
    ]


def log_boot_summary(settings: Settings) -> None:
    log.info("DayZ control panel %s", __version__)
    log.info("Python %s on %s", platform.python_version(), platform.platform())
    log.info("Data directory: %s", settings.paths.data)
    log.info("Steam home:     %s", settings.paths.steam)
    log.info("SteamCMD:       %s", shutil.which("steamcmd") or "NOT FOUND")
    log.info("Steam login:    %s", "set" if settings.steam_credentials_set else "missing")
    log.info("Server files:   %s", "present" if settings.server_installed else "not installed")
    log.info("Admin user:     %s", settings.admin_username)

    for check in environment_checks(settings):
        if not check["ok"]:
            log.warning("Pending: %s (%s)", check["name"], check["detail"])
