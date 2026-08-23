"""Process control for the DayZ server.

One process, one state machine, one lock. The panel runs in a single gunicorn
worker (see gunicorn.conf.py) precisely so that this object can be the only
authority on whether the server is up - a second worker would mean a second
manager, a second watchdog and orphaned processes nobody owns.

The DayZ server is stopped with SIGTERM and only killed if it ignores that:
it writes its persistence to disk on the way out, and a SIGKILL in the middle
of that is how a save gets corrupted.

"Running" means the mission is loaded, not that the process exists. The engine
announces that itself, and the watcher below is already reading every line it
prints - see READY_MARKERS.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import signal
import subprocess
import threading
import time
from collections import deque
from enum import Enum
from pathlib import Path

from ..config import Settings
from . import server_config
from .jobs import LineBuffer, jobs
from .server_settings import ServerSettings, SettingsStore

log = logging.getLogger(__name__)

MAX_CONSOLE_LINES = 2000

# What the engine prints once the mission is loaded and players may join.
# "Player connect enabled" is that moment exactly; the FPS line is the game
# loop's own heartbeat and stands in for it should a build rename the first.
# Measured on 1.29: the process exists for over two minutes before either.
READY_MARKERS = ("Player connect enabled", "Average server FPS:")

# What the panel itself gets when the container stops, whatever the operator
# set for a manual stop. The chain in PLAN.md 7.5 is built on this number:
# it has to stay under gunicorn's graceful_timeout (50s), which has to stay
# under compose's stop_grace_period (60s). A longer wait here does not buy the
# server more time, it only moves the kill from the panel to Docker.
CONTAINER_STOP_TIMEOUT = 30

# A build that renames both markers must not strand the panel in "Starting":
# everything gated on RUNNING - RCON, the console command line - would stay
# disabled for good. Generous, because a heavily modded server loads for long.
READY_TIMEOUT_SECONDS = 900

# Auto restart after a crash, with growing gaps. A server that dies instantly
# because of a broken mod would otherwise restart forever and bury the reason.
RESTART_DELAYS = (5, 15, 60)

# Survive this long and the crash counter resets - the last crash was a
# one-off, not a loop.
HEALTHY_SECONDS = 300


class ServerState(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    CRASHED = "crashed"

    @property
    def is_up(self) -> bool:
        return self in {ServerState.STARTING, ServerState.RUNNING, ServerState.STOPPING}


LABELS = {
    ServerState.STOPPED: "Stopped",
    ServerState.STARTING: "Starting",
    ServerState.RUNNING: "Running",
    ServerState.STOPPING: "Stopping",
    ServerState.CRASHED: "Crashed",
}

BADGES = {
    ServerState.STOPPED: "text-bg-secondary",
    ServerState.STARTING: "text-bg-info",
    ServerState.RUNNING: "text-bg-success",
    ServerState.STOPPING: "text-bg-warning",
    ServerState.CRASHED: "text-bg-danger",
}


class ServerError(RuntimeError):
    """The requested action cannot be carried out right now."""


class ServerManager:
    def __init__(self, settings: Settings, store: SettingsStore) -> None:
        self.settings = settings
        self.store = store

        self._lock = threading.RLock()
        self._state = ServerState.STOPPED
        self._proc: subprocess.Popen | None = None
        self._started_at: float | None = None
        self._stopped_at: float | None = None
        self._exit_code: int | None = None
        self._error = ""
        self._command = ""
        self._stop_requested = False
        self._console = LineBuffer(MAX_CONSOLE_LINES)
        self._exited = threading.Event()
        self._exited.set()
        self._crashes: deque[float] = deque(maxlen=len(RESTART_DELAYS) + 1)
        self._restart_timer: threading.Timer | None = None
        self._ready_timer: threading.Timer | None = None
        self._cpu_sample: tuple[float, float] | None = None  # (ticks, monotonic)
        self._cpu_percent = 0.0

    # --- state ------------------------------------------------------------

    @property
    def state(self) -> ServerState:
        with self._lock:
            return self._state

    @property
    def running(self) -> bool:
        return self.state.is_up

    @property
    def console_buffer(self) -> LineBuffer:
        """The current output buffer. Replaced on every start, hence a property."""
        return self._console

    @property
    def pid(self) -> int | None:
        with self._lock:
            return self._proc.pid if self._proc and self._proc.poll() is None else None

    def blocked_reason(self) -> str:
        """Why starting is not possible right now, empty string if it is."""
        if not self.settings.server_installed:
            return "The server files are not installed yet."
        active = jobs.active
        if active is not None:
            # Not "a SteamCMD job": since phase 9 a backup holds the same slot,
            # and the title below already says which it is.
            return f"A job is running: {active.title}"
        return ""

    # --- actions ----------------------------------------------------------

    def start(self, reason: str = "manual") -> None:
        """Spawn the server. Returns as soon as the process exists."""
        with self._lock:
            if self._state.is_up:
                raise ServerError("The server is already running.")

            blocked = self.blocked_reason()
            if blocked:
                raise ServerError(blocked)

            self._state = ServerState.STARTING
            self._stop_requested = False
            self._error = ""
            self._exit_code = None
            self._cancel_pending_restart()

        try:
            self._spawn(reason)
        except Exception as exc:  # noqa: BLE001 - surface it, do not hang in STARTING
            with self._lock:
                self._state = ServerState.STOPPED
                self._error = str(exc)
            self._console.append(f"[panel] Start failed: {exc}")
            log.error("Server start failed: %s", exc)
            raise

    @property
    def stop_timeout(self) -> int:
        """How long a shutdown gets before the process is killed."""
        return self.store.current.stop_timeout_seconds

    def stop(self, *, wait: bool = False, timeout: float | None = None) -> None:
        """Ask the server to shut down. Runs in the background unless `wait`."""
        if timeout is None:
            timeout = self.stop_timeout
        with self._lock:
            self._cancel_pending_restart()
            self._cancel_ready_timer()
            if not self._state.is_up or self._proc is None:
                raise ServerError("The server is not running.")
            if self._state is ServerState.STOPPING and not wait:
                raise ServerError("The server is already shutting down.")
            self._state = ServerState.STOPPING
            self._stop_requested = True
            proc = self._proc
            exited = self._exited

        if wait:
            self._terminate(proc, timeout)
            # proc.wait() returning is not the same as the manager being back
            # in a settled state - the watcher thread still has to record the
            # exit. A restart that skipped this would find itself "running".
            exited.wait(10)
            return

        threading.Thread(
            target=self._terminate, args=(proc, timeout), name="dayz-stop", daemon=True
        ).start()

    def kill(self) -> str:
        """End the process now, without waiting for it to save.

        The way out of a shutdown that is not progressing. It costs whatever the
        server had not written yet, which is why it is a separate decision from
        stop() rather than a shorter timeout.
        """
        with self._lock:
            self._cancel_pending_restart()
            self._cancel_ready_timer()
            if not self._state.is_up or self._proc is None:
                raise ServerError("The server is not running.")
            self._state = ServerState.STOPPING
            self._stop_requested = True
            proc = self._proc

        self._console.append("[panel] Killing the process on request (SIGKILL)")
        log.warning("DayZ server killed on request (pid %s)", proc.pid)
        self._send_kill(proc)
        return "Kill signal sent - unsaved state is lost."

    def restart(self) -> None:
        with self._lock:
            if not self._state.is_up:
                raise ServerError("The server is not running.")

        def run() -> None:
            try:
                self.stop(wait=True)
                # A moment of daylight between the two: ports and the BattlEye
                # config file are only really free once the process is gone.
                time.sleep(2)
                self.start(reason="restart")
            except ServerError as exc:
                log.warning("Restart aborted: %s", exc)

        threading.Thread(target=run, name="dayz-restart", daemon=True).start()

    def shutdown(self) -> None:
        """Stop the server when the panel itself goes down.

        Registered with atexit: the container stopping must not leave the DayZ
        process to be killed by Docker, mid-write.
        """
        with self._lock:
            self._cancel_pending_restart()
            if not self._state.is_up:
                return
        log.info("Panel is shutting down - stopping the DayZ server first")
        try:
            self.stop(wait=True, timeout=min(self.stop_timeout, CONTAINER_STOP_TIMEOUT))
        except ServerError:
            pass

    # --- process ----------------------------------------------------------

    def _spawn(self, reason: str) -> None:
        paths = self.settings.paths
        server_settings = self.store.current

        binary = paths.server_binary
        if not binary.is_file():
            raise ServerError(f"{binary} does not exist - install the server files first.")
        if not os.access(binary, os.X_OK):
            raise ServerError(f"{binary} is not executable.")

        mission_dir = paths.server / "mpmissions" / server_settings.mission
        if not mission_dir.is_dir():
            raise ServerError(
                f"Mission '{server_settings.mission}' does not exist in mpmissions/."
            )

        self._console = LineBuffer(MAX_CONSOLE_LINES)
        self._console.append(f"[panel] Starting the DayZ server ({reason})")
        server_config.prepare(self.settings, server_settings, self._console.append)

        args = build_command(self.settings, server_settings)
        command = " ".join(shlex.quote(arg) for arg in args)
        self._console.append(f"[panel] $ {command}")

        env = os.environ.copy()
        env["HOME"] = str(paths.steam)
        # The server loads several shared objects from its own directory, and
        # nothing else puts that directory on the search path.
        env["LD_LIBRARY_PATH"] = ":".join(
            filter(None, [str(paths.server), env.get("LD_LIBRARY_PATH", "")])
        )

        proc = subprocess.Popen(  # noqa: S603 - argument list, never a shell
            args,
            cwd=str(paths.server),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            text=True,
            errors="replace",
            # Own process group, so a stop signal reaches whatever the server
            # spawned instead of only the top process.
            start_new_session=True,
        )

        exited = threading.Event()
        with self._lock:
            self._proc = proc
            self._command = command
            # Not RUNNING: the process exists, but the mission takes minutes to
            # load and nobody can join until it has. _note_progress promotes it.
            self._state = ServerState.STARTING
            self._started_at = time.time()
            self._stopped_at = None
            self._exited = exited
            self._cpu_sample = None
            self._cpu_percent = 0.0
            self._arm_ready_timer()

        log.info("DayZ server started (pid %s, %s)", proc.pid, reason)
        threading.Thread(
            target=self._watch,
            args=(proc, exited),
            name=f"dayz-watch-{proc.pid}",
            daemon=True,
        ).start()

    def _watch(self, proc: subprocess.Popen, exited: threading.Event) -> None:
        """Pump the server's output, then decide what its exit meant."""
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                text = line.rstrip("\r\n")
                self._console.append(text)
                self._note_progress(text, proc)
        except (OSError, ValueError):
            pass
        finally:
            try:
                if proc.stdout:
                    proc.stdout.close()
            except OSError:
                pass

        code = proc.wait()
        try:
            self._on_exit(proc, code)
        finally:
            exited.set()

    def _note_progress(self, line: str, proc: subprocess.Popen) -> None:
        """Promote STARTING to RUNNING when the engine says it is open."""
        with self._lock:
            if self._proc is not proc or self._state is not ServerState.STARTING:
                return
            if not any(marker in line for marker in READY_MARKERS):
                return
        self._mark_ready("the mission is loaded")

    def _mark_ready(self, reason: str) -> None:
        with self._lock:
            if self._state is not ServerState.STARTING:
                return  # stopped, crashed, or already up while we waited
            self._state = ServerState.RUNNING
            self._cancel_ready_timer()
            waited = time.time() - (self._started_at or time.time())

        self._console.append(f"[panel] Server is up after {waited:.0f}s - {reason}")
        log.info("DayZ server is up after %.0fs (%s)", waited, reason)

    def _arm_ready_timer(self) -> None:
        """Caller holds the lock."""
        self._cancel_ready_timer()
        timer = threading.Timer(
            READY_TIMEOUT_SECONDS,
            self._mark_ready,
            args=(f"no ready marker within {READY_TIMEOUT_SECONDS}s",),
        )
        timer.name = "dayz-ready"
        timer.daemon = True
        self._ready_timer = timer
        timer.start()

    def _cancel_ready_timer(self) -> None:
        """Caller holds the lock."""
        if self._ready_timer is not None:
            self._ready_timer.cancel()
            self._ready_timer = None

    def _on_exit(self, proc: subprocess.Popen, code: int) -> None:
        with self._lock:
            if self._proc is not proc:
                return  # a newer process has taken over

            self._cancel_ready_timer()
            expected = self._stop_requested
            uptime = time.time() - (self._started_at or time.time())
            self._proc = None
            self._exit_code = code
            self._stopped_at = time.time()
            self._state = ServerState.STOPPED if expected else ServerState.CRASHED

            if expected:
                self._crashes.clear()
                self._console.append(f"[panel] Server stopped (exit code {code})")
                log.info("DayZ server stopped (exit code %s)", code)
                return

            self._error = f"The server exited unexpectedly with code {code}."
            self._console.append(f"[panel] {self._error}")
            log.warning("DayZ server exited unexpectedly (code %s)", code)

            if uptime >= HEALTHY_SECONDS:
                self._crashes.clear()  # it ran fine for a while, not a loop
            self._crashes.append(time.time())
            auto_restart = self.store.current.auto_restart
            attempt = len(self._crashes)

        if not auto_restart:
            return

        if attempt > len(RESTART_DELAYS):
            message = (
                f"[panel] Giving up after {attempt - 1} restart attempts - "
                "fix the cause and start the server manually."
            )
            self._console.append(message)
            log.error(message)
            return

        delay = RESTART_DELAYS[attempt - 1]
        self._console.append(
            f"[panel] Auto restart {attempt}/{len(RESTART_DELAYS)} in {delay}s"
        )
        self._schedule_restart(delay)

    def _schedule_restart(self, delay: float) -> None:
        def run() -> None:
            try:
                self.start(reason="auto restart")
            except (ServerError, OSError) as exc:
                self._console.append(f"[panel] Auto restart failed: {exc}")

        with self._lock:
            self._cancel_pending_restart()
            timer = threading.Timer(delay, run)
            timer.name = "dayz-auto-restart"
            timer.daemon = True
            self._restart_timer = timer
        timer.start()

    def _cancel_pending_restart(self) -> None:
        """Caller holds the lock."""
        if self._restart_timer is not None:
            self._restart_timer.cancel()
            self._restart_timer = None

    def _terminate(self, proc: subprocess.Popen, timeout: float) -> None:
        self._console.append(
            f"[panel] Sending SIGTERM, waiting up to {timeout:.0f}s for a clean shutdown"
        )
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass

        try:
            proc.wait(timeout=timeout)
            return
        except subprocess.TimeoutExpired:
            pass

        self._console.append(
            f"[panel] No shutdown within {timeout:.0f}s - killing the process"
        )
        log.warning("DayZ server ignored SIGTERM for %ss, sending SIGKILL", timeout)
        self._send_kill(proc)

    def _send_kill(self, proc: subprocess.Popen) -> None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass

    # --- reporting --------------------------------------------------------

    def status(self) -> dict:
        with self._lock:
            state = self._state
            proc = self._proc
            started_at = self._started_at
            uptime = time.time() - started_at if started_at and state.is_up else 0.0
            data = {
                "state": state.value,
                "label": LABELS[state],
                "badge": BADGES[state],
                "pid": proc.pid if proc else None,
                "started_at": started_at if state.is_up else None,
                "uptime_seconds": int(uptime),
                "exit_code": self._exit_code,
                "error": self._error,
                "command": self._command,
                "auto_restart": self.store.current.auto_restart,
                "restart_attempts": len(self._crashes),
                "blocked_reason": "" if state.is_up else self.blocked_reason(),
                "can_start": not state.is_up and not self.blocked_reason(),
                "can_stop": state.is_up and state is not ServerState.STOPPING,
                # Separate from can_stop: this is the way out of a shutdown
                # that is not progressing, so it stays available during one.
                "can_kill": state.is_up and proc is not None,
                "stop_timeout_seconds": self.store.current.stop_timeout_seconds,
                "mission": self.store.current.mission,
            }

        data.update(self._resource_usage(data["pid"]))
        return data

    def console_note(self, line: str) -> None:
        """Put a line into the console that did not come from the process.

        RCON messages and the panel's own notes go through here so that they
        share the buffer - and therefore the ordering - with the server output
        they belong to.
        """
        self._console.append(line)

    def console(self, after: int = 0) -> dict:
        first, lines = self._console.since(after)
        return {
            "lines": lines,
            "first_index": first,
            "next_index": first + len(lines),
            "gap": first > after,
        }

    def _resource_usage(self, pid: int | None) -> dict:
        if pid is None:
            return {"rss_mb": None, "cpu_percent": None}

        rss_mb = _read_rss_mb(pid)
        ticks = _read_cpu_ticks(pid)
        if ticks is None:
            return {"rss_mb": rss_mb, "cpu_percent": None}

        now = time.monotonic()
        with self._lock:
            previous = self._cpu_sample
            # Below a second the quantisation of the tick counter makes the
            # percentage jump around; keep showing the last one instead.
            if previous and now - previous[1] >= 1.0:
                elapsed = now - previous[1]
                self._cpu_percent = max(
                    0.0, (ticks - previous[0]) / _CLOCK_TICKS / elapsed * 100.0
                )
                self._cpu_sample = (ticks, now)
            elif previous is None:
                self._cpu_sample = (ticks, now)
            percent = self._cpu_percent

        return {"rss_mb": rss_mb, "cpu_percent": round(percent, 1)}


