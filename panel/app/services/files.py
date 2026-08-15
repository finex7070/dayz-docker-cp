"""File browser confined to the server directory.

The whole module exists around one rule: **every path is resolved and checked
against the root before anything touches it.** Not sanitised - resolved. Cutting
`..` out of a string is a game one loses eventually, and it would not catch a
symlink at all: `/data/server/mpmissions/link -> /etc` contains no `..` and is
still a way out. `Path.resolve()` follows the symlink, and the result either
sits under the root or it does not.

What can be edited is decided by looking at the file, not at its name. A
`.txt` full of NUL bytes is not text, and a mission file without a suffix is.
Editing a binary in a textarea would silently destroy it on save, so binaries
are offered for download only.
"""

from __future__ import annotations

import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# Read for the binary sniff, and the ceiling for the editor. 2 MB is already
# far past what anyone edits in a textarea; beyond it the browser is the thing
# that breaks, not the panel.
SNIFF_BYTES = 8192
MAX_EDIT_BYTES = 2 * 1024 * 1024

# Backups keep this many copies per file, mirroring the tree so that three
# different types.xml do not overwrite each other in one flat directory.
KEEP_BACKUPS = 20

# Rejected in uploaded and created names. The separators would turn one name
# into a path, and NUL ends it early for anything below Python.
BAD_NAME_CHARS = ("/", "\\", "\x00")


class FileError(ValueError):
    """A refused operation, phrased for the person who asked for it."""


@dataclass(frozen=True)
class Entry:
    name: str
    path: str          # relative to the root, '/' separated
    is_dir: bool
    size: int
    modified: float

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "path": self.path,
            "is_dir": self.is_dir,
            "size": self.size,
            "modified": self.modified,
            "modified_text": time.strftime("%Y-%m-%d %H:%M", time.localtime(self.modified))
            if self.modified else "—",
        }


