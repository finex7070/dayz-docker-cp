"""Config files the panel writes immediately before every server start.

Five values are not maintained by hand: `steamQueryPort` and the `class
Missions` template in serverDZ.cfg, `RConPort`/`RConPassword` in
beserver_x64.cfg and `maxcores` in dayzsetting.xml. They live in config files
but have to match what the container publishes and what the panel knows, and a
hand-maintained copy of a value drifts sooner or later.

steamQueryPort is the one that goes wrong quietly: if it disagrees with the
mapped UDP port, the server runs, players can join by direct connect, and it
simply never appears in the server browser.

Both files are edited **line by line**. Regenerating them from a template
would be less code but would silently discard comments, `class Missions` and
every hand-made addition - and those are exactly what an operator adds.
"""

from __future__ import annotations

import logging
import re
import shutil
import time
from pathlib import Path

log = logging.getLogger(__name__)

# serverDZ.cfg:  key = value;   // comment
_CFG_LINE = r"^(?P<lead>\s*{key}\s*=\s*)(?P<value>[^;]*)(?P<tail>;.*)$"

# beserver_x64.cfg:  Key value      (space separated, no '=' and no semicolon)
_BE_LINE = r"^(?P<lead>\s*{key}\s+)(?P<value>.*)$"

# serverDZ.cfg:  class Missions { class DayZ { template = "..."; }; };
_TEMPLATE = re.compile(r'\btemplate\s*=\s*"(?P<value>[^"]*)"', re.IGNORECASE)

# dayzsetting.xml:  <jobsystem ...> <pc maxcores="4" reservedcores="1" /> ...
# Edited as text, not through an XML parser: a round trip through ElementTree
# rewrites attribute order, quoting and indentation of the whole file, and this
# one is also open to the operator in the file editor.
_JOBSYSTEM = re.compile(r"<jobsystem\b.*?</jobsystem>", re.IGNORECASE | re.DOTALL)
_PC_TAG = re.compile(r"<pc\b[^>]*>", re.IGNORECASE)
_MAX_CORES = re.compile(r'(?P<lead>\bmaxcores\s*=\s*")(?P<value>[^"]*)(?P<tail>")', re.IGNORECASE)


def set_cfg_value(path: Path, key: str, value: object) -> bool:
    """Set `key = value;` in a DayZ style config, keeping everything else.

    Returns True when the file changed. A missing key is appended rather than
    ignored - the operator may well have deleted the line.
    """
    if not path.is_file():
        return False

    pattern = re.compile(_CFG_LINE.format(key=re.escape(key)), re.IGNORECASE)
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()

    replaced = False
    for index, line in enumerate(lines):
        match = pattern.match(line)
        if not match:
            continue
        if match.group("value").strip() == str(value):
            return False  # already correct, leave the file alone
        lines[index] = f"{match.group('lead')}{value}{match.group('tail')}"
        replaced = True
        break

    if not replaced:
        lines.append(f"{key} = {value};   // written by the control panel")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def read_cfg_values(path: Path, keys) -> dict[str, str]:
    """Read `key = value;` pairs, with the quotes stripped off strings.

    Only the requested keys are returned. Anything else in the file - comments,
    class blocks, settings the panel does not know - is none of the form's
    business and must survive untouched.
    """
    if not path.is_file():
        return {}

    patterns = {
        key: re.compile(_CFG_LINE.format(key=re.escape(key)), re.IGNORECASE)
        for key in keys
    }
    found: dict[str, str] = {}

    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        for key, pattern in patterns.items():
            if key in found:
                continue
            match = pattern.match(line)
            if match:
                found[key] = match.group("value").strip().strip('"')
                break

    return found


def backup_config(path: Path, backup_dir: Path, keep: int = 20) -> Path | None:
    """Copy a config aside before it is edited.

    A form that writes into serverDZ.cfg is the one place where a mistake can
    cost work that was done by hand. The copies are plain files in /data/backup,
    so restoring one needs nothing but a file manager.
    """
    if not path.is_file():
        return None

    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"{path.name}.{stamp}"

    # Same second, second edit: do not silently overwrite the older copy.
    counter = 1
    while target.exists():
        target = backup_dir / f"{path.name}.{stamp}-{counter}"
        counter += 1

    try:
        shutil.copy2(path, target)
    except OSError as exc:
        log.warning("Could not back up %s: %s", path, exc)
        return None

    _prune_backups(backup_dir, path.name, keep)
    return target


