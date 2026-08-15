"""Driving SteamCMD from the panel.

The awkward part is that SteamCMD is an interactive program. It writes its
password and Steam Guard prompts **without a trailing newline** and then blocks
on stdin, so the usual "read a line at a time" loop would deadlock: the prompt
never completes a line, and the process never continues because nobody answers.

This module therefore reads raw chunks from the pipe, keeps the unterminated
remainder, and checks that remainder against the known prompts. When a Steam
Guard prompt appears, the job switches to `needs_guard`, the UI asks the
operator for the code, and the worker writes it back into the process.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

from ..config import Settings
from . import server_files
from .jobs import Job, JobState, jobs

log = logging.getLogger(__name__)

# How long a job waits for the operator to type a Steam Guard code.
GUARD_TIMEOUT_SECONDS = 300

# Guard against a prompt loop (wrong password entered over and over).
MAX_PROMPTS = 4

# Progress lines arrive many times per second; keep at most one per interval.
PROGRESS_INTERVAL_SECONDS = 0.5

# Shortest secret that is worth masking - see _mask().
MIN_MASK_LENGTH = 4

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

# The two shapes SteamCMD repeats many times per second:
#   [ 42%] Downloading update...
#   Update state (0x61) downloading, progress: 42.00 (1024 / 2048)
_PROGRESS = re.compile(
    r"^\[\s*\d+(\.\d+)?%\]|update state \(0x[0-9a-f]+\)", re.IGNORECASE
)

# Prompts SteamCMD emits while waiting on stdin.
_PASSWORD_PROMPT = re.compile(r"password\s*:\s*$", re.IGNORECASE)
_GUARD_PROMPT = re.compile(
    r"(steam\s*guard|two[- ]?factor|2fa)[^:]*:\s*$", re.IGNORECASE
)

# Output that means the run did not do what we asked, even when the exit code
# is 0 - which SteamCMD does return in several failure cases.
_FAILURE_PATTERNS = (
    (re.compile(r"two[- ]?factor code mismatch", re.I), "The Steam Guard code was wrong."),
    (re.compile(r"invalid password", re.I), "Steam rejected the password."),
    (re.compile(r"rate limit exceeded", re.I),
     "Steam is rate limiting this account. Wait a few minutes before retrying."),
    (re.compile(r"account logon denied", re.I),
     "Steam requires a Steam Guard code for this login."),
    (re.compile(r"no subscription", re.I),
     "The Steam account does not own DayZ, so it cannot download the server."),
    (re.compile(r"error!\s*app\s*'?\d+'?.*state is 0x", re.I),
     "SteamCMD could not install the app - see the log above."),
    (re.compile(r"error!\s*download item .*failed", re.I),
     "The workshop download failed - check the mod ID and that the Steam account "
     "owns DayZ."),
    (re.compile(r"disk write failure|no space left", re.I),
     "Writing to the data volume failed - check free space and permissions."),
)

# "Success! App '223350' fully installed." and the workshop equivalent
# "Success. Downloaded item 1234567890 to ... (123456 bytes)".
_SUCCESS = re.compile(r"success!\s*app\s*'?\d+'?|success\.\s*downloaded item", re.I)


class SteamCmdService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.binary = os.environ.get("STEAMCMD_BIN", "steamcmd")
        self.sdk_script = Path(
            os.environ.get("STEAM_SDK_SCRIPT", "/opt/scripts/steam_sdk_links.sh")
        )

    # --- public API -------------------------------------------------------

    def install_server(self) -> Job:
        return self._start_app_update("install", "Install server files", validate=True)

    def update_server(self, validate: bool = False) -> Job:
        return self._start_app_update("update", "Update server files", validate=validate)

    def download_workshop_item(self, job: Job, workshop_id: int | str) -> bool:
        """Download one workshop item inside an *existing* job.

        Public because the mod service runs several downloads in one job: the
        operator wants one output log for "update all mods", not one job per mod
        queueing behind the exclusive slot.

        force_install_dir points at the Steam home, so the item lands in
        Paths.workshop. Without it the destination depends on where SteamCMD
        itself was installed, which is not something the panel should guess.
        """
        args = [
            self.binary,
            "+force_install_dir", str(self.settings.paths.steam),
            *self._login_args(),
            "+workshop_download_item", str(self.settings.WORKSHOP_APP_ID), str(workshop_id),
            "+quit",
        ]
        self._run(job, args, expect_success=True)
        return job.state is not JobState.FAILED and not job.cancelled

    @property
    def available(self) -> bool:
        return shutil.which(self.binary) is not None or Path(self.binary).is_file()

    # --- job wiring -------------------------------------------------------

    def _login_args(self) -> list[str]:
        """+login, with the guard code when one came from the environment.

        Credentials go on the command line, the way every SteamCMD automation
        does it: without a terminal, SteamCMD does not reliably fall back to
        prompting, and a one-time guard code passed upfront avoids the
        "Account Logon Denied" dead end. The prompt handling in _pump stays as
        a fallback for the cases where it does ask. Downside is that the
        credentials are visible in the container's process list; they are
        masked everywhere the panel shows or logs the command.
        """
        login = ["+login", self.settings.steam_username, self.settings.steam_password]
        if self.settings.steam_guard_code:
            login.append(self.settings.steam_guard_code)
        return login

    def _start_app_update(self, kind: str, title: str, validate: bool) -> Job:
        app_id = self.settings.SERVER_APP_ID

        args = [
            self.binary,
            "+force_install_dir", str(self.settings.paths.server),
            *self._login_args(),
            "+app_update", str(app_id),
        ]
        if validate:
            args.append("validate")
        args.append("+quit")

        def runner(job: Job) -> None:
            job.detail = f"Running SteamCMD for app {app_id}"
            self._run(job, args, expect_success=True)
            if job.state is not JobState.FAILED and not job.cancelled:
                self._refresh_sdk_links(job)
                # Recorded even when nothing was downloaded: "checked and still
                # current" is exactly what this note is for.
                server_files.mark_checked(self.settings.paths)
                job.detail = "Server files are ready"

        return jobs.start(kind, title, runner)

    # --- process handling -------------------------------------------------

    def _run(self, job: Job, args: list[str], expect_success: bool) -> None:
        if not self.settings.steam_credentials_set:
            self._fail(job, "Steam credentials are missing - set STEAM_USERNAME and "
                            "STEAM_PASSWORD.")
            return

        secrets = list(self.settings.steam_secrets())
        job.log_line(f"[panel] $ {_mask(' '.join(args), secrets)}")

        env = os.environ.copy()
        env["HOME"] = str(self.settings.paths.steam)
        env["STEAM_HOME"] = str(self.settings.paths.steam)

        try:
            proc = subprocess.Popen(  # noqa: S603 - argument list, never a shell
                args,
                cwd=str(self.settings.paths.steam),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
            )
        except OSError as exc:
            self._fail(job, f"Could not start SteamCMD: {exc}")
            return

        stopper = _terminate_on_cancel(job, proc)
        try:
            failure = self._pump(job, proc, secrets)
            proc.wait()
        finally:
            stopper.set()
            for stream in (proc.stdin, proc.stdout):
                try:
                    if stream:
                        stream.close()
                except OSError:
                    pass

        job.exit_code = proc.returncode
        job.log_line(f"[panel] SteamCMD exited with code {proc.returncode}")

        if job.cancelled:
            return
        if failure:
            self._fail(job, failure)
        elif proc.returncode != 0:
            self._fail(job, f"SteamCMD exited with code {proc.returncode}.")
        elif expect_success and not getattr(job, "_saw_success", False):
            self._fail(job, "SteamCMD finished without reporting success - see the log.")

    def _pump(self, job: Job, proc: subprocess.Popen, secrets: list[str]) -> str | None:
        """Read the process output, answer prompts, return a failure reason."""
        fd = proc.stdout.fileno()
        pending = ""
        failure: str | None = None
        prompts = 0
        job._saw_success = False  # type: ignore[attr-defined]

        # Progress redraws arrive many times per second. Keeping every one of
        # them would bury the interesting output, so they are throttled - but
        # the most recent one is always held back and flushed, so the log never
        # ends on a stale percentage.
        held_progress: str | None = None
        last_progress = 0.0

        def flush_progress() -> None:
            nonlocal held_progress
            if held_progress is not None:
                job.log_line(held_progress)
                held_progress = None

        while True:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break

            pending += _ANSI.sub("", chunk.decode("utf-8", errors="replace"))

            # SteamCMD uses \r to redraw progress, so both count as breaks.
            parts = re.split(r"\r\n|\r|\n", pending)
            pending = parts.pop()

            for raw in parts:
                line = _mask(raw.rstrip(), secrets)
                if not line:
                    continue

                if _PROGRESS.search(line):
                    now = time.monotonic()
                    if now - last_progress < PROGRESS_INTERVAL_SECONDS:
                        held_progress = line   # keep the newest, drop the rest
                        continue
                    last_progress = now
                    held_progress = None
                    job.log_line(line)
                    continue

                flush_progress()
                job.log_line(line)

                if _SUCCESS.search(line):
                    job._saw_success = True  # type: ignore[attr-defined]
                if failure is None:
                    failure = _match_failure(line)

            # The remainder is where an unterminated prompt shows up.
            if pending.strip():
                answered, prompt_failure = self._answer_prompt(
                    job, proc, pending, secrets, prompts
                )
                if prompt_failure:
                    return prompt_failure
                if answered:
                    prompts += 1
                    pending = ""

        flush_progress()
        if pending.strip():
            job.log_line(_mask(pending.strip(), secrets))

        return failure

    def _answer_prompt(
        self, job: Job, proc: subprocess.Popen, pending: str, secrets: list[str], prompts: int
    ) -> tuple[bool, str | None]:
        """Reply to a password or Steam Guard prompt. Returns (answered, failure)."""
        text = pending.strip()

        if _PASSWORD_PROMPT.search(text):
            if prompts >= MAX_PROMPTS:
                return False, "SteamCMD kept asking for credentials - giving up."
            job.log_line("[panel] SteamCMD asked for the password, sending it")
            return self._write_stdin(proc, self.settings.steam_password), None

        if _GUARD_PROMPT.search(text):
            if prompts >= MAX_PROMPTS:
                return False, "The Steam Guard code was rejected repeatedly."

            code = self.settings.steam_guard_code if prompts == 0 else ""
            if code:
                job.log_line("[panel] Steam Guard requested, using STEAM_GUARD_CODE")
            else:
                job.log_line("[panel] Steam Guard requested - waiting for a code")
                job.request_guard_code(text)
                code = job.wait_for_guard_code(GUARD_TIMEOUT_SECONDS) or ""
                if job.cancelled:
                    return False, None
                if not code:
                    return False, (
                        "No Steam Guard code was entered within "
                        f"{GUARD_TIMEOUT_SECONDS // 60} minutes."
                    )
                # Mask the freshly entered code in any later output as well.
                secrets.append(code)
                job.log_line("[panel] Steam Guard code received, continuing")

            return self._write_stdin(proc, code), None

        return False, None

    @staticmethod
    def _write_stdin(proc: subprocess.Popen, value: str) -> bool:
        try:
            proc.stdin.write((value + "\n").encode())
            proc.stdin.flush()
            return True
        except (OSError, ValueError):
            return False

    # --- after a successful run ------------------------------------------

    def _refresh_sdk_links(self, job: Job) -> None:
        """Re-create ~/.steam/sdk32|64.

        The link targets only exist once SteamCMD has unpacked its runtime, so
        the container start cannot do this reliably - see steam_sdk_links.sh.
        """
        if not self.sdk_script.is_file():
            return
        try:
            result = subprocess.run(  # noqa: S603 - fixed path, argument list
                [str(self.sdk_script)],
                capture_output=True,
                text=True,
                timeout=60,
                env={**os.environ, "STEAM_HOME": str(self.settings.paths.steam)},
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            job.log_line(f"[panel] Could not refresh the Steam SDK links: {exc}")
            return

        for line in (result.stdout or "").splitlines():
            job.log_line(line)

    @staticmethod
    def _fail(job: Job, message: str) -> None:
        job.state = JobState.FAILED
        job.error = message
        job.detail = "Failed"
        job.log_line(f"[panel] {message}")


def _match_failure(line: str) -> str | None:
    for pattern, message in _FAILURE_PATTERNS:
        if pattern.search(line):
            return message
    return None


def _mask(text: str, secrets: list[str]) -> str:
    """Replace credentials with *** before anything reaches a log or the UI.

    Secrets shorter than MIN_MASK_LENGTH are skipped on purpose. A one or two
    character value occurs all over ordinary output - masking it would corrupt
    the log (turning "0x61" into "0***61"), break the pattern matching that runs
    on these lines, and reveal the secret's positions rather than hide it.
    Steam passwords have an 8 character minimum and guard codes are 5, so
    nothing that matters is exempt.
    """
    for secret in secrets:
        if secret and len(secret) >= MIN_MASK_LENGTH and secret in text:
            text = text.replace(secret, "***")
    return text


def _terminate_on_cancel(job: Job, proc: subprocess.Popen) -> threading.Event:
    """Kill the process when the job is cancelled.

    Needed because the reader blocks in os.read: without an outside nudge, a
    cancelled job would only notice once SteamCMD produced more output.
    """
    done = threading.Event()

    def watch() -> None:
        while not done.wait(0.25):
            if job.cancelled and proc.poll() is None:
                job.log_line("[panel] Cancelled - stopping SteamCMD")
                try:
                    proc.send_signal(signal.SIGTERM)
                    if not _wait(proc, 10):
                        proc.kill()
                except OSError:
                    pass
                return

    threading.Thread(target=watch, name=f"cancel-{job.id}", daemon=True).start()
    return done


def _wait(proc: subprocess.Popen, timeout: float) -> bool:
    try:
        proc.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False