class FileService:
    def __init__(self, root: Path, backup_dir: Path) -> None:
        self._root = root
        self._backup_dir = backup_dir

    @property
    def root(self) -> Path:
        return self._root

    # --- the one rule ------------------------------------------------------

    def resolve(self, relative: str) -> Path:
        """Turn a browser-supplied path into a real one inside the root.

        Raises FileError for anything that leaves it - including via a symlink,
        which is why this resolves rather than joins.
        """
        # Nothing is cut out of the string beyond the leading separators -
        # `..` is allowed to survive here precisely so that resolve() can turn
        # it into a real path and the check below can reject it.
        text = (relative or "").strip().replace("\\", "/").strip("/")

        try:
            root = self._root.resolve()
            target = (root / text).resolve() if text else root
        except OSError as exc:
            raise FileError(f"That path cannot be read: {exc}") from exc

        if target != root and root not in target.parents:
            raise FileError("That path leads outside the server directory.")
        return target

    def relative(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self._root.resolve()).as_posix()
        except ValueError:
            return ""

    # --- browsing ----------------------------------------------------------

    def listing(self, relative: str = "") -> dict:
        directory = self.resolve(relative)
        if not directory.is_dir():
            raise FileError("That is not a directory.")

        entries = []
        for child in directory.iterdir():
            try:
                stat = child.stat()
                is_dir = child.is_dir()
            except OSError:
                # A broken symlink or a file that vanished mid-listing is worth
                # showing as an entry, not worth failing the whole page for.
                stat, is_dir = None, False
            entries.append(
                Entry(
                    name=child.name,
                    path=self.relative(child) or child.name,
                    is_dir=is_dir,
                    size=stat.st_size if stat and not is_dir else 0,
                    modified=stat.st_mtime if stat else 0.0,
                )
            )

        # Directories first, then by name: the order a file manager uses,
        # and the one that makes a long mpmissions tree navigable.
        entries.sort(key=lambda item: (not item.is_dir, item.name.lower()))
        current = self.relative(directory)
        return {
            "path": current,
            "parent": current.rpartition("/")[0] if current else None,
            "crumbs": self._crumbs(current),
            "entries": [entry.to_dict() for entry in entries],
        }

    @staticmethod
    def _crumbs(current: str) -> list[dict]:
        crumbs = [{"name": "server", "path": ""}]
        walked = ""
        for part in filter(None, current.split("/")):
            walked = f"{walked}/{part}" if walked else part
            crumbs.append({"name": part, "path": walked})
        return crumbs

    # --- reading -----------------------------------------------------------

    def read(self, relative: str) -> dict:
        path = self.resolve(relative)
        if not path.is_file():
            raise FileError("That file does not exist.")

        size = path.stat().st_size
        if is_binary(path):
            raise FileError(
                "That file is not text. Use Download - editing it here would "
                "corrupt it."
            )
        if size > MAX_EDIT_BYTES:
            raise FileError(
                f"That file is {size // 1024} KB. Anything above "
                f"{MAX_EDIT_BYTES // 1024} KB is download only - a textarea "
                "that large locks up the browser."
            )

        try:
            # Deliberately not errors="replace": that would show the file as
            # readable and then write the replacement characters back over the
            # bytes it could not decode.
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise FileError(
                "That file is not valid UTF-8 text. Use Download - saving it "
                "here would change bytes the panel cannot read."
            ) from None

        return {
            "path": self.relative(path),
            "name": path.name,
            "size": size,
            "text": text,
            "modified": path.stat().st_mtime,
        }

    def download_path(self, relative: str) -> Path:
        path = self.resolve(relative)
        if not path.is_file():
            raise FileError("That file does not exist.")
        return path

    # --- writing -----------------------------------------------------------

    def write(self, relative: str, text: str) -> None:
        """Save a text file.

        No backup, deliberately: the editor showed the previous contents and
        the change was made on purpose. Saving a file twice while working on it
        would otherwise fill /data/backup with copies of the same edit, and
        bury the copies that do matter - the ones from a delete or an upload,
        where the old contents disappear without anyone having seen them.
        """
        path = self.resolve(relative)
        if not path.is_file():
            raise FileError("That file does not exist.")
        if is_binary(path):
            raise FileError("That file is not text and cannot be saved from here.")

        # Written through a temporary file in the same directory: a crash
        # halfway through a direct write leaves a truncated types.xml, and the
        # server would not start on it.
        temp = path.with_name(path.name + ".panel-tmp")
        temp.write_text(text.replace("\r\n", "\n"), encoding="utf-8")
        temp.replace(path)

    def upload(self, relative_dir: str, filename: str, stream) -> str:
        directory = self.resolve(relative_dir)
        if not directory.is_dir():
            raise FileError("That is not a directory.")

        name = check_name(filename)
        target = directory / name
        if target.is_dir():
            raise FileError(f"'{name}' is a directory here.")
        if target.is_file():
            self.backup(target)

        temp = target.with_name(target.name + ".panel-tmp")
        with temp.open("wb") as handle:
            shutil.copyfileobj(stream, handle)
        temp.replace(target)
        return self.relative(target)

    def rename(self, relative: str, new_name: str) -> str:
        """Rename in place. The new name is a name, never a path."""
        path = self.resolve(relative)
        if path == self._root.resolve():
            raise FileError("The server directory itself cannot be renamed.")
        if not path.exists():
            raise FileError("That no longer exists.")

        target = path.with_name(check_name(new_name))
        if target == path:
            return self.relative(path)
        if target.exists():
            raise FileError(f"'{target.name}' already exists here.")

        path.rename(target)
        return self.relative(target)

    def move(self, relative: str, target_dir: str) -> str:
        """Move into another directory inside the root."""
        path = self.resolve(relative)
        if path == self._root.resolve():
            raise FileError("The server directory itself cannot be moved.")
        if not path.exists():
            raise FileError("That no longer exists.")

        directory = self.resolve(target_dir)
        if not directory.is_dir():
            raise FileError(f"'{target_dir or '/'}' is not a directory.")
        # Moving a directory into itself would detach the whole subtree from
        # the filesystem - Python does it without complaining.
        if path == directory or path in directory.parents:
            raise FileError("A directory cannot be moved into itself.")

        target = directory / path.name
        if target.exists():
            raise FileError(f"'{path.name}' already exists in that directory.")

        path.rename(target)
        return self.relative(target)

    def create_file(self, relative_dir: str, name: str) -> str:
        """Create an empty text file, so the editor has something to open."""
        directory = self.resolve(relative_dir)
        if not directory.is_dir():
            raise FileError("That is not a directory.")

        target = directory / check_name(name)
        if target.exists():
            raise FileError(f"'{target.name}' already exists here.")

        target.write_text("", encoding="utf-8")
        return self.relative(target)

    def make_directory(self, relative_dir: str, name: str) -> str:
        directory = self.resolve(relative_dir)
        if not directory.is_dir():
            raise FileError("That is not a directory.")

        target = directory / check_name(name)
        if target.exists():
            raise FileError(f"'{target.name}' already exists here.")
        target.mkdir()
        return self.relative(target)

    def delete(self, relative: str) -> str:
        path = self.resolve(relative)
        if path == self._root.resolve():
            raise FileError("The server directory itself cannot be deleted.")
        if not path.exists():
            raise FileError("That no longer exists.")

        if path.is_dir():
            # A recursive delete of a mod or mission folder is not something to
            # offer behind a single click; the bind mount is the right tool.
            if any(path.iterdir()):
                raise FileError(
                    "That directory is not empty. Empty it first, or remove it "
                    "from the host - a recursive delete is not offered here."
                )
            path.rmdir()
            return "directory"

        self.backup(path)
        path.unlink()
        return "file"

    # --- backups -----------------------------------------------------------

    def backup(self, path: Path) -> str | None:
        """Copy a file into /data/backup, mirroring its place in the tree."""
        if not path.is_file():
            return None

        relative = self.relative(path)
        target_dir = self._backup_dir / "files" / (relative.rpartition("/")[0])
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            target = target_dir / f"{path.name}.{stamp}"

            counter = 1
            while target.exists():
                target = target_dir / f"{path.name}.{stamp}-{counter}"
                counter += 1

            shutil.copy2(path, target)
        except OSError as exc:
            # A failed backup must not block the edit: the operator asked to
            # save, and refusing would be surprising. It is logged instead.
            log.warning("Could not back up %s: %s", path, exc)
            return None

        _prune(target_dir, path.name, KEEP_BACKUPS)
        return str(target)


