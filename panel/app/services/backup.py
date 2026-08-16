"""Snapshots of /data/server, driven by restic.

/data/server is around 4 GB. A tar per run would mean 4 GB per backup for a
state that usually differs from the last one only in the persistence files -
ten backups would cost 40 GB for maybe 300 MB of real difference. That is the
whole reason there is an engine here instead of a call to `tarfile`.

restic deduplicates on **block** level and compresses with zstd: the first
snapshot costs the full size, every later one only the changed blocks. What
decided the choice over borg or an rsync hardlink tree, though, is `--json` on
every command - progress, the snapshot list and the result of a prune come back
structured instead of as text this module would have to parse.

The repository is encrypted, with the key beside it in /data/backup. That
placement is deliberate but narrows what the encryption buys: whoever copies
the whole backup directory takes the key along, so the protection only covers
someone who ends up with the repo/ files alone. What it does buy in every case
is that the snapshots are unreadable without a file the operator can keep track
of - and losing that file means losing the backups, which is why nothing here
ever quietly writes a new one.

Every operation that changes the repository (backup, restore, forget) runs as a
Job. That is not only for the output: the job manager has exactly one slot, so
a backup can never run while SteamCMD is writing into the very directory being
snapshotted.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .jobs import Job, jobs

log = logging.getLogger(__name__)

# Job families, so the Backups page asks for its own jobs - see jobs.latest().
BACKUP_KINDS = {"backup", "backup_restore", "backup_forget"}

# Every snapshot is recorded under this name instead of the container's
# hostname, which Docker changes on every recreate. Without it the snapshot
# list would read as if a dozen different machines had written to the
# repository - and restic's retention rules group by host by default.
SNAPSHOT_HOST = "dayz"

STATE_FILE = "backup.json"
KEY_FILE = "backup_key"

# What the download menu offers besides the whole snapshot. One table, read by
# the page to build the menu and by the route to decide whether the answer is a
# tar or the file itself - so the two cannot disagree about what is on offer.
DOWNLOAD_PARTS = (
    {"path": "mpmissions", "label": "mpmissions", "hint": "Map and persistence",
     "archive": True},
    {"path": "profiles", "label": "profiles", "hint": "Server logs",
     "archive": True},
    {"path": "serverDZ.cfg", "label": "serverDZ.cfg", "hint": "Server config",
     "archive": False},
    {"path": "ban.txt", "label": "ban.txt", "hint": "Bans", "archive": False},
    {"path": "whitelist.txt", "label": "whitelist.txt", "hint": "Whitelist",
     "archive": False},
)

_PARTS_BY_PATH = {part["path"]: part for part in DOWNLOAD_PARTS}


def download_part(subpath: str) -> dict | None:
    """The offered part a download path names, or None for anything else."""
    return _PARTS_BY_PATH.get((subpath or "").strip().strip("/"))
CACHE_DIR = "restic-cache"

MAX_EXCLUDES = 40
MAX_KEEP_LAST = 999
MAX_KEEP_DAYS = 3650

# Below this much free space a backup is refused rather than started. Running
# out mid-snapshot is the most unpleasant way to learn about it.
MIN_FREE_BYTES = 2 * 1024**3

# A progress line at most this often. restic reports several times a second;
# all of it in the job output would bury the lines that say something.
PROGRESS_SECONDS = 3.0

# Read-only calls happen inside a request, so they cannot hang forever.
READ_TIMEOUT = 60

# How long the snapshot list and the repository size may be reused.
#
# Every restic call pays about 0.75 s before it does anything: opening the
# repository derives the key with scrypt, deliberately an expensive function.
# Two calls per page view made the Backups page take 1.9 s where the other
# pages take 30 ms. The panel is the only writer here, and it drops the cache
# itself after every job, so the age limit is a backstop for changes made with
# restic on the host - not the mechanism.
CACHE_SECONDS = 60.0

_SNAPSHOT_ID = re.compile(r"^[0-9a-f]{8,64}$")
# restic timestamps carry nanoseconds, which datetime does not parse.
_FRACTION = re.compile(r"\.(\d{6})\d+")


class BackupError(RuntimeError):
    """Anything the operator should read rather than find in the log."""


# --- settings -----------------------------------------------------------------


@dataclass(frozen=True)
class BackupSettings:
    """Retention and what to leave out.

    Both retention rules may be set at once. restic keeps a snapshot when
    **either** rule keeps it - an or, not an and - and the page says so.
    """

    keep_last: int | None = None
    keep_days: int | None = None
    excludes: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "keep_last": self.keep_last,
            "keep_days": self.keep_days,
            "excludes": list(self.excludes),
        }

    @property
    def has_retention(self) -> bool:
        return bool(self.keep_last or self.keep_days)


def parse_settings(form) -> BackupSettings:
    """Validate the retention form. Empty fields mean "no such rule"."""
    return BackupSettings(
        keep_last=_optional_int(form.get("keep_last"), "Keep last", MAX_KEEP_LAST),
        keep_days=_optional_int(form.get("keep_days"), "Delete after", MAX_KEEP_DAYS),
        excludes=_parse_excludes(form.get("excludes", "")),
    )


def _optional_int(value: object, label: str, maximum: int) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        number = int(text)
    except ValueError:
        raise BackupError(f"{label} must be a whole number.") from None
    if not 1 <= number <= maximum:
        raise BackupError(f"{label} must be between 1 and {maximum}.")
    return number


def _parse_excludes(raw: object) -> tuple[str, ...]:
    """One pattern per line, relative to the server directory."""
    lines = [line.strip().strip("/") for line in str(raw or "").splitlines()]
    patterns = [line for line in lines if line and not line.startswith("#")]

    if len(patterns) > MAX_EXCLUDES:
        raise BackupError(f"At most {MAX_EXCLUDES} exclude patterns.")
    for pattern in patterns:
        # ".." would reach outside the directory being backed up, where an
        # exclude has no meaning and could only ever confuse.
        if ".." in pattern.split("/"):
            raise BackupError(f"An exclude pattern cannot contain '..': {pattern}")
    return tuple(patterns)


class BackupStore:
    """Retention settings in /data/panel/backup.json."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._current = self._read()

    @property
    def current(self) -> BackupSettings:
        with self._lock:
            return self._current

    def save(self, settings: BackupSettings) -> BackupSettings:
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temp = self._path.with_suffix(".tmp")
            temp.write_text(json.dumps(settings.to_dict(), indent=2) + "\n", encoding="utf-8")
            temp.replace(self._path)
            self._current = settings
        return settings

    def _read(self) -> BackupSettings:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return BackupSettings()
        if not isinstance(raw, dict):
            return BackupSettings()

        try:
            return BackupSettings(
                keep_last=_optional_int(raw.get("keep_last"), "Keep last", MAX_KEEP_LAST),
                keep_days=_optional_int(raw.get("keep_days"), "Delete after", MAX_KEEP_DAYS),
                excludes=_parse_excludes("\n".join(raw.get("excludes") or [])),
            )
        except BackupError as exc:
            log.warning("Invalid value in %s (%s), using defaults", self._path, exc)
            return BackupSettings()


