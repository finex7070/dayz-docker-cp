"""Recurring tasks in crontab format.

A schedule is a cron expression plus a **list** of actions that run one after
the other. That shape comes from what people actually do at 4 in the morning:
announce the restart, lock the server so nobody joins into it, then restart.
Three separate entries at 03:55, 03:58 and 04:00 would express the same thing
in a way that quietly breaks as soon as one of them is edited.

The actions run through the same objects as the dashboard buttons - the
startup sequence for a start, the manager for a stop, the RCON service for
everything else. A second path would be a second set of rules about when a
start is allowed, and the two would drift.

Persistence is `/data/panel/schedules.json`, like the mod list: a handful of
records the operator can read and repair with a text editor on the host.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

log = logging.getLogger(__name__)

MAX_ACTIONS = 10
MAX_NAME = 60
MAX_COMMAND = 200

# Between two actions of the same entry. "say" then "#lock" back to back is
# fine, but a stop followed by a start needs the process to be gone first -
# and the stop itself already waits for that.
ACTION_GAP_SECONDS = 1.0

ACTIONS = {
    "start": "Start the server",
    "stop": "Stop the server",
    "restart": "Restart the server",
    "lock": "Lock (no new players)",
    "unlock": "Unlock",
    "rcon": "RCON command",
    "backup": "Back up the server directory",
}

# Actions that need a command line of their own.
NEEDS_COMMAND = {"rcon"}


def _stamp(value: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(value)) if value else "—"


class ScheduleError(ValueError):
    """A rejected entry, phrased for the person who typed it."""


@dataclass(frozen=True)
class Action:
    kind: str
    command: str = ""

    def to_dict(self) -> dict:
        return {"kind": self.kind, "command": self.command}

    @property
    def label(self) -> str:
        if self.kind == "rcon":
            return f"RCON: {self.command}"
        return ACTIONS.get(self.kind, self.kind)


@dataclass(frozen=True)
class Schedule:
    id: str
    name: str
    cron: str
    actions: tuple[Action, ...]
    enabled: bool = True
    last_run: float | None = None
    last_result: str = ""
    last_ok: bool | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "cron": self.cron,
            "actions": [action.to_dict() for action in self.actions],
            "enabled": self.enabled,
            "last_run": self.last_run,
            "last_result": self.last_result,
            "last_ok": self.last_ok,
        }


def parse_cron(expression: str) -> CronTrigger:
    """Validate a five-field crontab expression.

    Done here rather than at save time in the scheduler so that a typo comes
    back as a form error instead of a task that silently never fires.
    """
    text = " ".join((expression or "").split())
    if not text:
        raise ScheduleError("A time is required, e.g. 0 4 * * * for 04:00 daily.")
    if len(text.split()) != 5:
        raise ScheduleError(
            "The time needs five fields: minute hour day month weekday "
            "(e.g. 0 4 * * *)."
        )
    try:
        return CronTrigger.from_crontab(text)
    except ValueError as exc:
        raise ScheduleError(f"That is not a valid crontab expression: {exc}") from exc


def parse_actions(raw: object) -> tuple[Action, ...]:
    if not isinstance(raw, list) or not raw:
        raise ScheduleError("Add at least one action.")
    if len(raw) > MAX_ACTIONS:
        raise ScheduleError(f"At most {MAX_ACTIONS} actions per entry.")

    actions = []
    for item in raw:
        kind = str((item or {}).get("kind", "")).strip()
        if kind not in ACTIONS:
            raise ScheduleError(f"Unknown action: {kind or '(empty)'}")
        command = str((item or {}).get("command", "")).strip()
        if kind in NEEDS_COMMAND and not command:
            raise ScheduleError("An RCON action needs a command.")
        if len(command) > MAX_COMMAND:
            raise ScheduleError(f"A command may be at most {MAX_COMMAND} characters.")
        actions.append(Action(kind, command if kind in NEEDS_COMMAND else ""))
    return tuple(actions)


def parse_name(value: object) -> str:
    name = " ".join(str(value or "").split())
    if not name:
        raise ScheduleError("A name is required.")
    if len(name) > MAX_NAME:
        raise ScheduleError(f"The name may be at most {MAX_NAME} characters.")
    return name


class ScheduleStore:
    """The entries on disk. Reads tolerate damage, writes are atomic."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._items: dict[str, Schedule] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.is_file():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # A broken file must not stop the panel from starting - it is the
            # only tool the operator has for fixing it.
            log.warning("schedules.json unreadable (%s) - starting with none", exc)
            return

        for entry in raw.get("schedules", []):
            try:
                self._items[entry["id"]] = Schedule(
                    id=str(entry["id"]),
                    name=parse_name(entry.get("name")),
                    cron=" ".join(str(entry.get("cron", "")).split()),
                    actions=parse_actions(entry.get("actions")),
                    enabled=bool(entry.get("enabled", True)),
                    last_run=entry.get("last_run"),
                    last_result=str(entry.get("last_result", "")),
                    last_ok=entry.get("last_ok"),
                )
            except (KeyError, ScheduleError) as exc:
                log.warning("Skipping a damaged schedule entry: %s", exc)

    def _write(self) -> None:
        payload = {"schedules": [item.to_dict() for item in self._items.values()]}
        temp = self._path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temp.replace(self._path)

    def list(self) -> list[Schedule]:
        with self._lock:
            return sorted(self._items.values(), key=lambda item: item.name.lower())

    def get(self, schedule_id: str) -> Schedule | None:
        with self._lock:
            return self._items.get(schedule_id)

    def put(self, schedule: Schedule) -> Schedule:
        with self._lock:
            self._items[schedule.id] = schedule
            self._write()
        return schedule

    def delete(self, schedule_id: str) -> bool:
        with self._lock:
            if self._items.pop(schedule_id, None) is None:
                return False
            self._write()
        return True


