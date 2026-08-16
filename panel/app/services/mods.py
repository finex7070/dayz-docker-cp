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
import urllib.error
import urllib.parse
import urllib.request
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
        result = []
        for mod in self.registry.all():
            path = self.settings.paths.server / mod.dir_name
            entry = mod.to_dict()
            # Not "keys": in a template `mod.keys` on a dict resolves to
            # dict.keys, the method, and silently renders nonsense.
            entry["key_files"] = entry.pop("keys")
            entry["installed"] = path.is_dir()
            entry["path"] = str(path)
            result.append(entry)
        return result

    def orphan_dirs(self) -> list[str]:
        """@directories on disk that no registry entry claims.

        Usually a leftover from a mod installed by hand or removed while the
        panel was down. Naming them beats silently ignoring a directory that
        still takes up disk space.
        """
        known = {m.dir_name for m in self.registry.all()}
        return sorted(p.name for p in self.settings.paths.installed_mod_dirs()
                      if p.name not in known)

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

        title = (
            f"Update mod {workshop_ids[0]}" if len(workshop_ids) == 1
            else f"Update {len(workshop_ids)} mods"
        )
        return self._download_job("mod_update", title, workshop_ids, wipe=False)

    def reinstall(self, workshop_id: int) -> Job:
        if not self.registry.get(workshop_id):
            raise ModError(f"Mod {workshop_id} is not installed.")
        return self._download_job(
            "mod_reinstall", f"Reinstall mod {workshop_id}", [workshop_id], wipe=True
        )

    def update_all(self) -> Job | None:
        ids = [m.workshop_id for m in self.registry.all()]
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

    def export_modlist(self) -> str:
        """The enabled mods, client and server alike, as a launcher preset.

        A disabled mod is not on the command line, so the server does not load
        it - handing it to players would have them install something that is
        not being run.
        """
        mods = [mod for mod in self.registry.all() if mod.enabled]
        if not mods:
            raise ModError("There are no enabled mods to export.")
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
    ) -> Job:
        def runner(job: Job) -> None:
            for index, workshop_id in enumerate(ids, start=1):
                if job.cancelled:
                    return
                job.detail = f"Downloading mod {workshop_id} ({index}/{len(ids)})"
                job.log_line(f"[panel] --- mod {workshop_id} ({index}/{len(ids)}) ---")

                if not self.steamcmd.download_workshop_item(job, workshop_id):
                    return  # _run already recorded why

                try:
                    mod = self._integrate(job, workshop_id, wipe=wipe, mod_type=mod_type)
                except ModError as exc:
                    job.state = JobState.FAILED
                    job.error = str(exc)
                    job.log_line(f"[panel] {exc}")
                    return
                job.log_line(f"[panel] {mod.name} is ready ({mod.size_mb:.1f} MB)")

            job.detail = f"{len(ids)} mod(s) ready - restart the server to load them"

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

            mod = replace(mod, keys=self._sync_keys(mod, job.log_line))
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

    def _sync_keys(self, mod: Mod, note) -> tuple[str, ...]:
        """Put a client mod's .bikey into server/keys, keep a server mod's out.

        Returns the key file names now owned by this mod, so removing it later
        can take exactly its keys and nothing else.
        """
        keys_dir = self.settings.paths.keys
        keys_dir.mkdir(parents=True, exist_ok=True)
        source_dir = self.settings.paths.server / mod.dir_name

        found = sorted(p for p in source_dir.rglob("*.bikey") if p.is_file())

        if not mod.is_client:
            if found:
                note(
                    f"[panel] {len(found)} key(s) not installed: a server mod runs "
                    "on the server only, so no client presents its signature"
                )
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

            # Drop the old keys first: switching to a server mod has to take
            # them back out of the server's keys directory.
            self._drop_keys(mod)
            updated = replace(mod, mod_type=mod_type, keys=())
            updated = replace(updated, keys=self._sync_keys(updated, lambda _m: None))
            self.registry.upsert(updated)

        self.sync_launch_settings()
        return updated

    def set_enabled(self, workshop_id: int, enabled: bool) -> Mod:
        with self._lock:
            updated = replace(self._require(workshop_id), enabled=bool(enabled))
            self.registry.upsert(updated)
        self.sync_launch_settings()
        return updated

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


def _dir_size_mb(path: Path) -> float:
    total = 0
    for current, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(current) / name).stat().st_size
            except OSError:
                pass
    return round(total / (1024 * 1024), 1)
