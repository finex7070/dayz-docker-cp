"""Flask extension instances.

Kept in their own module so blueprints can import them without importing the
application factory (which would be a circular import).
"""

from __future__ import annotations

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect

csrf = CSRFProtect()

login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message = "Please sign in to continue."
login_manager.login_message_category = "warning"
login_manager.session_protection = "strong"

# In-memory storage is correct here: the panel deliberately runs a single
# gunicorn worker (see gunicorn.conf.py), so there is exactly one counter.
limiter = Limiter(
    key_func=get_remote_address,
    storage_uri="memory://",
    strategy="fixed-window",
)