class ScheduleService:
    """Keeps APScheduler in step with the stored entries."""

    def __init__(self, store: ScheduleStore, runner, audit=None) -> None:
        self._store = store
        self._runner = runner
        # Passed in rather than looked up: a cron firing at 4 a.m. has no
        # request and therefore no current_app to reach it through.
        self._audit = audit
        self._scheduler = BackgroundScheduler(
            # One entry at a time, and a job that was missed while the panel was
            # busy is dropped rather than fired late: a 4 a.m. restart running
            # at 4:20 because the container was updating is worse than not
            # running at all.
            job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 120},
        )
        self._lock = threading.Lock()

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._scheduler.start()
        for schedule in self._store.list():
            self._sync(schedule)
        log.info("Scheduler running with %d entries", len(self._store.list()))

    def shutdown(self) -> None:
        try:
            self._scheduler.shutdown(wait=False)
        except Exception:  # noqa: BLE001 - shutdown must not raise on the way out
            pass

    # --- entries -----------------------------------------------------------

    def list(self) -> list[dict]:
        return [self._view(schedule) for schedule in self._store.list()]

    def create(self, data: dict) -> Schedule:
        schedule = Schedule(
            id=uuid.uuid4().hex[:12],
            name=parse_name(data.get("name")),
            cron=self._valid_cron(data.get("cron")),
            actions=parse_actions(data.get("actions")),
            enabled=bool(data.get("enabled", True)),
        )
        self._store.put(schedule)
        self._sync(schedule)
        return schedule

    def update(self, schedule_id: str, data: dict) -> Schedule:
        current = self._require(schedule_id)
        # The result of the last run is kept: editing the time of an entry does
        # not make yesterday's failure untrue.
        schedule = Schedule(
            id=current.id,
            name=parse_name(data.get("name", current.name)),
            cron=self._valid_cron(data.get("cron", current.cron)),
            actions=parse_actions(data.get("actions"))
            if "actions" in data else current.actions,
            enabled=bool(data.get("enabled", current.enabled)),
            last_run=current.last_run,
            last_result=current.last_result,
            last_ok=current.last_ok,
        )
        self._store.put(schedule)
        self._sync(schedule)
        return schedule

    def delete(self, schedule_id: str) -> None:
        self._require(schedule_id)
        self._store.delete(schedule_id)
        self._unschedule(schedule_id)

    def run_now(self, schedule_id: str) -> Schedule:
        """Run an entry immediately - the only honest way to test one."""
        schedule = self._require(schedule_id)
        self._run(schedule.id, manual=True)
        return self._require(schedule_id)

    def _require(self, schedule_id: str) -> Schedule:
        schedule = self._store.get(schedule_id)
        if schedule is None:
            raise ScheduleError("That schedule no longer exists.")
        return schedule

    @staticmethod
    def _valid_cron(expression: object) -> str:
        parse_cron(str(expression or ""))
        return " ".join(str(expression or "").split())

    # --- scheduling --------------------------------------------------------

    def _sync(self, schedule: Schedule) -> None:
        self._unschedule(schedule.id)
        if not schedule.enabled:
            return
        self._scheduler.add_job(
            self._run,
            trigger=parse_cron(schedule.cron),
            id=schedule.id,
            args=[schedule.id],
            name=schedule.name,
            replace_existing=True,
        )

    def _unschedule(self, schedule_id: str) -> None:
        try:
            self._scheduler.remove_job(schedule_id)
        except Exception:  # noqa: BLE001 - "was not scheduled" is the normal case
            pass

    def next_run(self, schedule_id: str) -> float | None:
        job = self._scheduler.get_job(schedule_id)
        return job.next_run_time.timestamp() if job and job.next_run_time else None

    def _view(self, schedule: Schedule) -> dict:
        data = schedule.to_dict()
        data["actions"] = [
            {"kind": action.kind, "command": action.command, "label": action.label}
            for action in schedule.actions
        ]
        data["next_run"] = self.next_run(schedule.id)
        # Formatted here, not in the browser: cron fires in the container's
        # timezone, and an operator connecting from another one would otherwise
        # read a next run that is off by hours and correct for nobody.
        data["next_run_text"] = _stamp(data["next_run"])
        data["last_run_text"] = _stamp(schedule.last_run)
        return data

    # --- running -----------------------------------------------------------

    def _run(self, schedule_id: str, manual: bool = False) -> None:
        schedule = self._store.get(schedule_id)
        if schedule is None:
            self._unschedule(schedule_id)
            return

        # Two entries firing at the same minute would otherwise interleave a
        # stop with someone else's start.
        with self._lock:
            log.info("Schedule '%s' running (%s)", schedule.name,
                     "manual" if manual else "cron")
            results, ok = self._run_actions(schedule)

        if self._audit is not None:
            self._audit.record("schedules.fired", schedule.name, ok=ok,
                               detail="; ".join(results))

        self._store.put(
            Schedule(
                id=schedule.id,
                name=schedule.name,
                cron=schedule.cron,
                actions=schedule.actions,
                enabled=schedule.enabled,
                last_run=time.time(),
                last_result="; ".join(results),
                last_ok=ok,
            )
        )

    def _run_actions(self, schedule: Schedule) -> tuple[list[str], bool]:
        """Run the actions in order, stopping at the first failure.

        Carrying on would be worse than stopping: the point of a sequence like
        announce - lock - restart is the order, and a restart that runs after a
        failed lock is not the thing the operator asked for.
        """
        results: list[str] = []
        for index, action in enumerate(schedule.actions):
            if index:
                time.sleep(ACTION_GAP_SECONDS)
            try:
                message = self._runner(action)
            except Exception as exc:  # noqa: BLE001 - the reason is the result
                results.append(f"{action.label}: {exc}")
                log.warning("Schedule '%s' stopped at '%s': %s",
                            schedule.name, action.label, exc)
                return results, False
            results.append(f"{action.label}: {message or 'done'}")
        return results, True


