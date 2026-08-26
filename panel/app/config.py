"""Configuration loaded from environment variables.

Single source of truth for every env value. Other modules do NOT read from
os.environ directly, they use `Settings.load()`.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number, got: {raw!r}") from None


def _env_port_range(name: str, default: int) -> int:
    """The game port out of a published range like "2302-2304".

    SERVER_PORT carries the whole range because docker-compose.yml publishes it
    verbatim and Compose cannot add - see the comment there. What the server
    needs is the first port of it, the one it binds and reports to Steam.
    A bare number is the older form; entrypoint.sh refuses it before the panel
    ever starts, so accepting it here only keeps a debug run working.
    """
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    first = raw.split("-", 1)[0].strip()
    try:
        return int(first)
    except ValueError:
        raise ValueError(
            f"{name} must be a port range like 2302-2304, got: {raw!r}"
        ) from None


def normalize_mod_name(name: str) -> str:
    """Turn a workshop title into a directory-safe name.

    Lowercase, spaces to underscores. Linux is case sensitive and DayZ mods are
    routinely referenced with different casing than the workshop title uses, so
    normalising once removes a whole class of "mod not found" errors.
    Characters that would break a path are replaced as well.
    """
    cleaned = []
    for char in name.strip().lower():
        if char.isalnum() or char in "._-":
            cleaned.append(char)
        else:
            # Spaces and anything path-unsafe (/, \, :, quotes, ...) collapse
            # into the same separator.
            cleaned.append("_")

    # Collapse runs of underscores and trim them off the ends.
    result = "_".join(part for part in "".join(cleaned).split("_") if part)
    return result or "mod"


def _env_list(name: str) -> tuple[str, ...]:
    """Comma or whitespace separated list, empty entries dropped."""
    raw = (os.environ.get(name) or "").replace(",", " ")
    return tuple(part for part in raw.split() if part)


def _looks_like_comment(value: str) -> bool:
    """Detect a comment that leaked into a value.

    Docker Compose only strips a trailing "# ..." from a NON-empty value in an
    env_file. Written as `FOO=   # explanation`, an empty FOO ends up holding
    the comment text - which then silently becomes a Steam Guard code or, worse,
    a session key that is a publicly known constant from the template.
    """
    stripped = value.strip()
    return stripped.startswith("#") and " " in stripped


def _env_guard_code(name: str) -> str:
    """Steam Guard codes are exactly five alphanumeric characters.

    Anything else is rejected rather than handed to SteamCMD: passing a bogus
    third login argument makes Steam reject the whole login with a misleading
    "Invalid Password".
    """
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return ""
    if re.fullmatch(r"[A-Za-z0-9]{4,8}", raw):
        return raw.upper()

    log.warning(
        "%s does not look like a Steam Guard code and is ignored%s",
        name,
        " (it contains a comment - move comments to their own line in .env)"
        if _looks_like_comment(raw) else "",
    )
    return ""


def _env_secret_key(name: str) -> str:
    raw = (os.environ.get(name) or "").strip()
    if _looks_like_comment(raw):
        log.warning(
            "%s contains a comment instead of a key and is ignored - a generated "
            "key is used instead. Move comments to their own line in .env.",
            name,
        )
        return ""
    return raw


def _env_plain(name: str) -> str:
    """A value that must never be a leaked comment - see _looks_like_comment."""
    raw = (os.environ.get(name) or "").strip()
    if _looks_like_comment(raw):
        log.warning(
            "%s contains a comment instead of a value and is ignored - move "
            "comments to their own line in .env.",
            name,
        )
        return ""
    return raw


def _env_choice(name: str, default: str, allowed: set[str]) -> str:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    if raw not in allowed:
        raise ValueError(f"{name} must be one of {sorted(allowed)}, got: {raw!r}")
    return raw


@dataclass(frozen=True)
class Paths:
    """Directories inside the /data volume.

    Four top level directories: panel, steam, server and backup. Everything the
    DayZ server touches lives under `server/` -- including profiles and mods, so
    they can be passed to the server as paths relative to its install directory.
    """

    data: Path
    panel: Path
    steam: Path
    server: Path
    backup: Path

    @classmethod
    def from_env(cls) -> "Paths":
        data = Path(os.environ.get("DATA_DIR", "/data"))
        return cls(
            data=data,
            panel=data / "panel",
            steam=Path(os.environ.get("STEAM_HOME", str(data / "steam"))),
            server=data / "server",
            backup=data / "backup",
        )

    @property
    def profiles(self) -> Path:
        """Server profile directory: *.RPT, *.ADM and script logs."""
        return self.server / "profiles"

    @property
    def workshop(self) -> Path:
        """Where SteamCMD drops workshop downloads, in their raw state.

        Mods are copied from here into the server directory rather than moved:
        keeping this cache intact is what lets a later workshop_download_item
        update incrementally instead of fetching everything again.
        """
        return self.steam / "steamapps/workshop/content" / str(Settings.WORKSHOP_APP_ID)

    def mod_dir(self, workshop_id: int | str, mod_name: str) -> Path:
        """Install directory of a mod: /data/server/@<id>_<name>.

        Mods live directly beside the server binary, so -mod= can reference
        them by plain name. The workshop ID keeps the directory unique even
        when two mods share a title.
        """
        return self.server / f"@{workshop_id}_{normalize_mod_name(mod_name)}"

    def installed_mod_dirs(self) -> list[Path]:
        return sorted(p for p in self.server.glob("@*") if p.is_dir())

    @property
    def mpmissions(self) -> Path:
        return self.server / "mpmissions"

    def available_missions(self) -> list[str]:
        """Mission folders the server could be pointed at.

        Offered as suggestions rather than as the only choice: a mission can be
        added while the panel is running, and the server files may not be
        installed yet when the settings page is first opened.
        """
        if not self.mpmissions.is_dir():
            return []
        return sorted(p.name for p in self.mpmissions.iterdir() if p.is_dir())

    @property
    def battleye(self) -> Path:
        """-BEpath: the BattlEye directory that ships with the server files.

        Deliberately the server's own battleye/ folder, not one under profiles/:
        it holds beserver_x64.so, which BattlEye loads from this path. Pointing
        -BEpath at an empty directory leaves the server without its anti-cheat.
        """
        return self.server / "battleye"

    @property
    def battleye_config(self) -> Path:
        """RConPort and RConPassword live here.

        The server copies this file to beserver_x64_active_<random>.cfg on
        startup and uses the copy, so stale copies must be removed whenever the
        file is rewritten - otherwise the old password stays in effect.
        """
        return self.battleye / "beserver_x64.cfg"

    def stale_battleye_configs(self) -> list[Path]:
        return sorted(self.battleye.glob("beserver_x64_active_*.cfg"))

    @property
    def keys(self) -> Path:
        """Mod signature keys the server accepts."""
        return self.server / "keys"

    @property
    def server_binary(self) -> Path:
        return self.server / "DayZServer"

    @property
    def server_config(self) -> Path:
        return self.server / "serverDZ.cfg"

    @property
    def dayzsetting(self) -> Path:
        """Engine settings that ship with the server files.

        Mostly a game client's video options, but the <jobsystem> block in it
        is what sizes the server's worker threads - see set_max_cores().
        """
        return self.server / "dayzsetting.xml"

    @property
    def database(self) -> Path:
        return self.panel / "panel.db"

    @property
    def secret_key_file(self) -> Path:
        return self.panel / "secret_key"


@dataclass(frozen=True)
class Settings:
    # --- Panel ---
    admin_username: str
    admin_password: str
    secret_key: str
    panel_port: int

    # --- Steam ---
    steam_username: str
    steam_password: str
    steam_guard_code: str
    # Optional. Only the workshop search needs it - installing by ID does not.
    steam_api_key: str

    # --- DayZ server ---
    # Ports only. Everything else about the server (cpuCount, mission, log
    # switches, ...) is edited on the settings page and stored in the database,
    # so it can be changed without recreating the container.
    # Display only: the address players connect to. The panel cannot work it
    # out - it sees the container's private address, not the public one the
    # port is forwarded from.
    server_real_ip: str
    server_port: int
    steam_query_port: int
    rcon_port: int

    # --- Automation ---
    # Only the two that describe how the container itself boots. Whether the
    # server files and mods are updated on start is a running decision and
    # lives on the settings page - see services/server_settings.py.
    auto_install: bool
    auto_start: bool

    # --- Files page ---
    # Largest upload accepted, in MB. A limit belongs here and not on the page
    # because it also bounds what an authenticated session can push into the
    # container at all - the value applies to every request body.
    max_upload_mb: int

    # --- Reverse proxy / cookies ---
    trusted_proxy_ips: tuple[str, ...]
    session_cookie_secure: str  # "auto" | "true" | "false"
    session_lifetime_hours: int

    paths: Paths = field(default_factory=Paths.from_env)

    # Steam app IDs -- fixed, not a configuration value.
    SERVER_APP_ID: int = 223350   # DayZ Server (Linux, cannot be downloaded anonymously)
    WORKSHOP_APP_ID: int = 221100  # DayZ client -- source for workshop mods

    @classmethod
    def load(cls) -> "Settings":
        paths = Paths.from_env()

        admin_password = os.environ.get("ADMIN_PASSWORD", "")
        if not admin_password:
            raise RuntimeError(
                "ADMIN_PASSWORD is not set. The panel will not start without a "
                "password - see .env.example."
            )

        return cls(
            admin_username=os.environ.get("ADMIN_USERNAME", "admin"),
            admin_password=admin_password,
            secret_key=_load_or_create_secret_key(paths),
            panel_port=_env_int("PANEL_PORT", 8080),
            steam_username=os.environ.get("STEAM_USERNAME", ""),
            steam_password=os.environ.get("STEAM_PASSWORD", ""),
            steam_guard_code=_env_guard_code("STEAM_GUARD_CODE"),
            steam_api_key=_env_plain("STEAM_API_KEY"),
            server_real_ip=_env_plain("SERVER_REAL_IP"),
            server_port=_env_port_range("SERVER_PORT", 2302),
            steam_query_port=_env_int("STEAM_QUERY_PORT", 27016),
            # BattlEye RCON. Written into beserver_x64.cfg before every start,
            # together with the password from the settings page.
            rcon_port=_env_int("RCON_PORT", 2305),
            auto_install=_env_bool("AUTO_INSTALL", True),
            auto_start=_env_bool("AUTO_START", False),
            # 64 MB covers mission folders and mod configs. A whole PBO belongs
            # in the bind mount, not through a browser upload.
            max_upload_mb=_env_int("MAX_UPLOAD_MB", 64),
            # Empty by default -- forwarded headers are only honoured once the
            # operator names the proxy. Trusting them unconditionally would let
            # any client spoof its IP (bypassing the login rate limit) and fake
            # X-Forwarded-Proto: https.
            trusted_proxy_ips=_env_list("TRUSTED_PROXY_IPS"),
            session_cookie_secure=_env_choice(
                "SESSION_COOKIE_SECURE", "auto", {"auto", "true", "false"}
            ),
            session_lifetime_hours=_env_int("SESSION_LIFETIME_HOURS", 12),
            paths=paths,
        )

    @property
    def server_address(self) -> str:
        """What a player types into the DayZ launcher, or "" if unconfigured."""
        return f"{self.server_real_ip}:{self.server_port}" if self.server_real_ip else ""

    @property
    def steam_credentials_set(self) -> bool:
        """Enough to attempt a SteamCMD login.

        The username alone is enough, because the password is only needed when
        there is no stored session to reuse - see SteamCmdService._login_args.
        An operator who has logged in once can then take STEAM_PASSWORD back out
        of .env and keep it off the disk. If it turns out to be needed after all,
        SteamCMD asks for it and the job fails with that as the reason.
        """
        return bool(self.steam_username)

    @property
    def server_installed(self) -> bool:
        return self.paths.server_binary.is_file()

    def secrets_to_mask(self) -> list[str]:
        """Values that must never appear in clear text in logs or the UI."""
        return [v for v in (self.steam_password, self.steam_guard_code, self.admin_password) if v]

    def steam_secrets(self) -> list[str]:
        """Secrets that can plausibly show up in SteamCMD output.

        Deliberately excludes the admin password: it never passes through
        SteamCMD, and masking an unrelated value only risks mangling output that
        happens to contain it.
        """
        return [v for v in (self.steam_password, self.steam_guard_code) if v]


def _load_or_create_secret_key(paths: Paths) -> str:
    """Flask session key.

    Taken from the environment, otherwise persisted inside the volume. A key
    regenerated on every start would invalidate all sessions whenever the
    container restarts.
    """
    from_env = _env_secret_key("PANEL_SECRET_KEY")
    if from_env:
        return from_env

    key_file = paths.secret_key_file
    if key_file.is_file():
        stored = key_file.read_text(encoding="utf-8").strip()
        if stored:
            return stored

    key = secrets.token_urlsafe(48)
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_text(key, encoding="utf-8")
    key_file.chmod(0o600)
    return key