# --- the service ---------------------------------------------------------------


class BackupService:
    """Everything the panel does with the restic repository."""

    def __init__(self, settings, store: BackupStore, manager=None) -> None:
        self.settings = settings
        self.store = store
        self.manager = manager

        paths = settings.paths
        self.source = paths.server
        self.repo = paths.backup / "repo"
        # Beside the repository, not under panel/: the key belongs to the
        # backups, and restoring elsewhere means carrying both anyway.
        self.key_file = paths.backup / KEY_FILE
        self._legacy_key = paths.panel / KEY_FILE
        self.cache = paths.panel / CACHE_DIR

        self._overview: dict | None = None
        self._overview_lock = threading.Lock()

    def clean(self, text: str) -> str:
        """Strip the volume prefix out of a message restic wrote itself.

        restic is given absolute paths and repeats them in its errors. /data is
        where the container keeps things, not where the operator finds them, so
        no message reaches the UI carrying it.
        """
        return (text or "").replace(str(self.settings.paths.data).rstrip("/") + "/", "")

    def label(self, path: Path) -> str:
        """A path as the operator sees it: relative to the data volume.

        /data is where the container keeps things, not where anyone finds them
        on the host - so nothing in the panel ever shows a path starting there.
        """
        try:
            return str(path.relative_to(self.settings.paths.data)).replace("\\", "/")
        except ValueError:
            return path.name

    # --- environment ----------------------------------------------------------

    @property
    def binary(self) -> str | None:
        return shutil.which("restic")

    @property
    def available(self) -> bool:
        """False on an image built before phase 9 - the page then says so."""
        return self.binary is not None

    @property
    def initialised(self) -> bool:
        return (self.repo / "config").is_file()

    def free_bytes(self) -> int:
        target = self.repo if self.repo.is_dir() else self.settings.paths.data
        try:
            return shutil.disk_usage(target).free
        except OSError:
            return 0

    def _env(self) -> dict:
        # Here rather than only in ensure_key(): reading the snapshot list
        # needs the key just as much as writing one, and after an update from
        # a version that kept it under panel/ the very first thing that
        # happens is usually a page view.
        self._adopt_legacy_key()

        env = os.environ.copy()
        env["RESTIC_REPOSITORY"] = str(self.repo)
        env["RESTIC_PASSWORD_FILE"] = str(self.key_file)
        # Not under $HOME: that is the Steam home, and restic's cache has no
        # business next to the Steam sentry file.
        env["RESTIC_CACHE_DIR"] = str(self.cache)
        env.pop("RESTIC_PASSWORD", None)
        return env

    def ensure_key(self) -> None:
        """Create the repository key once, and never a second one."""
        self._adopt_legacy_key()
        if self.key_file.is_file() and self.key_file.read_text(encoding="utf-8").strip():
            return

        if self.initialised:
            # Generating a fresh key here would turn "the key file is missing"
            # into "wrong password", which is a much harder thing to work out.
            raise BackupError(
                f"The repository exists but {self.label(self.key_file)} is missing or "
                "empty. "
                "Restore that file from a copy - without it the snapshots "
                "cannot be read."
            )

        self.key_file.parent.mkdir(parents=True, exist_ok=True)
        self.key_file.write_text(secrets.token_urlsafe(48) + "\n", encoding="utf-8")
        try:
            self.key_file.chmod(0o600)
        except OSError:
            pass
        log.info("Created the backup repository key at %s", self.key_file)

    def _adopt_legacy_key(self) -> None:
        """Move a key written by an older version to its place beside the repo.

        Without this an update would look like data loss: the repository is
        still there, its key simply is not where the panel now looks, and every
        backup would fail with "the key is missing".
        """
        if self.key_file.is_file() or not self._legacy_key.is_file():
            return
        try:
            self.key_file.parent.mkdir(parents=True, exist_ok=True)
            self._legacy_key.replace(self.key_file)
            log.info("Moved the backup key from %s to %s",
                     self._legacy_key, self.key_file)
        except OSError as exc:
            raise BackupError(
                f"The backup key could not be moved from "
                f"{self.label(self._legacy_key)} to {self.label(self.key_file)}: {exc}"
            ) from None

    def ensure_repo(self, note=lambda _msg: None) -> None:
        if not self.available:
            raise BackupError(
                "restic is not installed in this image. Rebuild the container "
                "image to get the backup engine."
            )

        self.ensure_key()
        if self.initialised:
            return

        self.repo.mkdir(parents=True, exist_ok=True)
        note(f"[panel] Creating the backup repository in {self.label(self.repo)}")
        self._run(["init"], timeout=120)
        note("[panel] Repository created")

    # --- running restic --------------------------------------------------------

    def _run(self, args: list[str], timeout: int = READ_TIMEOUT) -> str:
        """One restic call, output collected. For everything short."""
        if not self.available:
            raise BackupError("restic is not installed in this image.")

        result = subprocess.run(
            [self.binary, *args],
            env=self._env(),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            raise BackupError(_message(self.clean(result.stderr or result.stdout), args))
        return result.stdout

    def _run_json(self, args: list[str], timeout: int = READ_TIMEOUT):
        raw = self._run([*args, "--json"], timeout=timeout)
        try:
            return json.loads(raw or "null")
        except json.JSONDecodeError as exc:
            raise BackupError(f"restic returned output that is not JSON: {exc}") from None

    def _stream(self, args: list[str], job: Job, on_event) -> None:
        """A long call, followed line by line.

        restic writes progress to stdout as one JSON object per line, and its
        errors to stderr - with --json those are JSON too. Unpacking them
        matters: the raw object as an error message would read
        `{"message_type":"exit_error",...}` where the useful part is the one
        sentence naming the file that could not be written.
        """
        proc = subprocess.Popen(
            [self.binary, *args, "--json"],
            env=self._env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        problems: list[str] = []
        last_line: list[str] = []

        def drain() -> None:
            for line in proc.stderr:
                text, is_problem = _readable(self.clean(line.rstrip()))
                if not text:
                    continue
                if is_problem:
                    problems.append(text)
                last_line.append(text)
                job.log_line(f"[restic] {text}")

        watcher = threading.Thread(target=drain, name="restic-stderr", daemon=True)
        watcher.start()

        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    on_event(json.loads(line))
                except json.JSONDecodeError:
                    job.log_line(f"[restic] {line}")
        finally:
            proc.wait()
            watcher.join(timeout=5)

        if proc.returncode != 0:
            # The first real problem, not restic's closing "there were 1
            # errors" - the count is never the part that helps.
            detail = problems[0] if problems else (last_line[-1] if last_line else "")
            raise BackupError(detail or f"restic {args[0]} failed without a message")

    # --- reading ---------------------------------------------------------------

    def overview(self, force: bool = False) -> dict:
        """The snapshot list and the repository size, together and cached.

        Both come from the same source and are shown on the same page, so they
        are fetched and dropped as one - a size that belongs to a different
        list than the one below it would be worse than a slow page.
        """
        with self._overview_lock:
            fresh = (
                self._overview is not None
                and time.time() - self._overview["at"] < CACHE_SECONDS
            )
            if fresh and not force:
                return self._overview

            data = {
                "at": time.time(),
                "snapshots": [],
                "stats": {"size": 0, "count": 0},
                "error": "",
            }
            try:
                data["snapshots"] = self._read_snapshots()
                data["stats"] = self._read_stats()
            except BackupError as exc:
                # A wrong or missing key, a damaged repository: the page is
                # where one would go to find out, so it must still render and
                # say what restic said.
                log.warning("Could not read the backup repository: %s", exc)
                data["error"] = str(exc)

            self._overview = data
            return data

    def invalidate(self) -> None:
        """Called after anything that changes the repository."""
        with self._overview_lock:
            self._overview = None

    def snapshots(self) -> list[dict]:
        return self.overview()["snapshots"]

    def stats(self) -> dict:
        # Free space is asked fresh: it is one cheap syscall, and it changes
        # without the repository changing at all.
        return {**self.overview()["stats"], "free": self.free_bytes()}

    def error(self) -> str:
        """Why the repository could not be read, empty when it could."""
        return self.overview()["error"]

    def _read_snapshots(self) -> list[dict]:
        """Newest first, with the two numbers the page shows."""
        if not (self.available and self.initialised):
            return []

        raw = self._run_json(["snapshots"]) or []
        result = []
        for entry in raw:
            summary = entry.get("summary") or {}
            result.append({
                "id": entry.get("id", ""),
                "short_id": entry.get("short_id", ""),
                "time": _epoch(entry.get("time")),
                "tags": list(entry.get("tags") or []),
                # What the state was, and what keeping it actually costs. The
                # second number is the interesting one: it shows that the tenth
                # snapshot costs megabytes and not another four gigabytes.
                "size": summary.get("total_bytes_processed"),
                "added": summary.get("data_added_packed"),
                "files": summary.get("total_files_processed"),
            })
        result.sort(key=lambda item: item["time"] or 0, reverse=True)
        return result

    def _read_stats(self) -> dict:
        empty = {"size": 0, "count": 0}
        if not (self.available and self.initialised):
            return empty
        try:
            raw = self._run_json(["stats", "--mode", "raw-data"]) or {}
        except BackupError as exc:
            log.warning("Could not read repository stats: %s", exc)
            return empty
        return {"size": raw.get("total_size", 0), "count": raw.get("snapshots_count", 0)}

    def dump(self, snapshot_id: str, subpath: str = "", archive: bool = True):
        """A snapshot, or part of one, straight out of restic.

        Piped through rather than written to a file first: a snapshot is
        gigabytes, and building an archive on disk to hand it out would need
        that space twice at the exact moment someone is worried about it.

        A single file comes out as itself. Wrapping serverDZ.cfg in a tar would
        mean unpacking two kilobytes to read them.
        """
        self._check_id(snapshot_id)
        path = self._inside_source(subpath)

        command = [self.binary, "dump"]
        if archive:
            command += ["--archive", "tar"]
        command += [snapshot_id, str(path)]

        return subprocess.Popen(
            command,
            env=self._env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    # --- jobs ------------------------------------------------------------------

    def start_backup(self, tag: str = "manual") -> Job:
        """Snapshot /data/server. Raises JobBusy if anything else is running."""
        return jobs.start("backup", "Backup of the server directory",
                          self._tracked(lambda job: self._backup(job, tag)))

    def start_restore(self, snapshot_id: str) -> Job:
        self._check_id(snapshot_id)
        return jobs.start("backup_restore", f"Restore of snapshot {snapshot_id[:8]}",
                          self._tracked(lambda job: self._restore(job, snapshot_id)))

    def start_delete(self, snapshot_id: str) -> Job:
        self._check_id(snapshot_id)
        return jobs.start("backup_forget", f"Deleting snapshot {snapshot_id[:8]}",
                          self._tracked(lambda job: self._delete(job, snapshot_id)))

    def start_retention(self) -> Job:
        if not self.initialised:
            # Not a job that fails: there is simply nothing to apply the rules
            # to, and creating a repository would be a strange way to say so.
            raise BackupError(
                "There are no backups yet - the rules apply from the first one."
            )
        return jobs.start("backup_forget", "Applying the retention rules",
                          self._tracked(lambda job: self._forget(job, self.store.current)))

    def _tracked(self, run):
        """Every job changes the repository - so every job drops the cache.

        In a finally, not after a successful run: a backup that failed half way
        through may still have written a snapshot, and a stale list is exactly
        what the operator would be staring at while wondering what happened.
        """
        def wrapped(job: Job) -> None:
            try:
                run(job)
            finally:
                self.invalidate()
                # The page reloads itself a moment after a job ends, so the
                # refill starts now rather than in that request.
                self.warm()

        return wrapped

    def warm(self) -> None:
        """Fill the cache in the background so the first visit is not the slow one.

        Only for a repository that already exists: booting the panel must never
        be what creates one.
        """
        if not (self.available and self.initialised and self.key_file.is_file()):
            return

        def run() -> None:
            try:
                self.overview(force=True)
            except BackupError as exc:
                log.warning("Could not read the backup repository: %s", exc)

        threading.Thread(target=run, name="backup-warmup", daemon=True).start()

    # --- what those jobs do ------------------------------------------------------

    def _backup(self, job: Job, tag: str) -> None:
        self.ensure_repo(job.log_line)

        free = self.free_bytes()
        if free < MIN_FREE_BYTES:
            raise BackupError(
                f"Only {_human(free)} free on the data volume - "
                f"at least {_human(MIN_FREE_BYTES)} is required for a backup."
            )

        settings = self.store.current
        running = bool(self.manager and self.manager.running)
        tags = [tag] + (["hot"] if running else [])

        if running:
            # Not refused: a torn backup is still better than none. But the
            # persistence files are being written while this runs, so the
            # snapshot is not a consistent point in time and says so.
            job.log_line(
                "[panel] The server is running - this snapshot is tagged 'hot' "
                "because persistence may be written while it is taken."
            )

        args = ["backup", "--host", SNAPSHOT_HOST]
        for value in tags:
            args += ["--tag", value]
        for pattern in settings.excludes:
            args += ["--exclude", str(self.source / pattern)]
            job.log_line(f"[panel] Excluded: {pattern}")
        args.append(str(self.source))

        job.detail = "Reading the server directory"
        job.log_line(f"[panel] Snapshotting {self.label(self.source)}")

        summary: dict = {}
        last = [0.0]

        def on_event(event: dict) -> None:
            kind = event.get("message_type")
            if kind == "status":
                now = time.time()
                if now - last[0] < PROGRESS_SECONDS:
                    return
                last[0] = now
                percent = round((event.get("percent_done") or 0) * 100)
                done = _human(event.get("bytes_done") or 0)
                total = _human(event.get("total_bytes") or 0)
                job.detail = f"{percent}% - {done} of {total}"
                job.log_line(f"[restic] {percent:3d}%  {done} / {total}")
            elif kind == "summary":
                summary.update(event)
            elif kind == "error":
                job.log_line(f"[restic] {event.get('error', {}).get('message', event)}")

        self._stream(args, job, on_event)

        added = summary.get("data_added_packed") or 0
        snapshot = (summary.get("snapshot_id") or "")[:8]
        changed = (summary.get("files_new") or 0) + (summary.get("files_changed") or 0)
        job.detail = f"Snapshot {snapshot}, {_human(added)} added"
        job.log_line(
            f"[panel] Snapshot {snapshot}: {changed} new or changed file(s), "
            f"{_human(added)} added to the repository"
        )

        if settings.has_retention:
            self._forget(job, settings, keep_detail=job.detail)

    def _forget(self, job: Job, settings: BackupSettings, keep_detail: str = "") -> None:
        """Apply the retention rules and free the space they release."""
        if not settings.has_retention:
            job.log_line("[panel] No retention rules are set - nothing is deleted.")
            job.detail = keep_detail or "No retention rules are set"
            return

        if not self.initialised:
            job.log_line("[panel] There is no repository yet - nothing to clean up.")
            job.detail = keep_detail or "No backups yet"
            return

        # Without --group-by the rules are applied per host and path. Docker
        # gives the container a new hostname on every recreate, so "keep last
        # 5" would quietly become "keep last 5 per container the panel has ever
        # run in" - and older snapshots would never be cleaned up. An empty
        # grouping treats the repository as the one collection it is, including
        # snapshots taken under earlier hostnames.
        args = ["forget", "--prune", "--group-by", ""]
        if settings.keep_last:
            args += ["--keep-last", str(settings.keep_last)]
        if settings.keep_days:
            args += ["--keep-within", f"{settings.keep_days}d"]
            if not settings.keep_last:
                # An age rule alone can empty the repository: leave the server
                # untouched for longer than the window and every snapshot falls
                # out of it at once. The newest one always stays.
                args += ["--keep-last", "1"]

        job.log_line(f"[panel] Retention: {' '.join(args[2:])}")
        removed = [0]

        def on_event(event: dict) -> None:
            if isinstance(event, list):  # forget reports one entry per group
                for group in event:
                    removed[0] += len(group.get("remove") or [])
                return
            kind = event.get("message_type")
            if kind == "summary" and "snapshots_removed" in event:
                removed[0] = event["snapshots_removed"]

        self._stream(args, job, on_event)

        note = f"{removed[0]} snapshot(s) removed" if removed[0] else "nothing to remove"
        job.log_line(f"[panel] Retention applied: {note}")
        job.detail = f"{keep_detail} - {note}" if keep_detail else f"Retention applied: {note}"

    def _delete(self, job: Job, snapshot_id: str) -> None:
        self.ensure_repo(job.log_line)
        job.log_line(f"[panel] Deleting snapshot {snapshot_id[:8]}")
        self._stream(["forget", snapshot_id, "--prune"], job, lambda _event: None)
        job.detail = f"Snapshot {snapshot_id[:8]} deleted"

    def _restore(self, job: Job, snapshot_id: str) -> None:
        """Put /data/server back to the state of a snapshot.

        Two details that would otherwise be found out the hard way:

        `--delete` is not optional. Without it a restore is a copy over the
        top, and everything added since the snapshot stays - which with a
        botched mod update is the entire point of restoring.

        And restic refuses `--target / --delete` without an include filter, so
        the include is not decoration but the condition under which restic
        permits the deleting at all.
        """
        self.ensure_repo(job.log_line)

        was_running = bool(self.manager and self.manager.running)
        if was_running:
            job.detail = "Stopping the server"
            job.log_line("[panel] Stopping the server before the restore")
            self.manager.stop(wait=True)
            time.sleep(2)

        job.detail = "Safety snapshot"
        job.log_line("[panel] Taking a 'pre-restore' snapshot of the current state")
        self._snapshot_quietly(job, "pre-restore")

        job.detail = "Restoring"
        job.log_line(
            f"[panel] Restoring snapshot {snapshot_id[:8]} into {self.label(self.source)}"
        )

        restored = {}

        def on_event(event: dict) -> None:
            if event.get("message_type") == "summary":
                restored.update(event)

        # A restore that fails part way still has to give the server back. One
        # unwritable file (a stray root-owned one is enough) makes restic exit
        # non-zero after having restored everything else - leaving a stopped
        # server behind would turn a small problem into an outage.
        failure: BackupError | None = None
        try:
            self._stream(
                ["restore", snapshot_id, "--target", "/",
                 "--include", str(self.source), "--delete"],
                job, on_event,
            )
        except BackupError as exc:
            failure = exc
            job.log_line(f"[panel] The restore reported a problem: {exc}")

        files = restored.get("files_restored", 0)
        deleted = restored.get("files_deleted", 0)
        job.log_line(
            f"[panel] {files} file(s) restored, {deleted} removed that were not "
            "in the snapshot"
        )
        job.detail = f"Restored {snapshot_id[:8]}: {files} file(s)"

        if was_running:
            job.log_line("[panel] Starting the server again")
            try:
                self.manager.start(reason="after restore")
            except Exception as exc:  # noqa: BLE001 - the restore matters more
                job.log_line(f"[panel] The server did not start again: {exc}")
                job.detail += " - restart it by hand"

        if failure is not None:
            raise failure

    def _snapshot_quietly(self, job: Job, tag: str) -> None:
        """A snapshot whose progress is not worth its own output."""
        args = ["backup", "--host", SNAPSHOT_HOST, "--tag", tag]
        for pattern in self.store.current.excludes:
            args += ["--exclude", str(self.source / pattern)]
        args.append(str(self.source))

        summary: dict = {}
        self._stream(
            args, job,
            lambda event: summary.update(event)
            if event.get("message_type") == "summary" else None,
        )
        job.log_line(
            f"[panel] Safety snapshot {(summary.get('snapshot_id') or '')[:8]} "
            f"({_human(summary.get('data_added_packed') or 0)} added)"
        )

    # --- guards ------------------------------------------------------------------

    @staticmethod
    def _check_id(snapshot_id: str) -> None:
        """Snapshot ids end up on a command line, so nothing else may."""
        if not _SNAPSHOT_ID.match(str(snapshot_id or "")):
            raise BackupError("That is not a snapshot id.")

    def _inside_source(self, subpath: str) -> Path:
        """A download path, confined to what was backed up."""
        cleaned = str(subpath or "").strip().strip("/")
        if not cleaned:
            return self.source
        if ".." in cleaned.split("/"):
            raise BackupError("That path is not inside the backup.")
        return self.source / cleaned


# --- helpers ---------------------------------------------------------------------


def _epoch(value: object) -> float | None:
    """restic's RFC 3339 stamp with nanoseconds as plain unix time."""
    if not isinstance(value, str) or not value:
        return None
    text = _FRACTION.sub(r".\1", value.replace("Z", "+00:00"))
    try:
        from datetime import datetime

        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _human(size: float | None) -> str:
    value = float(size or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def _readable(line: str) -> tuple[str, bool]:
    """One stderr line as a sentence, and whether it reports a problem.

    With --json restic writes its errors as JSON objects too. Passed through
    unchanged they would reach the operator as `{"message_type":"error",...}`,
    with the one part that matters - which file, and why - buried inside.
    """
    line = (line or "").strip()
    if not line:
        return "", False

    try:
        event = json.loads(line)
    except ValueError:
        return line, False
    if not isinstance(event, dict):
        return line, False

    kind = event.get("message_type")
    if kind == "error":
        message = (event.get("error") or {}).get("message") or event.get("message") or line
        item = event.get("item")
        return (f"{message} ({item})" if item else message), True
    if kind == "exit_error":
        # The closing summary ("There were 1 errors"), which says nothing on
        # its own - kept as a fallback, not counted as the problem.
        return str(event.get("message", line)).strip(), False
    return str(event.get("message") or line), False


def _message(stderr: str, args: list[str]) -> str:
    """The line from restic worth showing, not the whole dump."""
    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    for line in lines:
        readable, is_problem = _readable(line)
        if is_problem or line.lower().startswith(("fatal", "error")):
            return readable
    return _readable(lines[-1])[0] if lines else f"restic {args[0]} failed without a message"
