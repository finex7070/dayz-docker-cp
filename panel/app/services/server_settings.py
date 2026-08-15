"""Server settings the operator controls, persisted across container restarts.

Kept as a JSON file in /data/panel rather than in a database. These are a
handful of scalars, and the operator already has /data mounted on the host -
a plain file can be read and repaired from there without a database client.

The mod lists below are written by the mod service, not by the settings form:
the mods page owns which mods exist and in what order, and this is where the
command line picks them up. serverDZ.cfg is edited in place instead of being
mirrored here - see services/serverdz.py.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_MISSION = "dayzOffline.chernarusplus"

# DayZ ignores anything above this, so a larger value is a typo, not a wish.
MAX_LIMIT_FPS = 200


class SettingsError(ValueError):
    """A supplied value cannot be used - reported back to the form."""


@dataclass(frozen=True)
class ServerSettings:
    """Everything that shapes the DayZ command line.

    Port numbers are deliberately absent: they come from the environment,
    because they have to match what the container publishes.
    """

    mission: str = DEFAULT_MISSION
    cpu_count: int = 2
    do_logs: bool = True
    admin_log: bool = True
    net_log: bool = False
    freeze_check: bool = True
    file_patching: bool = False
    limit_fps: int | None = None      # None = no -limitFPS parameter at all
    rcon_password: str = ""
    # BattlEye's RestrictRCon, written next to the password. Kept out of the
    # environment for the same reason as the password: it belongs to the
    # server, not to how the container was created.
    rcon_restrict: bool = True
    auto_restart: bool = True

    # What the panel does on container start. Here rather than in the
    # environment because they are operating decisions that get changed while
    # a server is live - recreating the container to stop updating mods would
    # be the wrong tool.
    auto_update: bool = False       # app_update of the server files
    auto_mod_update: bool = False   # update every installed mod, after the above

    # Directory names, filled by the mod manager in phase 6. Two lists because
    # a client mod goes into -mod= and a server-only mod into -serverMod=.
    client_mods: tuple[str, ...] = field(default_factory=tuple)
    server_mods: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["client_mods"] = list(self.client_mods)
        data["server_mods"] = list(self.server_mods)
        return data


def _as_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _as_int(value: object, name: str, minimum: int, maximum: int) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise SettingsError(f"{name} must be a whole number.") from None
    if not minimum <= number <= maximum:
        raise SettingsError(f"{name} must be between {minimum} and {maximum}.")
    return number


def _as_optional_int(value: object, name: str, minimum: int, maximum: int) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    return _as_int(value, name, minimum, maximum)


def _as_mission(value: object) -> str:
    mission = str(value or "").strip().strip("/")
    if not mission:
        raise SettingsError("A mission must be selected.")
    # The value is pasted into a path below mpmissions/, so nothing may be able
    # to climb out of it.
    if "/" in mission or "\\" in mission or mission.startswith("."):
        raise SettingsError("The mission name must be a plain folder name.")
    return mission


def _as_mod_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


class SettingsStore:
    """Reads and writes ServerSettings, atomically and thread safely."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._current = self._read()

        if not path.is_file():
            # Write the defaults out straight away. A settings file that only
            # appears after the first change cannot be inspected or repaired
            # from the host, which is half the reason for using a file.
            try:
                self._write(self._current)
            except OSError as exc:
                log.warning("Could not create %s: %s", path, exc)

    @property
    def current(self) -> ServerSettings:
        with self._lock:
            return self._current

    def update(self, **changes: object) -> ServerSettings:
        """Apply validated changes and persist them.

        Unknown keys are rejected rather than ignored: silently dropping a
        setting the caller believed it had saved is the worse failure.
        """
        with self._lock:
            updated = replace(self._current, **_validate(changes))
            self._current = updated
            self._write(updated)
        return updated

    # --- persistence ------------------------------------------------------

    def _read(self) -> ServerSettings:
        if not self._path.is_file():
            return ServerSettings()

        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # Defaults rather than a crash: a damaged settings file must not
            # keep the panel - the only way to fix it - from starting.
            log.warning("Could not read %s (%s), using defaults", self._path, exc)
            return ServerSettings()

        if not isinstance(raw, dict):
            return ServerSettings()

        known = {f for f in ServerSettings().to_dict()}
        try:
            return replace(
                ServerSettings(),
                **_validate({k: v for k, v in raw.items() if k in known}, strict=False),
            )
        except SettingsError as exc:
            log.warning("Invalid value in %s (%s), using defaults", self._path, exc)
            return ServerSettings()

    def _write(self, settings: ServerSettings) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Write beside the target and rename: a half-written file after a
        # crash would be read as "no settings" on the next start.
        temp = self._path.with_suffix(".tmp")
        temp.write_text(json.dumps(settings.to_dict(), indent=2) + "\n", encoding="utf-8")
        temp.replace(self._path)
        try:
            self._path.chmod(0o600)  # it holds the RCON password
        except OSError:
            pass


def _validate(changes: dict, strict: bool = True) -> dict:
    """Coerce and range check incoming values."""
    defaults = ServerSettings()
    cores = os.cpu_count() or 1
    result: dict = {}

    for key, value in changes.items():
        if key == "mission":
            result[key] = _as_mission(value)
        elif key == "cpu_count":
            result[key] = _as_int(value, "CPU count", 1, max(cores, 1))
        elif key == "limit_fps":
            result[key] = _as_optional_int(value, "FPS limit", 1, MAX_LIMIT_FPS)
        elif key == "rcon_password":
            result[key] = str(value or "").strip()
        elif key in {"client_mods", "server_mods"}:
            result[key] = _as_mod_list(value)
        elif key in {
            "do_logs", "admin_log", "net_log", "freeze_check",
            "file_patching", "auto_restart", "auto_update", "auto_mod_update",
            "rcon_restrict",
        }:
            result[key] = _as_bool(value, getattr(defaults, key))
        elif strict:
            raise SettingsError(f"Unknown setting: {key}")

    return result