def make_runner(sequence, manager, rcon, backup=None):
    """One action, performed the same way the dashboard performs it.

    A start returns as soon as it has been set going, because it may have a
    server update in front of it that takes minutes. Actions meant to happen
    *before* a restart therefore have to be listed before it - which is the
    order one writes them in anyway.
    """

    def run(action: Action) -> str:
        if action.kind == "backup":
            # Waits, unlike a start: the chain that makes sense is stop ->
            # backup -> start, and a backup that returned early would be taken
            # while the server was already coming back up.
            if backup is None:
                raise ScheduleError("The backup service is not available.")
            job = backup.start_backup(tag="scheduled")
            job.done.wait()
            if job.state.is_final and job.state.value != "success":
                raise ScheduleError(job.error or job.detail or "the backup failed")
            return job.detail or "backed up"
        if action.kind == "start":
            return sequence.start_server(reason="schedule")
        if action.kind == "restart":
            return sequence.start_server(reason="schedule", restart=True)
        if action.kind == "stop":
            manager.stop(wait=True)
            return "stopped"
        if action.kind == "lock":
            rcon.send("#lock")
            return "locked"
        if action.kind == "unlock":
            rcon.send("#unlock")
            return "unlocked"
        if action.kind == "rcon":
            answer = rcon.send(action.command)
            return " ".join(answer.split())[:120] or "sent"
        raise ScheduleError(f"Unknown action: {action.kind}")

    return run
