"""Steam Workshop mods: registry, download, installation.

A mod reaches the server in two steps. SteamCMD downloads it into the workshop
cache under /data/steam, and the panel then copies it to
/data/server/@<id>_<name>. The copy is what makes `-mod=@name` work, and keeping
the cache intact is what lets the next download be incremental instead of a
full re-fetch.

Two things happen on the way that are easy to miss and expensive to debug:

* File names are lowercased. Mods are built on Windows, where `Addons/Foo.pbo`
  and `addons/foo.pbo` are the same file - on Linux they are not, and a mod with
  mixed case simply does not load.
* Only client mods (-mod=) get their .bikey copied into the server's keys
  directory. A server mod runs on the server alone, so no client ever presents
  its signature; adding the key would only widen what the server accepts.

The registry is a JSON file next to the other panel state. It holds a handful
of records with no relations to speak of, and being repairable from the host
with a text editor is worth more here than anything a database would add.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
import zlib
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from ..config import Settings, normalize_mod_name
from .jobs import Job, JobState, jobs
from .modlist import ModlistEntry, ModlistError, parse_modlist, render_modlist
from .server_settings import SettingsStore
from .steamcmd import SteamCmdService

log = logging.getLogger(__name__)

MOD_TYPES = ("client", "server")

# Steam's Web API. The details endpoint needs no key; searching does.
API_BASE = "https://api.steampowered.com"
API_TIMEOUT = 15

# Public page of a workshop item, for the "open in Steam" link.
WORKSHOP_ITEM_URL = "https://steamcommunity.com/sharedfiles/filedetails/?id="

# Six results fill exactly three rows of the two-column grid on the mods page.
SEARCH_PER_PAGE = 6

# `name = "Mod Title";` inside a mod's meta.cpp - the mod's own idea of its
# name, and available without asking Steam anything.
_META_NAME = re.compile(r'^\s*name\s*=\s*"([^"]*)"', re.IGNORECASE | re.MULTILINE)

# Accepts a bare ID, a workshop URL, or anything with ?id= in it.
_ID_PATTERN = re.compile(r"(?:^|[?&/=])(\d{6,12})(?:$|[^0-9])")

# A mod uploaded by hand has no workshop ID, but everything downstream - the
# registry, the routes, the buttons - identifies a mod by one. It gets a number
# from a range Steam cannot reach: published IDs are ten digits today, and the
# pattern above accepts at most twelve, so nothing that arrives from Steam or
# from the operator's clipboard can land here.
LOCAL_ID_BASE = 10 ** 14

# What makes a directory a mod, per the modding basics: the launcher and the
# server both go by `@Name`, and the PBOs live in `addons` beside it. A folder
# with neither is somebody's notes, not a mod.
ADDONS_DIR = "addons"

# The key of the base game. It comes with the server files rather than with a
# mod, so nothing that rebuilds keys/ from the mod list may take it.
VANILLA_KEY = "dayz.bikey"

ARCHIVE_SUFFIX = ".zip"


class ModError(RuntimeError):
    """Something the operator can fix, reported back to the page."""


@dataclass(frozen=True)
class Mod:
    workshop_id: int
    name: str
    dir_name: str              # "@1234567890_mod_name"
    mod_type: str = "client"   # "client" -> -mod=, "server" -> -serverMod=
    enabled: bool = True
    keys: tuple[str, ...] = ()  # .bikey files this mod put into server/keys
    size_mb: float = 0.0
    updated_at: float = 0.0

    @property
    def is_client(self) -> bool:
        return self.mod_type == "client"

    @property
    def is_local(self) -> bool:
        """Uploaded into the server directory rather than fetched from Steam."""
        return self.workshop_id >= LOCAL_ID_BASE

    def to_dict(self) -> dict:
        data = asdict(self)
        data["keys"] = list(self.keys)
        return data


def parse_workshop_id(raw: str) -> int:
    """Turn what the operator pasted into a workshop ID.

    Accepting the full URL matters: that is what the browser puts on the
    clipboard, and asking someone to cut the ID out of it by hand is an
    invitation to paste the wrong number.
    """
    text = (raw or "").strip()
    if text.isdigit():
        return int(text)

    match = _ID_PATTERN.search(text)
    if not match:
        raise ModError("No workshop ID found. Paste the ID or the workshop URL.")
    return int(match.group(1))


class ModRegistry:
    """The installed mods and their order, persisted as JSON."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._mods: list[Mod] = self._read()

    def all(self) -> list[Mod]:
        with self._lock:
            return list(self._mods)

    def get(self, workshop_id: int) -> Mod | None:
        with self._lock:
            return next((m for m in self._mods if m.workshop_id == workshop_id), None)

    def upsert(self, mod: Mod) -> None:
        with self._lock:
            for index, existing in enumerate(self._mods):
                if existing.workshop_id == mod.workshop_id:
                    self._mods[index] = mod
                    break
            else:
                self._mods.append(mod)
            self._write()

    def remove(self, workshop_id: int) -> Mod | None:
        with self._lock:
            mod = self.get(workshop_id)
            if mod is None:
                return None
            self._mods = [m for m in self._mods if m.workshop_id != workshop_id]
            self._write()
            return mod

    def move(self, workshop_id: int, offset: int) -> None:
        """Shift a mod in the load order.

        Order is not cosmetic: DayZ loads mods in the order given on the command
        line, and a mod that patches another has to come after it.
        """
        with self._lock:
            ids = [m.workshop_id for m in self._mods]
            if workshop_id not in ids:
                return
            index = ids.index(workshop_id)
            target = min(max(index + offset, 0), len(self._mods) - 1)
            if target == index:
                return
            self._mods.insert(target, self._mods.pop(index))
            self._write()

    # --- persistence ------------------------------------------------------

    def _read(self) -> list[Mod]:
        if not self._path.is_file():
            return []
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # An unreadable registry must not keep the panel from starting -
            # the panel is where it gets repaired.
            log.warning("Could not read %s (%s), starting with no mods", self._path, exc)
            return []

        mods = []
        for entry in raw.get("mods", []) if isinstance(raw, dict) else []:
            try:
                mods.append(
                    Mod(
                        workshop_id=int(entry["workshop_id"]),
                        name=str(entry.get("name") or entry["workshop_id"]),
                        dir_name=str(entry["dir_name"]),
                        mod_type=(
                            entry.get("mod_type") if entry.get("mod_type") in MOD_TYPES
                            else "client"
                        ),
                        enabled=bool(entry.get("enabled", True)),
                        keys=tuple(str(k) for k in entry.get("keys", [])),
                        size_mb=float(entry.get("size_mb") or 0.0),
                        updated_at=float(entry.get("updated_at") or 0.0),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("Skipping malformed mod entry %r: %s", entry, exc)
        return mods

    def _write(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"mods": [m.to_dict() for m in self._mods]}
        temp = self._path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        temp.replace(self._path)


class ModService:
    """Everything the mods page does, minus the HTTP layer."""

    def __init__(
        self, settings: Settings, steamcmd: SteamCmdService, store: SettingsStore
    ) -> None:
        self.settings = settings
        self.steamcmd = steamcmd
        self.store = store
        self.registry = ModRegistry(settings.paths.panel / "mods.json")
        self._lock = threading.Lock()
        self.sync_launch_settings()

    # --- queries ----------------------------------------------------------

    def list_mods(self) -> list[dict]:
        """Registry entries plus whether the files are actually there."""
        self.adopt_uploaded()
        result = []
        for mod in self.registry.all():
            path = self.settings.paths.server / mod.dir_name
            entry = mod.to_dict()
            # Not "keys": in a template `mod.keys` on a dict resolves to
            # dict.keys, the method, and silently renders nonsense.
            entry["key_files"] = entry.pop("keys")
            entry["installed"] = path.is_dir()
            entry["path"] = str(path)
            entry["local"] = mod.is_local
            # Built here rather than in the template: the search results already
            # link to the same page, and one of the two would drift.
            entry["url"] = "" if mod.is_local else f"{WORKSHOP_ITEM_URL}{mod.workshop_id}"
            result.append(entry)
        return result

    def adopt_uploaded(self) -> list[Mod]:
        """Take mod directories the panel did not install into the list.

        A mod dropped into `server/` by hand or through the file browser is a
        mod the operator wants; leaving it out of the list would mean managing
        it - order, type, keys - by editing the launch parameters instead.

        It comes in **disabled**. Adopting is a discovery, not an instruction:
        a folder appearing on disk must not put itself on the command line of
        the next start.
        """
        known = {m.dir_name for m in self.registry.all()}
        adopted = []
        with self._lock:
            for path in self.settings.paths.installed_mod_dirs():
                if path.name in known or not _has_addons(path):
                    continue
                mod = Mod(
                    workshop_id=_local_id(path.name),
                    name=self._read_mod_name(path) or path.name.lstrip("@"),
                    dir_name=path.name,
                    enabled=False,
                    size_mb=_dir_size_mb(path),
                    updated_at=path.stat().st_mtime,
                )
                log.info("Adopting uploaded mod %s", path.name)
                self.registry.upsert(mod)
                adopted.append(mod)
        return adopted

    def orphan_dirs(self) -> list[str]:
        """@directories on disk that are in the list of neither mods nor mods.

        What is left after adoption is a directory that looks like a mod by its
        name and has no `addons` in it - an interrupted upload, or a folder
        somebody named with an @. Naming it beats silently ignoring it.
        """
        known = {m.dir_name for m in self.registry.all()}
        return sorted(p.name for p in self.settings.paths.installed_mod_dirs()
                      if p.name not in known and not _has_addons(p))

    # --- jobs -------------------------------------------------------------

    def install(self, workshop_id: int, mod_type: str = "client") -> Job:
        if self.registry.get(workshop_id):
            raise ModError(f"Mod {workshop_id} is already installed - use Update.")
        if mod_type not in MOD_TYPES:
            raise ModError("A mod is either a client mod or a server mod.")

        return self._download_job(
            kind="mod_install",
            title=f"Install mod {workshop_id}",
            ids=[workshop_id],
            wipe=False,
            mod_type=mod_type,
        )

    def update(self, workshop_ids: list[int]) -> Job:
        if not workshop_ids:
            raise ModError("No mod selected.")

        # Without this an update of an unknown ID would quietly install it -
        # the same work as Install, but reached through a path where nobody
        # chose whether it is a client or a server mod.
        unknown = [i for i in workshop_ids if self.registry.get(i) is None]
        if unknown:
            raise ModError(
                "Not installed: " + ", ".join(str(i) for i in unknown) + "."
            )

        local = [self.registry.get(i) for i in workshop_ids if i >= LOCAL_ID_BASE]
        if local:
            raise ModError(
                "Uploaded, not from the workshop: "
                + ", ".join(mod.name for mod in local)
                + ". Replace the files to update it."
            )

        title = (
            f"Update mod {workshop_ids[0]}" if len(workshop_ids) == 1
            else f"Update {len(workshop_ids)} mods"
        )

        stale, problem = self.outdated(workshop_ids)
        if not stale:
            return self._nothing_stale_job(title, len(workshop_ids))

        return self._download_job("mod_update", title, stale, wipe=False,
                                  note=self._skip_note(workshop_ids, stale, problem))

    def outdated(self, workshop_ids: list[int]) -> tuple[list[int], str]:
        """Which of these changed on the workshop since they were installed.

        Also returns why the check could not be made, if it could not - and
        then every ID comes back as stale. Skipping a download because Steam
        was unreachable would leave the server on an old mod without saying so.
        """
        known = {mod.workshop_id: mod for mod in self.registry.all()}
        try:
            details = lookup_workshop_items([i for i in workshop_ids if i in known])
        except ModError as exc:
            return list(workshop_ids), str(exc)

        stale = []
        for workshop_id in workshop_ids:
            mod = known.get(workshop_id)
            item = details.get(workshop_id)
            if mod is None or item is None:
                # Nothing to compare against - let SteamCMD have the last word.
                stale.append(workshop_id)
                continue
            # updated_at is when the panel finished installing it, so a workshop
            # update older than that is already on disk. Missing files count as
            # stale whatever the dates say.
            on_disk = (self.settings.paths.server / mod.dir_name).is_dir()
            if not on_disk or item["updated_at"] > mod.updated_at:
                stale.append(workshop_id)
        return stale, ""

    @staticmethod
    def _skip_note(asked: list[int], stale: list[int], problem: str) -> str:
        if problem:
            return f"[panel] Could not ask Steam what changed ({problem}) - updating all."
        skipped = len(asked) - len(stale)
        if not skipped:
            return ""
        return (f"[panel] {skipped} of {len(asked)} mod(s) are already current "
                f"and are not downloaded again.")

    def _nothing_stale_job(self, title: str, count: int) -> Job:
        """A job that finds there is nothing to do, rather than no job at all.

        The callers - the page and the start sequence - both wait on a job and
        report what it says. "Every mod is current" is an outcome worth seeing,
        and it costs no SteamCMD run to reach it.
        """
        def runner(job: Job) -> None:
            job.log_line(f"[panel] All {count} mod(s) are up to date on the workshop.")
            job.detail = "Nothing to update - every mod is current."

        return jobs.start("mod_update", title, runner)

    def reinstall(self, workshop_id: int) -> Job:
        mod = self.registry.get(workshop_id)
        if not mod:
            raise ModError(f"Mod {workshop_id} is not installed.")
        if mod.is_local:
            raise ModError(
                f"{mod.name} was uploaded, not downloaded - there is nothing to "
                "fetch it from."
            )
        return self._download_job(
            "mod_reinstall", f"Reinstall mod {workshop_id}", [workshop_id], wipe=True
        )

    def update_all(self) -> Job | None:
        # Uploaded mods are left out rather than refused: "Update all" is about
        # the ones Steam has a newer copy of.
        ids = [m.workshop_id for m in self.registry.all() if not m.is_local]
        return self.update(ids) if ids else None

    # --- launcher mod lists -----------------------------------------------

    def import_modlist(self, text: str) -> dict:
        """Install a DayZ Launcher preset.

        Everything in the file becomes a client mod: the list is what players
        load, and a mod the players load is by definition not server-only. A
        mod that belongs on the server alone is switched over afterwards, in
        the row it already has.
        """
        try:
            entries = parse_modlist(text)
        except ModlistError as exc:
            raise ModError(str(exc)) from exc

        known = {mod.workshop_id for mod in self.registry.all()}
        fresh = [entry for entry in entries if entry.workshop_id not in known]
        skipped = len(entries) - len(fresh)

        if not fresh:
            raise ModError(
                f"All {len(entries)} mods in that list are already installed."
            )

        title = (
            f"Import {len(fresh)} mod from a mod list" if len(fresh) == 1
            else f"Import {len(fresh)} mods from a mod list"
        )
        job = self._download_job(
            kind="mod_install",
            title=title,
            ids=[entry.workshop_id for entry in fresh],
            wipe=False,
            mod_type="client",
        )
        return {"job": job, "added": len(fresh), "skipped": skipped}

    # --- uploads ----------------------------------------------------------

    def install_upload(self, filename: str, stream) -> tuple[list[Mod], str]:
        """Unpack a zipped mod into the server directory and take it into the list.

        Strict about what it accepts: the archive has to carry the mod folder
        itself, `@Name/addons/...`. Unpacking loose PBOs into a folder named
        after the zip would be guessing at what the mod is called, and a mod
        under the wrong name loads on nobody's server.
        """
        if not filename.lower().endswith(ARCHIVE_SUFFIX):
            raise ModError("A mod is uploaded as a .zip.")

        staged = self.settings.paths.panel / "upload.zip.part"
        try:
            with staged.open("wb") as target:
                shutil.copyfileobj(stream, target)
            with zipfile.ZipFile(staged) as archive:
                roots = _mod_roots(archive.namelist())
                if not roots:
                    raise ModError(
                        "No mod in that zip: it needs a folder starting with @ "
                        "and an addons directory inside it."
                    )
                replaced = self._unpack(archive, roots)
        except zipfile.BadZipFile as exc:
            raise ModError(f"That is not a readable zip: {exc}") from exc
        except OSError as exc:
            raise ModError(f"Could not unpack the upload: {exc}") from exc
        finally:
            staged.unlink(missing_ok=True)

        adopted = self.adopt_uploaded()
        known = {mod.dir_name: mod for mod in self.registry.all()}
        mods = [known[name] for name in roots if name in known]

        what = ", ".join(sorted(roots))
        if replaced:
            message = f"Replaced {what}. It keeps its place in the list."
        elif adopted:
            message = f"Added {what}, switched off. Enable it to load it."
        else:
            message = f"Unpacked {what}."
        return mods, message

    def _unpack(self, archive: zipfile.ZipFile, roots: set[str]) -> bool:
        """Write the mod folders out of the archive, replacing what is there.

        Returns whether anything was replaced. A second upload of the same mod
        is how one updates it, so the old directory goes first - leaving it
        would mix two versions in one folder.
        """
        server = self.settings.paths.server
        root_dir = server.resolve()

        # Every path is checked before the first byte is written. The archive
        # decides where its members land, so it could point out of the tree -
        # and a zip that tries must not have taken half a mod's place by the
        # time that is noticed, least of all the place of the mod it replaces.
        planned = []
        for member in archive.infolist():
            name = member.filename.replace("\\", "/")
            if name.split("/")[0] not in roots:
                continue
            destination = (server / name).resolve()
            if not destination.is_relative_to(root_dir):
                raise ModError(f"That zip writes outside the server directory: {name}")
            planned.append((member, destination))

        replaced = False
        for root in roots:
            target = server / root
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
                replaced = True

        try:
            for member, destination in planned:
                if member.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, destination.open("wb") as out:
                    shutil.copyfileobj(source, out)
        except (OSError, zipfile.BadZipFile):
            # Half a mod would be adopted as a whole one on the next page load.
            for root in roots:
                shutil.rmtree(server / root, ignore_errors=True)
            raise

        # Lowercased like a download, and for the same reason: these files come
        # from the panel's own hand, and DayZ on Linux will not find them
        # otherwise. Files put there past the panel are left as they are.
        for root in roots:
            renamed = _lowercase_tree(server / root)
            if renamed:
                log.info("Lowercased %s path(s) in %s", renamed, root)
        return replaced

    def export_modlist(self) -> str:
        """The enabled mods, client and server alike, as a launcher preset.

        A disabled mod is not on the command line, so the server does not load
        it - handing it to players would have them install something that is
        not being run. An uploaded mod has no workshop entry to point a
        launcher at, so it cannot go in either.
        """
        mods = [mod for mod in self.registry.all() if mod.enabled and not mod.is_local]
        if not mods:
            raise ModError("There are no enabled workshop mods to export.")
        return render_modlist(
            [ModlistEntry(mod.workshop_id, mod.name) for mod in mods]
        )

    def _download_job(
        self,
        kind: str,
        title: str,
        ids: list[int],
        wipe: bool,
        mod_type: str | None = None,
        note: str = "",
    ) -> Job:
        def runner(job: Job) -> None:
            if note:
                job.log_line(note)

            job.detail = f"Downloading {len(ids)} mod(s) in one SteamCMD run"
            downloaded = self.steamcmd.download_workshop_items(job, ids)
            if job.cancelled:
                return

            # Every mod that arrived is installed even when the run as a whole
            # failed: one item Steam would not serve must not throw away the
            # four that came down with it.
            ready, missing = [], []
            for workshop_id in ids:
                try:
                    mod = self._integrate(job, workshop_id, wipe=wipe, mod_type=mod_type)
                except ModError as exc:
                    missing.append(f"{workshop_id}: {exc}")
                    job.log_line(f"[panel] {exc}")
                    continue
                ready.append(mod)
                job.log_line(f"[panel] {mod.name} is ready ({mod.size_mb:.1f} MB)")

            if not downloaded:
                return  # _run already recorded why, and set the state

            if missing:
                job.state = JobState.FAILED
                job.error = "; ".join(missing)
                return

            job.detail = f"{len(ready)} mod(s) ready - restart the server to load them"

        return jobs.start(kind, title, runner)

    # --- installation -----------------------------------------------------

    def _integrate(
        self, job: Job, workshop_id: int, wipe: bool, mod_type: str | None
    ) -> Mod:
        """Copy a downloaded mod into the server directory and register it."""
        with self._lock:
            source = self._source_dir(workshop_id)
            existing = self.registry.get(workshop_id)

            name = self._read_mod_name(source) or (existing.name if existing else "")
            if not name:
                name = f"mod_{workshop_id}"

            # The directory name is fixed at first install. A mod that renames
            # itself upstream would otherwise land in a second directory while
            # the launch parameters still point at the old one.
            dir_name = existing.dir_name if existing else (
                self.settings.paths.mod_dir(workshop_id, name).name
            )
            target = self.settings.paths.server / dir_name

            if wipe and target.exists():
                job.log_line(f"[panel] Removing {dir_name} before reinstalling")
                shutil.rmtree(target, ignore_errors=True)

            job.log_line(f"[panel] Copying to {target}")
            try:
                shutil.copytree(source, target, dirs_exist_ok=True)
            except OSError as exc:
                raise ModError(f"Could not copy the mod: {exc}") from exc

            renamed = _lowercase_tree(target)
            if renamed:
                job.log_line(f"[panel] Lowercased {renamed} path(s) for Linux")

            resolved_type = mod_type or (existing.mod_type if existing else "client")
            mod = Mod(
                workshop_id=workshop_id,
                name=name,
                dir_name=dir_name,
                mod_type=resolved_type,
                enabled=existing.enabled if existing else True,
                keys=(),
                size_mb=_dir_size_mb(target),
                updated_at=time.time(),
            )

            # Carries the old ownership in so the previous keys are dropped:
            # a new version can ship a key under a different name.
            mod = self._apply_keys(
                replace(mod, keys=existing.keys if existing else ()), job.log_line
            )
            self.registry.upsert(mod)

        self.sync_launch_settings()
        return mod

    def _source_dir(self, workshop_id: int) -> Path:
        """Where SteamCMD left the download.

        The primary location follows from force_install_dir, but SteamCMD has
        moved this around between versions, so a miss falls back to a search
        rather than to a misleading "download failed".
        """
        primary = self.settings.paths.workshop / str(workshop_id)
        if primary.is_dir():
            return primary

        pattern = f"**/steamapps/workshop/content/{self.settings.WORKSHOP_APP_ID}/{workshop_id}"
        for candidate in self.settings.paths.steam.glob(pattern):
            if candidate.is_dir():
                return candidate

        raise ModError(
            f"SteamCMD reported success but {primary} does not exist. "
            "The mod may have been removed from the workshop."
        )

    @staticmethod
    def _read_mod_name(source: Path) -> str:
        for candidate in ("meta.cpp", "mod.cpp"):
            path = source / candidate
            if not path.is_file():
                continue
            try:
                match = _META_NAME.search(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            if match and match.group(1).strip():
                return match.group(1).strip()
        return ""

    # --- keys -------------------------------------------------------------

    def _apply_keys(self, mod: Mod, note=lambda _line: None) -> Mod:
        """Bring server/keys in line with what this mod is now.

        Run whenever the answer could have changed - a download, a type switch,
        the enable switch, or the operator asking for it. The old keys go
        first: a mod that no longer wants them, or brought differently named
        ones, must not leave the previous set behind.
        """
        self._drop_keys(mod)
        return replace(mod, keys=self._sync_keys(mod, note))

    def _sync_keys(self, mod: Mod, note) -> tuple[str, ...]:
        """Put a client mod's .bikey into server/keys, keep the others out.

        A key is in keys/ exactly while the mod that ships it is **enabled and
        a client mod**. Disabled means not on the command line, so no client
        loads it and nothing should present its signature; a server mod is not
        loaded by clients either. Both would only widen what the server accepts.

        Returns the key file names now owned by this mod, so removing it later
        can take exactly its keys and nothing else.
        """
        keys_dir = self.settings.paths.keys
        keys_dir.mkdir(parents=True, exist_ok=True)
        source_dir = self.settings.paths.server / mod.dir_name

        found = sorted(p for p in source_dir.rglob("*.bikey") if p.is_file())

        if not mod.is_client or not mod.enabled:
            if found:
                reason = (
                    "it is switched off" if not mod.enabled else
                    "a server mod runs on the server only, so no client "
                    "presents its signature"
                )
                note(f"[panel] {len(found)} key(s) not installed: {reason}")
            return ()

        installed = []
        for key in found:
            try:
                shutil.copy2(key, keys_dir / key.name)
                installed.append(key.name)
            except OSError as exc:
                note(f"[panel] Could not copy {key.name}: {exc}")

        if installed:
            note(f"[panel] Installed key(s): {', '.join(installed)}")
        else:
            note("[panel] No .bikey found - clients can only join with "
                 "verifySignatures = 0")
        return tuple(installed)

    def _drop_keys(self, mod: Mod) -> None:
        """Remove a mod's keys, unless another installed mod ships the same one."""
        others = {
            key
            for other in self.registry.all()
            if other.workshop_id != mod.workshop_id
            for key in other.keys
        }
        for key_name in mod.keys:
            if key_name in others:
                continue
            try:
                (self.settings.paths.keys / key_name).unlink(missing_ok=True)
            except OSError as exc:
                log.warning("Could not remove key %s: %s", key_name, exc)

    # --- changes without a download --------------------------------------

    def set_type(self, workshop_id: int, mod_type: str) -> Mod:
        if mod_type not in MOD_TYPES:
            raise ModError("A mod is either a client mod or a server mod.")

        with self._lock:
            mod = self._require(workshop_id)
            if mod.mod_type == mod_type:
                return mod

            updated = self._apply_keys(replace(mod, mod_type=mod_type))
            self.registry.upsert(updated)

        self.sync_launch_settings()
        return updated

    def set_enabled(self, workshop_id: int, enabled: bool) -> Mod:
        """Switch a mod on or off - and move its keys with it.

        Enabling is the moment a client mod's key starts to matter: it goes on
        the command line, players load it, and the server checks its signature
        against keys/. Switching off takes the key back out, because nothing
        loads the mod any more.
        """
        with self._lock:
            updated = self._apply_keys(
                replace(self._require(workshop_id), enabled=bool(enabled))
            )
            self.registry.upsert(updated)
        self.sync_launch_settings()
        return updated

    def sync_all_keys(self) -> str:
        """Empty server/keys and fill it from the enabled client mods.

        The automatic moments keep keys/ correct as mods come and go. This is
        for when it went wrong anyway: a key deleted in the file browser, mod
        files replaced by hand, a directory restored from an older backup, or
        keys left behind by a panel version that did not clean up after itself.
        Rebuilding beats reconciling - the enabled client mods *are* the answer.

        The DayZ key is not touched. It comes with the server files, belongs to
        no mod, and removing it would reject every player until the files are
        validated again.
        """
        keys_dir = self.settings.paths.keys
        removed, without = 0, []

        with self._lock:
            keys_dir.mkdir(parents=True, exist_ok=True)
            for key in sorted(keys_dir.glob("*.bikey")):
                if key.name.lower() == VANILLA_KEY:
                    continue
                try:
                    key.unlink()
                    removed += 1
                except OSError as exc:
                    log.warning("Could not remove key %s: %s", key.name, exc)

            installed = set()
            for mod in self.registry.all():
                # keys=() because the directory is already empty: there is
                # nothing left of the old set to take back out.
                fresh = self._sync_keys(replace(mod, keys=()), lambda _line: None)
                self.registry.upsert(replace(mod, keys=fresh))
                installed.update(fresh)
                if mod.enabled and mod.is_client and not fresh:
                    without.append(mod.name)

        message = (
            f"server/keys rebuilt: {len(installed)} key(s) from the enabled "
            f"client mods, {removed} removed."
        )
        if without:
            message += " Without a key of their own: " + ", ".join(without) + "."
        return message

    def move(self, workshop_id: int, offset: int) -> None:
        self._require(workshop_id)
        self.registry.move(workshop_id, offset)
        self.sync_launch_settings()

    def delete(self, workshop_id: int, keep_download: bool = True) -> Mod:
        """Remove a mod from the server directory and the registry.

        The workshop cache under /data/steam is kept by default: it is what
        makes reinstalling fast, and it is not what the server reads.
        """
        with self._lock:
            mod = self._require(workshop_id)
            self._drop_keys(mod)

            target = self.settings.paths.server / mod.dir_name
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)

            if not keep_download:
                cache = self.settings.paths.workshop / str(workshop_id)
                if cache.is_dir():
                    shutil.rmtree(cache, ignore_errors=True)

            self.registry.remove(workshop_id)

        self.sync_launch_settings()
        return mod

    def _require(self, workshop_id: int) -> Mod:
        mod = self.registry.get(workshop_id)
        if mod is None:
            raise ModError(f"Mod {workshop_id} is not installed.")
        return mod

    # --- launch parameters ------------------------------------------------

    def sync_launch_settings(self) -> None:
        """Write the mod directories into the stored launch parameters.

        The command line is built from ServerSettings, so the registry has to
        push into it. Doing it here rather than at start time means the settings
        page always shows what the next start will actually use.
        """
        client, server = [], []
        for mod in self.registry.all():
            if not mod.enabled:
                continue
            if not (self.settings.paths.server / mod.dir_name).is_dir():
                continue  # registered but not on disk - never point -mod= at it
            (client if mod.is_client else server).append(mod.dir_name)

        current = self.store.current
        if tuple(client) == current.client_mods and tuple(server) == current.server_mods:
            return
        self.store.update(client_mods=client, server_mods=server)


# --- Steam Web API ------------------------------------------------------------


def lookup_workshop_items(workshop_ids: list) -> dict:
    """Details for several workshop items in one request, keyed by ID.

    The keyless endpoint takes a list, so asking about ten mods costs one call.
    That matters because this runs before every server start, where the point
    is to find out that nothing needs downloading at all.
    """
    ids = list(dict.fromkeys(int(value) for value in workshop_ids))
    if not ids:
        return {}

    params = {"itemcount": str(len(ids))}
    for index, workshop_id in enumerate(ids):
        params[f"publishedfileids[{index}]"] = str(workshop_id)

    data = _api_post("/ISteamRemoteStorage/GetPublishedFileDetails/v1/", params)
    details = (data.get("response") or {}).get("publishedfiledetails") or []

    found = {}
    for item in details:
        if str(item.get("result")) != "1" or not item.get("publishedfileid"):
            continue
        found[int(item["publishedfileid"])] = _item_summary(item)
    return found


def lookup_workshop_item(workshop_id: int, app_id: int) -> dict:
    """Title, size and preview image for one workshop ID.

    Keyless endpoint, so it works out of the box. Used before installing to
    show what is about to be downloaded - and to catch the common case of an ID
    that belongs to a different game entirely.
    """
    data = _api_post(
        "/ISteamRemoteStorage/GetPublishedFileDetails/v1/",
        {"itemcount": "1", "publishedfileids[0]": str(workshop_id)},
    )
    details = (data.get("response") or {}).get("publishedfiledetails") or []
    if not details or str(details[0].get("result")) != "1":
        raise ModError(f"Steam does not know a workshop item with the ID {workshop_id}.")

    item = details[0]
    if item.get("consumer_app_id") and int(item["consumer_app_id"]) != app_id:
        raise ModError(
            f"Workshop item {workshop_id} does not belong to DayZ "
            f"(app {item['consumer_app_id']})."
        )

    return _item_summary(item)


def search_workshop(
    query: str, api_key: str, app_id: int, page: int = 1, per_page: int = SEARCH_PER_PAGE
) -> dict:
    """One page of DayZ workshop results. Needs a Steam Web API key.

    Steam offers no keyless search endpoint. Rather than scraping the store
    page - which breaks whenever Valve touches the markup - searching is simply
    unavailable until a key is configured, and installing by ID always works.

    Paging uses `page`, not `cursor`. The cursor is what Valve documents for
    deep paging, but measured against this endpoint it returns overlapping
    pages - the second cursor page repeated half of the first. `page` returns
    clean, disjoint pages.
    """
    if not api_key:
        raise ModError(
            "Searching needs a Steam Web API key. Set STEAM_API_KEY in .env "
            "(get one at steamcommunity.com/dev/apikey), or paste a workshop ID "
            "or URL above."
        )

    page = max(1, int(page or 1))
    params = {
        "key": api_key,
        "appid": str(app_id),
        "search_text": query,
        "numperpage": str(per_page),
        "page": str(page),
        "return_metadata": "true",
        "return_short_description": "true",
        # 12 = ranked by text search when there is a query, 9 = most subscribed.
        "query_type": "12" if query.strip() else "9",
    }
    response = _api_get("/IPublishedFileService/QueryFiles/v1/", params).get("response") or {}
    items = response.get("publishedfiledetails") or []
    total = int(response.get("total") or 0)

    return {
        "items": [_item_summary(item) for item in items if item.get("publishedfileid")],
        "total": total,
        "page": page,
        "pages": max(1, -(-total // per_page)),  # ceil
    }


def _item_summary(item: dict) -> dict:
    size = item.get("file_size") or 0
    try:
        size_mb = round(int(size) / (1024 * 1024), 1)
    except (TypeError, ValueError):
        size_mb = 0.0

    workshop_id = int(item["publishedfileid"])
    return {
        "workshop_id": workshop_id,
        "title": (item.get("title") or "").strip() or f"Mod {item['publishedfileid']}",
        "preview_url": item.get("preview_url") or "",
        "url": f"{WORKSHOP_ITEM_URL}{workshop_id}",
        "size_mb": size_mb,
        "updated_at": int(item.get("time_updated") or 0),
        "banned": bool(item.get("banned")),
        "description": (item.get("short_description") or "")[:200],
    }


def _api_get(path: str, params: dict) -> dict:
    url = f"{API_BASE}{path}?{urllib.parse.urlencode(params)}"
    return _api_call(urllib.request.Request(url, method="GET"))


def _api_post(path: str, params: dict) -> dict:
    return _api_call(
        urllib.request.Request(
            f"{API_BASE}{path}",
            data=urllib.parse.urlencode(params).encode(),
            method="POST",
        )
    )


def _api_call(request: urllib.request.Request) -> dict:
    try:
        with urllib.request.urlopen(request, timeout=API_TIMEOUT) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ModError("Steam rejected the API key.") from exc
        raise ModError(f"Steam replied with HTTP {exc.code}.") from exc
    except urllib.error.URLError as exc:
        raise ModError(f"Could not reach the Steam API: {exc.reason}") from exc
    except (json.JSONDecodeError, ValueError) as exc:
        raise ModError("The Steam API sent something unreadable.") from exc


# --- filesystem helpers -------------------------------------------------------


def _lowercase_tree(root: Path) -> int:
    """Lowercase every file and directory name below `root`.

    Bottom-up, so renaming a directory cannot invalidate paths still to be
    visited. Returns how many entries were renamed.
    """
    renamed = 0
    for current, dirs, files in os.walk(root, topdown=False):
        for name in files + dirs:
            lowered = name.lower()
            if lowered == name:
                continue
            source = Path(current) / name
            target = Path(current) / lowered
            try:
                # samefile, not exists: on a case-insensitive mount - which is
                # what a bind mount from a Windows host is - the lowercase name
                # already resolves to this very file. Treating that as a
                # collision would skip every rename there.
                if target.exists() and not source.samefile(target):
                    # Two genuinely different entries differing only in case:
                    # keep the newer one, which is what a case-insensitive
                    # filesystem would have left behind.
                    if source.stat().st_mtime <= target.stat().st_mtime:
                        continue
                    if target.is_dir():
                        shutil.rmtree(target, ignore_errors=True)
                    else:
                        target.unlink(missing_ok=True)
                source.rename(target)
                renamed += 1
            except OSError as exc:
                log.warning("Could not rename %s: %s", source, exc)
    return renamed


def _mod_roots(names: list[str]) -> set[str]:
    """The `@Name` folders in an archive that actually hold a mod.

    Same test as on disk, one level up: a top folder starting with @, with an
    addons directory in it. Anything else in the zip - a readme beside it, a
    second folder without addons - is left where it is.
    """
    roots = set()
    for raw in names:
        parts = [part for part in raw.replace("\\", "/").split("/") if part]
        if len(parts) < 2 or not parts[0].startswith("@"):
            continue
        if parts[1].lower() == ADDONS_DIR:
            roots.add(parts[0])
    return roots


def _has_addons(path: Path) -> bool:
    """`@Name/addons/` - the two marks of a mod directory.

    Case-insensitive on the addons folder: mods are built on Windows, and one
    that arrives as `Addons` is still a mod. The server needs it lowercased,
    which is a separate matter and says so on the page.
    """
    return any(
        child.is_dir() and child.name.lower() == ADDONS_DIR
        for child in _children(path)
    )


def _children(path: Path) -> list[Path]:
    try:
        return list(path.iterdir())
    except OSError:
        return []


def _local_id(dir_name: str) -> int:
    """A stable ID for an uploaded mod, derived from its directory name.

    Derived rather than counted up: the same folder has to come back with the
    same ID after a restart, and the folder name is the only thing about it
    that is guaranteed to be there.
    """
    return LOCAL_ID_BASE + zlib.crc32(dir_name.encode("utf-8"))


def _dir_size_mb(path: Path) -> float:
    total = 0
    for current, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(current) / name).stat().st_size
            except OSError:
                pass
    return round(total / (1024 * 1024), 1)
