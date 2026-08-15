"""Authentication: a single administrator account taken from the environment.

There is no user database. The panel controls exactly one DayZ server and is
operated by whoever runs the container, so ADMIN_USERNAME/ADMIN_PASSWORD are
enough. The password is hashed once at startup; the clear text value is never
stored on the user object, written to the session, or rendered anywhere.
"""

from __future__ import annotations

import hmac
import logging

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import UserMixin, current_user, login_required, login_user, logout_user
from flask_wtf import FlaskForm
from werkzeug.security import check_password_hash, generate_password_hash
from wtforms import BooleanField, PasswordField, StringField
from wtforms.validators import DataRequired, Length

from .extensions import limiter, login_manager
from .services.audit import record

log = logging.getLogger(__name__)

bp = Blueprint("auth", __name__)

# Flask-Login needs a stable identifier. There is only ever one account.
ADMIN_USER_ID = "admin"


class AdminUser(UserMixin):
    def __init__(self, username: str) -> None:
        self.id = ADMIN_USER_ID
        self.username = username


class LoginForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired(), Length(max=128)])
    password = PasswordField("Password", validators=[DataRequired(), Length(max=256)])
    remember = BooleanField("Stay signed in")


@login_manager.user_loader
def load_user(user_id: str) -> AdminUser | None:
    if user_id != ADMIN_USER_ID:
        return None
    return AdminUser(current_app.config["ADMIN_USERNAME"])


def init_credentials(app, username: str, password: str) -> None:
    """Hash the configured password once, at application start."""
    app.config["ADMIN_USERNAME"] = username
    app.config["ADMIN_PASSWORD_HASH"] = generate_password_hash(password)


def _credentials_valid(username: str, password: str) -> bool:
    # compare_digest on the username avoids leaking its length through timing,
    # and check_password_hash always runs so that a wrong username costs the
    # same as a wrong password.
    expected_user = current_app.config["ADMIN_USERNAME"]
    user_ok = hmac.compare_digest(username, expected_user)
    password_ok = check_password_hash(current_app.config["ADMIN_PASSWORD_HASH"], password)
    return user_ok and password_ok


@bp.route("/login", methods=["GET", "POST"])
@limiter.limit(
    "10 per minute; 60 per hour",
    methods=["POST"],
    # Successful sign-ins should not count towards the limit.
    deduct_when=lambda response: response.status_code != 302,
)
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))

    form = LoginForm()
    if form.validate_on_submit():
        if _credentials_valid(form.username.data or "", form.password.data or ""):
            login_user(AdminUser(form.username.data), remember=bool(form.remember.data))
            log.info("Sign-in succeeded for %r from %s", form.username.data, request.remote_addr)
            record("auth.login", form.username.data or "")
            return redirect(_safe_next_target())

        log.warning("Sign-in failed for %r from %s", form.username.data, request.remote_addr)
        # Failed attempts are the entries worth having: a run of them from one
        # address is the only sign the panel gives that someone is trying.
        record("auth.login", form.username.data or "", ok=False, detail="wrong credentials")
        flash("Invalid username or password.", "danger")

    return render_template("login.html", form=form)


@bp.post("/logout")
@login_required
def logout():
    record("auth.logout", current_user.get_id() or "")
    logout_user()
    flash("You have been signed out.", "success")
    return redirect(url_for("auth.login"))


def _safe_next_target() -> str:
    """Only follow relative ?next targets - never an absolute URL.

    Without this check an attacker could craft /login?next=https://evil.example
    and use the panel as an open redirect.
    """
    target = request.args.get("next", "")
    if target.startswith("/") and not target.startswith("//"):
        return target
    return url_for("dashboard.index")