def host_capacity() -> dict:
    """What the whole container has, to give the process numbers a scale.

    Read from the cgroup first, not from /proc: inside a container /proc shows
    the host's cores and RAM, and a panel that says "of 32 GB" while compose
    caps the container at 4 is worse than saying nothing.
    """
    return {"cores": _cpu_quota() or os.cpu_count() or 1, "memory_mb": _memory_limit_mb()}


def _read_first(*paths: str) -> str | None:
    for path in paths:
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            continue
    return None


def _cpu_quota() -> float | None:
    """Cores this container may use, from `cpu.max` (v2) or the v1 pair."""
    text = _read_first("/sys/fs/cgroup/cpu.max")
    if text and not text.startswith("max"):
        quota, _, period = text.partition(" ")
        try:
            return int(quota) / int(period or 100000)
        except ValueError:
            return None

    quota = _read_first("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
    period = _read_first("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
    try:
        if quota and period and int(quota) > 0:
            return int(quota) / int(period)
    except ValueError:
        pass
    return None


def _memory_limit_mb() -> float | None:
    text = _read_first("/sys/fs/cgroup/memory.max",
                       "/sys/fs/cgroup/memory/memory.limit_in_bytes")
    if text and not text.startswith("max"):
        try:
            limit = int(text)
            # An unlimited v1 cgroup reports a number near 2^63 rather than a
            # word, so anything absurd means "no limit" too.
            if 0 < limit < (1 << 60):
                return limit / 1024 / 1024
        except ValueError:
            pass

    meminfo = _read_first("/proc/meminfo") or ""
    match = re.search(r"^MemTotal:\s+(\d+) kB", meminfo, re.M)
    return int(match.group(1)) / 1024 if match else None


def build_command(settings: Settings, server_settings: ServerSettings) -> list[str]:
    """The DayZ command line.

    Switches that are off and empty values are left out entirely rather than
    passed empty: `-limitFPS=` or `-limitFPS=0` does not mean "no limit", and
    there is no way to spell a negated switch.
    """
    paths = settings.paths

    args = [
        str(paths.server_binary),
        "-config=serverDZ.cfg",
        f"-port={settings.server_port}",
        f"-BEpath={paths.battleye}",
        f"-profiles={paths.profiles}",
        f"-mission=mpmissions/{server_settings.mission}",
        f"-cpuCount={server_settings.cpu_count}",
    ]

    if server_settings.client_mods:
        args.append("-mod=" + ";".join(server_settings.client_mods))
    if server_settings.server_mods:
        args.append("-serverMod=" + ";".join(server_settings.server_mods))

    for flag, enabled in (
        ("-doLogs", server_settings.do_logs),
        ("-adminLog", server_settings.admin_log),
        ("-netLog", server_settings.net_log),
        ("-freezeCheck", server_settings.freeze_check),
        ("-filePatching", server_settings.file_patching),
    ):
        if enabled:
            args.append(flag)

    if server_settings.limit_fps:
        args.append(f"-limitFPS={server_settings.limit_fps}")

    return args


_CLOCK_TICKS = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


def _read_rss_mb(pid: int) -> float | None:
    """Resident memory from /proc - no psutil dependency for two numbers."""
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)
    except (OSError, ValueError, IndexError):
        return None
    return None


def _read_cpu_ticks(pid: int) -> float | None:
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            fields = handle.read().rsplit(") ", 1)[-1].split()
        # utime and stime, counted from the field after the process state.
        return float(fields[11]) + float(fields[12])
    except (OSError, ValueError, IndexError):
        return None