def _prune(directory: Path, prefix: str, keep: int) -> None:
    copies = sorted(directory.glob(f"{prefix}.*"), key=lambda p: p.name, reverse=True)
    for stale in copies[keep:]:
        try:
            stale.unlink()
        except OSError:
            pass


def check_name(value: str) -> str:
    """A single file or directory name - never a path."""
    name = (value or "").strip()
    if not name or name in {".", ".."}:
        raise FileError("A name is required.")
    if any(char in name for char in BAD_NAME_CHARS):
        raise FileError("A name cannot contain a slash.")
    if name.endswith(".panel-tmp"):
        raise FileError("That suffix is reserved by the panel.")
    return name


def is_binary(path: Path) -> bool:
    """A NUL byte in the first block means it is not something to edit as text.

    The same test `file` and `git` use, and for the same reason: extensions lie
    in both directions in a DayZ server tree.
    """
    try:
        with path.open("rb") as handle:
            head = handle.read(SNIFF_BYTES)
    except OSError:
        return True

    if b"\x00" in head:
        return True
    try:
        head.decode("utf-8")
    except UnicodeDecodeError as exc:
        # A multi-byte character cut in half by the sniff boundary is not a
        # binary file - only a failure before the last few bytes counts.
        return exc.start < len(head) - 4
    return False
