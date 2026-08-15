"""Background jobs with a live output buffer.

SteamCMD runs take minutes, so they cannot happen inside a request. A job is a
worker thread plus the state a browser needs to follow along: current status,
an output buffer that can be polled incrementally, and - for Steam Guard - a
way to hand a value back into the running process.

Only one exclusive job runs at a time. Two concurrent SteamCMD processes would
fight over the same Steam session and the same install directory.
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

log = logging.getLogger(__name__)

MAX_LINES = 5000
MAX_HISTORY = 20

# Job families, so that a page asks for the jobs it is actually about.
SERVER_FILE_KINDS = {"install", "update"}
MOD_KINDS = {"mod_install", "mod_update", "mod_reinstall"}


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    NEEDS_GUARD = "needs_guard"   # waiting for a Steam Guard code from the UI
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_final(self) -> bool:
        return self in {JobState.SUCCESS, JobState.FAILED, JobState.CANCELLED}


class LineBuffer:
    """Bounded output buffer that clients can poll with a cursor.

    Keeps an absolute index across dropped lines, so a client that polls with
    ?after=N can tell whether it missed anything instead of silently getting
    a shifted view once the buffer wraps.
    """

    def __init__(self, maxlen: int = MAX_LINES) -> None:
        self._lines: deque[str] = deque(maxlen=maxlen)
        self._offset = 0  # absolute index of _lines[0]
        self._lock = threading.Lock()
        # Identifies this buffer across requests. A restarted server or a new
        # job gets a fresh buffer, and a reader holding an index into the old
        # one has to be told to start over rather than silently reading
        # unrelated lines at the same position.
        self.id = uuid.uuid4().hex[:8]

    def append(self, line: str) -> None:
        with self._lock:
            if len(self._lines) == self._lines.maxlen:
                self._offset += 1
            self._lines.append(line)

    def since(self, index: int) -> tuple[int, list[str]]:
        """Return (absolute index of the first line returned, lines)."""
        with self._lock:
            start = max(index - self._offset, 0)
            lines = list(itertools.islice(self._lines, start, None))
            return self._offset + start, lines

    @property
    def next_index(self) -> int:
        with self._lock:
            return self._offset + len(self._lines)

    def text(self) -> str:
        with self._lock:
            return "\n".join(self._lines)


@dataclass
class Job:
    kind: str                 # "install", "update", "mod_download", ...
    title: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    state: JobState = JobState.QUEUED
    detail: str = ""          # short human-readable status line
    error: str = ""
    exit_code: int | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    lines: LineBuffer = field(default_factory=LineBuffer)

    # Steam Guard handshake: the worker waits on _guard_ready, the request
    # thread fills _guard_code and sets the event.
    guard_prompt: str = ""
    _guard_code: str | None = None
    _guard_ready: threading.Event = field(default_factory=threading.Event)
    _cancel: threading.Event = field(default_factory=threading.Event)

    # Set once the job has reached a final state. Lets a waiter block instead
    # of polling the state, which matters for the sequence that has to run
    # before the server may start.
    done: threading.Event = field(default_factory=threading.Event)

    def log_line(self, line: str) -> None:
        self.lines.append(line)

    # --- Steam Guard ------------------------------------------------------

    def request_guard_code(self, prompt: str) -> None:
        self.guard_prompt = prompt.strip()
        self._guard_code = None
        self._guard_ready.clear()
        self.state = JobState.NEEDS_GUARD
        self.detail = "Waiting for a Steam Guard code"

    def submit_guard_code(self, code: str) -> bool:
        if self.state is not JobState.NEEDS_GUARD:
            return False
        self._guard_code = code.strip()
        self._guard_ready.set()
        return True

    def wait_for_guard_code(self, timeout: float) -> str | None:
        """Block the worker until the UI supplies a code (or time runs out)."""
        if not self._guard_ready.wait(timeout):
            return None
        self.state = JobState.RUNNING
        self.detail = "Steam Guard code submitted"
        return self._guard_code

    # --- Cancellation -----------------------------------------------------

    def cancel(self) -> None:
        self._cancel.set()
        # A job waiting for a code would otherwise block until the timeout.
        self._guard_ready.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    # --- Serialisation ----------------------------------------------------

    @property
    def duration(self) -> float:
        if self.started_at is None:
            return 0.0
        return (self.finished_at or time.time()) - self.started_at

    def to_dict(self, after: int | None = None) -> dict:
        data = {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "state": self.state.value,
            "detail": self.detail,
            "error": self.error,
            "exit_code": self.exit_code,
            "guard_prompt": self.guard_prompt if self.state is JobState.NEEDS_GUARD else "",
            "is_final": self.state.is_final,
            "duration": round(self.duration, 1),
        }
        if after is not None:
            first, lines = self.lines.since(after)
            data["lines"] = lines
            data["first_index"] = first
            data["next_index"] = first + len(lines)
            # Tells the client its cursor was too old and lines were dropped.
            data["gap"] = first > after
        return data


class JobManager:
    """Registry of jobs, with a single exclusive slot for the running one."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._order: deque[str] = deque(maxlen=MAX_HISTORY)
        self._active: Job | None = None

    @property
    def active(self) -> Job | None:
        with self._lock:
            return self._active

    @property
    def busy(self) -> bool:
        return self.active is not None

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def latest(self, kinds: str | set[str] | None = None) -> Job | None:
        """Most recent job, optionally restricted to certain kinds.

        A set rather than a single kind because the pages that show jobs each
        own a family of them: the dashboard shows server file work, the mods
        page shows mod work. Mixing them puts a mod download's output under a
        heading that says "Server files".
        """
        wanted = {kinds} if isinstance(kinds, str) else kinds

        with self._lock:
            for job_id in reversed(self._order):
                job = self._jobs.get(job_id)
                if job and (wanted is None or job.kind in wanted):
                    return job
        return None

    def history(self) -> list[Job]:
        with self._lock:
            return [self._jobs[j] for j in reversed(self._order) if j in self._jobs]

    def start(self, kind: str, title: str, runner: Callable[[Job], None]) -> Job:
        """Start a job. Raises JobBusy if another one is already running."""
        job = Job(kind=kind, title=title)

        with self._lock:
            if self._active is not None:
                raise JobBusy(self._active)
            self._active = job
            self._jobs[job.id] = job
            self._order.append(job.id)
            # Drop buffers of jobs that fell out of the history window.
            for stale in list(self._jobs):
                if stale not in self._order:
                    del self._jobs[stale]

        thread = threading.Thread(
            target=self._run, args=(job, runner), name=f"job-{kind}-{job.id}", daemon=True
        )
        thread.start()
        return job

    def _run(self, job: Job, runner: Callable[[Job], None]) -> None:
        job.state = JobState.RUNNING
        job.started_at = time.time()
        log.info("Job %s (%s) started", job.id, job.kind)

        try:
            runner(job)
            if job.cancelled:
                job.state = JobState.CANCELLED
                job.detail = "Cancelled"
            elif job.state is not JobState.FAILED:
                job.state = JobState.SUCCESS
                job.detail = job.detail or "Finished"
        except Exception as exc:  # noqa: BLE001 - a job must never kill the worker
            log.exception("Job %s (%s) crashed", job.id, job.kind)
            job.state = JobState.FAILED
            job.error = str(exc)
            job.detail = "Failed"
            job.log_line(f"[panel] Job failed: {exc}")
        finally:
            job.finished_at = time.time()
            with self._lock:
                if self._active is job:
                    self._active = None
            job.done.set()
            log.info("Job %s finished: %s (%.1fs)", job.id, job.state.value, job.duration)


class JobBusy(RuntimeError):
    """Raised when a second exclusive job is requested."""

    def __init__(self, active: Job) -> None:
        super().__init__(f"another job is already running: {active.title}")
        self.active = active


# Single instance for the process (one gunicorn worker, see gunicorn.conf.py).
jobs = JobManager()
