"""Gunicorn configuration for the control panel.

IMPORTANT -- workers = 1 is not a performance decision, it is mandatory:
the panel keeps the DayZ server process (PID, Popen handle), the log ring
buffers and the SteamCMD job state in memory. With multiple workers each worker
would hold its own state -- requests would return contradictory answers
depending on which worker handled them, and the server process could end up
orphaned. Scaling happens through threads instead.
"""

import logging
import os

bind = f"0.0.0.0:{os.environ.get('PANEL_PORT', '8080')}"

# --- Process model ---------------------------------------------------------
workers = 1
worker_class = "gthread"

# Threads serve concurrent requests AND open SSE log streams. Every active
# stream occupies a thread for its whole lifetime, hence the generous default;
# the panel additionally caps concurrent streams at application level.
threads = int(os.environ.get("PANEL_THREADS", "16"))

# --- Reverse proxy ---------------------------------------------------------
# Disable gunicorn's own X-Forwarded-Proto handling. By default gunicorn
# rewrites wsgi.url_scheme to https whenever such a header arrives from
# forwarded_allow_ips (127.0.0.1 by default) -- which happens behind the panel's
# back and independently of TRUSTED_PROXY_IPS. The result was a request that
# claimed to be HTTPS while an empty TRUSTED_PROXY_IPS promised forwarded
# headers were ignored, which in turn made Flask-WTF's SSL-strict CSRF check
# reject the request. Forwarded headers are handled in exactly one place:
# TrustedProxyFix, driven by TRUSTED_PROXY_IPS (see app/proxy.py).
secure_scheme_headers = {}
forwarded_allow_ips = ""

# --- Timeouts --------------------------------------------------------------
# gthread workers notify the arbiter independently of running requests, so long
# lived SSE connections do not trigger a worker restart.
timeout = int(os.environ.get("PANEL_TIMEOUT", "120"))

# Long enough for the worker to shut the DayZ server down on the way out
# (SIGTERM, up to 30s, then SIGKILL - see services/server.py). Below that,
# gunicorn would kill the worker mid-shutdown and leave the DayZ process
# orphaned, writing its persistence into a container that is going away.
# Stays under the compose stop_grace_period of 60s.
graceful_timeout = 50
keepalive = 5

# --- Logging ---------------------------------------------------------------
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("PANEL_LOG_LEVEL", "info")
access_log_format = '%(h)s "%(r)s" %(s)s %(b)s %(D)sus'


class _SuppressHealthcheck(logging.Filter):
    """The Docker healthcheck polls /healthz every 30s - that floods the log."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "/healthz" not in record.getMessage()


def post_fork(server, worker):
    logging.getLogger("gunicorn.access").addFilter(_SuppressHealthcheck())


def on_starting(server):
    server.log.info("Control panel starting on %s (workers=%s, threads=%s)", bind, workers, threads)