def _prune_backups(backup_dir: Path, prefix: str, keep: int) -> None:
    copies = sorted(backup_dir.glob(f"{prefix}.*"), key=lambda p: p.name, reverse=True)
    for stale in copies[keep:]:
        try:
            stale.unlink()
        except OSError:
            pass


def set_battleye_value(path: Path, key: str, value: object) -> None:
    """Set `Key value` in a BattlEye config, keeping every other line."""
    pattern = re.compile(_BE_LINE.format(key=re.escape(key)), re.IGNORECASE)
    lines = (
        path.read_text(encoding="utf-8", errors="replace").splitlines()
        if path.is_file()
        else []
    )

    for index, line in enumerate(lines):
        match = pattern.match(line)
        if match:
            lines[index] = f"{match.group('lead')}{value}"
            break
    else:
        lines.append(f"{key} {value}")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _reclaim_active_config(paths, note) -> None:
    """Take the running copy back as beserver_x64.cfg.

    BattlEye does not copy its config on startup, it **renames** it to
    beserver_x64_active_<random>.cfg. So after the first start the original is
    gone, and simply writing a fresh one would drop everything the operator had
    added to it (MaxPing, RestrictRCon, ...). Renaming the newest active copy
    back keeps those lines; only then are the rest deleted.
    """
    if paths.battleye_config.is_file():
        return

    active = paths.stale_battleye_configs()
    if not active:
        return

    newest = max(active, key=lambda p: p.stat().st_mtime)
    try:
        newest.replace(paths.battleye_config)
        note(f"[panel] Recovered BattlEye config from {newest.name}")
    except OSError as exc:
        log.warning("Could not recover %s: %s", newest, exc)


def remove_battleye_key(path: Path, key: str) -> bool:
    """Drop a `Key value` line entirely. Returns True when one was removed."""
    if not path.is_file():
        return False

    pattern = re.compile(_BE_LINE.format(key=re.escape(key)), re.IGNORECASE)
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    kept = [line for line in lines if not pattern.match(line)]
    if len(kept) == len(lines):
        return False

    path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    return True


def set_mission(path: Path, mission: str) -> bool:
    """Point `class Missions` in serverDZ.cfg at the selected mission.

    `-mission=` on the command line is not what the engine loads; the template
    inside this block is. Setting the mission on the settings page and leaving
    the block behind would start the wrong map with no error anywhere.

    Returns True when the file changed. The block is found by counting braces
    rather than by matching `template =` anywhere in the file: the key is
    generic enough that a hand-added block elsewhere would be rewritten by a
    plain line match.
    """
    if not path.is_file() or not mission:
        return False
    if any(ch in mission for ch in '";\n\r'):
        # Would end the statement early and break the file. The settings store
        # rejects these already; this is the second lock on the same door.
        log.warning("Refusing to write mission %r into %s", mission, path)
        return False

    text = path.read_text(encoding="utf-8", errors="replace")
    header = re.search(r"class\s+Missions\b", text, re.IGNORECASE)

    if header is None:
        # No block at all: the server would have no mission to load, so a
        # correct one is better than a warning about a file that cannot start.
        addition = (
            "\nclass Missions\n{\n    class DayZ\n    {\n"
            f'        template = "{mission}";   // written by the control panel\n'
            "    };\n};\n"
        )
        path.write_text(text.rstrip("\n") + "\n" + addition, encoding="utf-8")
        return True

    block = _missions_block(text, header)
    if block is None:
        # Braces that never close. Appending a second block would make a
        # damaged file worse, and where the mission belongs is not knowable.
        log.warning("class Missions in %s is not closed - left alone", path)
        return False

    start, end = block
    current = _TEMPLATE.search(text, start, end)
    if current is None:
        log.warning("No template line inside class Missions in %s", path)
        return False
    if current.group("value") == mission:
        return False  # already correct, leave the file alone

    path.write_text(
        text[:current.start("value")] + mission + text[current.end("value"):],
        encoding="utf-8",
    )
    return True


def _missions_block(text: str, header: "re.Match[str]") -> tuple[int, int] | None:
    """Span of the `class Missions { ... }` block, braces counted."""
    open_brace = text.find("{", header.end())
    if open_brace < 0:
        return None

    depth = 0
    for index in range(open_brace, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return header.start(), index + 1
    return None  # unbalanced - not a file to start editing


def set_max_cores(path: Path, cores: int) -> bool:
    """Set `maxcores` on the `<pc>` element in dayzsetting.xml.

    The engine's job system sizes its worker pool from this attribute, while
    -cpuCount only covers the main simulation threads. Leaving the two to
    disagree is a quiet way to hand the container more threads than it has
    cores, so the CPU count on the settings page writes both.

    Returns True when the file changed. Only that one attribute is touched:
    everything else in there describes a game client and is none of our
    business. A file without a <jobsystem><pc> block is left alone - a version
    of DayZ that moved the setting is better reported than guessed at.
    """
    if not path.is_file():
        return False

    text = path.read_text(encoding="utf-8", errors="replace")

    block = _JOBSYSTEM.search(text)
    tag = _PC_TAG.search(text, block.start(), block.end()) if block else None
    if tag is None:
        log.warning("No <jobsystem><pc> element in %s - maxcores not written", path)
        return False

    old = tag.group(0)
    current = _MAX_CORES.search(old)
    if current is None:
        # Absent rather than wrong: add it instead of rewriting the element,
        # which keeps reservedcores and anything else the operator set.
        new = f'{old[:3]} maxcores="{cores}"{old[3:]}'
    elif current.group("value").strip() == str(cores):
        return False  # already correct, leave the file alone
    else:
        new = _MAX_CORES.sub(rf'\g<lead>{cores}\g<tail>', old, count=1)

    path.write_text(text[:tag.start()] + new + text[tag.end():], encoding="utf-8")
    return True


def prepare(settings, server_settings, note=lambda _msg: None) -> None:
    """Write everything that must be current when the server comes up.

    `note` receives one line per change so the caller can put it in front of
    the server's own output - a wrong port is far easier to spot there than in
    a config file nobody opens.
    """
    paths = settings.paths
    paths.profiles.mkdir(parents=True, exist_ok=True)

    if set_cfg_value(paths.server_config, "steamQueryPort", settings.steam_query_port):
        note(f"[panel] serverDZ.cfg: steamQueryPort = {settings.steam_query_port}")

    # The mission the settings page selected. -mission= alone is not enough:
    # the engine loads what the Missions block names.
    if set_mission(paths.server_config, server_settings.mission):
        note(f"[panel] serverDZ.cfg: mission template = {server_settings.mission}")

    # Also written when the settings form is saved. Repeated here because the
    # file arrives with the server files: a CPU count set before the first
    # install would otherwise never reach the copy SteamCMD puts down.
    if set_max_cores(paths.dayzsetting, server_settings.cpu_count):
        note(f"[panel] dayzsetting.xml: maxcores = {server_settings.cpu_count}")

    _prepare_battleye(settings, server_settings, note)


def _prepare_battleye(settings, server_settings, note) -> None:
    paths = settings.paths
    paths.battleye.mkdir(parents=True, exist_ok=True)

    _reclaim_active_config(paths, note)

    # Whatever is left is from an older run. Left in place, an old copy can
    # keep an old RCON password alive after it was changed here.
    for stale in paths.stale_battleye_configs():
        try:
            stale.unlink()
            note(f"[panel] Removed stale BattlEye config {stale.name}")
        except OSError as exc:
            log.warning("Could not remove %s: %s", stale, exc)

    set_battleye_value(paths.battleye_config, "RConPort", settings.rcon_port)
    set_battleye_value(
        paths.battleye_config, "RestrictRCon", 1 if server_settings.rcon_restrict else 0
    )

    if server_settings.rcon_password:
        set_battleye_value(
            paths.battleye_config, "RConPassword", server_settings.rcon_password
        )
        note(f"[panel] BattlEye: RCON on port {settings.rcon_port}")
    else:
        # An empty RConPassword would enable RCON without one, and a password
        # that was cleared here has to disappear from the file as well -
        # otherwise the old one keeps working. No key at all means no RCON.
        remove_battleye_key(paths.battleye_config, "RConPassword")
        note("[panel] BattlEye: RCON disabled (no password set)")

    try:
        paths.battleye_config.chmod(0o600)
    except OSError:
        pass
