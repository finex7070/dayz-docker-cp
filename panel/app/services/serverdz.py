"""The serverDZ.cfg values the settings page can edit.

Deliberately a fixed list rather than a generic config editor. serverDZ.cfg
holds a `class Missions` block and settings whose wrong value stops the server
from starting; a free-form editor over that file invites exactly the mistakes
that are hardest to diagnose from a log. The keys below are the ones an
operator actually changes, each with a type and a range, so a bad value is
rejected in the form instead of at the next start.

Everything not listed here stays in the file, untouched - including comments
and anything the operator added by hand. See server_config.set_cfg_value.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .server_config import read_cfg_values, set_cfg_value


class CfgError(ValueError):
    """A submitted value cannot go into the config - shown on the form."""


@dataclass(frozen=True)
class CfgField:
    key: str
    label: str
    # "text" | "password" | "int" | "float" | "choice"
    # "bool"     - written as 0/1, which is what most of this file uses
    # "boolword" - written as false/true, which disableBanlist and
    #              disablePrioritylist use. Writing 0 there would not be the
    #              same value, so the two spellings stay apart.
    kind: str
    help: str = ""
    minimum: float | None = None
    maximum: float | None = None
    max_length: int | None = None
    choices: tuple[tuple[str, str], ...] = ()  # (value, label)
    optional: bool = False    # empty is allowed and means "leave it out"

    @property
    def is_switch(self) -> bool:
        return self.kind in {"bool", "boolword"}


# The complete "main" block of serverDZ.cfg as the DayZ documentation lists
# it, grouped the way the form shows it. Each group is one card, holding the
# settings an operator would decide on together: the player limit sits with the
# login queue, not with the server name.
#
# Help texts follow the documentation. Where a comment there explains a value
# range or a consequence, it is kept - those are the parts that are wrong to
# paraphrase.
SECTIONS: tuple[tuple[str, tuple[CfgField, ...]], ...] = (
    (
        "Identity",
        (
            CfgField("hostname", "Server name", "text",
                     "Shown in the client server browser."),
            CfgField("description", "Description", "text",
                     "Description of the server, shown in the client server "
                     "browser. At most 255 characters.",
                     max_length=255, optional=True),
        ),
    ),
    (
        "Access",
        (
            CfgField("password", "Join password", "password",
                     "Password to connect to the server. Empty leaves it open.",
                     optional=True),
            CfgField("passwordAdmin", "Admin password", "password",
                     "Password to become a server admin. Empty means nobody can.",
                     optional=True),
            CfgField("enableWhitelist", "Enable Whitelist", "bool",
                     "Only players on the whitelist may connect."),
            CfgField("disableBanlist", "Disable banlist", "boolword",
                     "Disables the usage of ban.txt."),
            CfgField("disablePrioritylist", "Disable Priority", "boolword",
                     "Disables usage of priority.txt, which grants queue priority."),
        ),
    ),
    (
        "Gameplay",
        (
            CfgField("disable3rdPerson", "Force first person", "bool",
                     "Toggles the 3rd person view for players."),
            CfgField("disableCrosshair", "Disable crosshair", "bool",
                     "Toggles the cross-hair."),
        ),
    ),
    (
        "Players and queue",
        (
            CfgField("maxPlayers", "Player limit", "int",
                     "Maximum amount of players.", minimum=1, maximum=127),
            CfgField("loginQueueConcurrentPlayers", "Loading at once", "int",
                     "The number of players concurrently processed during the "
                     "login process. Should prevent a massive performance drop "
                     "when a lot of people connect at the same time.",
                     minimum=1, maximum=100),
            CfgField("loginQueueMaxPlayers", "Queue length", "int",
                     "The maximum number of players that can wait in the login "
                     "queue.", minimum=1, maximum=1000),
        ),
    ),
    (
        "Voice",
        (
            CfgField("disableVoN", "Disable voice chat", "bool",
                     "Enable/disable voice over network."),
            CfgField("vonCodecQuality", "Voice quality", "int",
                     "Voice over network codec quality, the higher the better "
                     "(values 0-20).", minimum=0, maximum=20),
        ),
    ),
    (
        "Security",
        (
            CfgField("verifySignatures", "Signature check", "choice",
                     "Verifies .pbos against .bisign files. Only 2 is supported "
                     "- with it off, the keys directory does nothing.",
                     choices=(("0", "Off (0)"), ("2", "Full (2)"))),
            CfgField("forceSameBuild", "Same game build only", "bool",
                     "The server will allow the connection only to clients with "
                     "the same .exe revision as the server."),
        ),
    ),
    (
        "World time",
        (
            CfgField("serverTime", "Start time", "text",
                     "Initial in-game time of the server. \"SystemTime\" means "
                     "the local time of the machine; otherwise a value in "
                     "YYYY/MM/DD/HH/MM format, e.g. 2015/4/8/17/23."),
            CfgField("serverTimeAcceleration", "Day acceleration", "float",
                     "A multiplier (0.1-64). Set to 24, time moves 24 times "
                     "faster than normal - an entire day passes in one hour.",
                     minimum=0.1, maximum=64),
            CfgField("serverNightTimeAcceleration", "Night acceleration", "float",
                     "A multiplier (0.1-64), multiplied by the day acceleration "
                     "on top. At 4 with a day acceleration of 2, night moves 8 "
                     "times faster and a whole night passes in 3 hours.",
                     minimum=0.1, maximum=64),
            CfgField("serverTimePersistent", "Keep time across restarts", "bool",
                     "The actual server time is saved to storage, so the next "
                     "start continues from the saved value."),
        ),
    ),
    (
        "Persistence",
        (
            CfgField("instanceId", "Instance ID", "int",
                     "DayZ server instance id, to identify the number of "
                     "instances per box and their storage folders with "
                     "persistence files.", minimum=1, maximum=9999),
            CfgField("storageAutoFix", "Repair broken persistence", "bool",
                     "Checks if the persistence files are corrupted and replaces "
                     "corrupted ones with empty ones."),
        ),
    ),
)

FIELDS: tuple[CfgField, ...] = tuple(f for _name, fields in SECTIONS for f in fields)
_BY_KEY = {field.key: field for field in FIELDS}

# Hidden input naming every field the form rendered - see _submitted_keys.
FORM_FIELDS_KEY = "_fields"
ALL_KEYS = ",".join(_BY_KEY)


def read_values(path: Path) -> dict[str, str]:
    """Current values, as strings for the form."""
    return read_cfg_values(path, _BY_KEY.keys())


def sections_with_values(path: Path) -> list[dict]:
    """The form model: sections, fields and what is in the file today."""
    values = read_values(path)
    return [
        {
            "name": name,
            "fields": [
                {"field": field, "value": values.get(field.key, "")}
                for field in fields
            ],
        }
        for name, fields in SECTIONS
    ]


def apply(path: Path, form: dict) -> int:
    """Validate the submitted form and write what changed.

    Returns the number of changed keys. Everything is validated before the
    first write, so a rejected value cannot leave the file half updated.

    No backup: the form only ever rewrites the values it shows, line by line,
    and everything else in the file is left untouched. There is nothing to
    restore that saving could have taken away.
    """
    if not path.is_file():
        raise CfgError(
            f"{path.name} does not exist yet - install the server files first."
        )

    current = read_values(path)
    submitted = _submitted_keys(form)
    pending: list[tuple[str, str]] = []

    for field in FIELDS:
        if field.key not in submitted:
            continue  # not on the submitted form at all

        empty = not str(form.get(field.key) or "").strip()
        if field.optional and empty and field.key not in current:
            continue  # nothing to clear, and no reason to add the key

        formatted = _validate(field, form.get(field.key))
        if formatted is None:
            continue  # optional and empty, and not in the file either
        if current.get(field.key) == formatted.strip('"'):
            continue
        pending.append((field.key, formatted))

    if not pending:
        return 0

    for key, value in pending:
        set_cfg_value(path, key, value)
    return len(pending)


def _submitted_keys(form: dict) -> set[str]:
    """Which fields the form actually carried.

    A browser leaves an unticked checkbox out of the submission entirely, so
    "absent" cannot be read as "unchanged" - it would make every switch a
    one-way trip. The form therefore names its own fields in a hidden input.
    Without it (a partial request from a script) only what was sent is touched.
    """
    declared = form.get(FORM_FIELDS_KEY) or ""
    if declared:
        return {key.strip() for key in declared.split(",") if key.strip()}
    return set(form.keys())


def _validate(field: CfgField, raw: object) -> str | None:
    """Return the value as it should appear in the file, or None to skip it."""
    text = str(raw if raw is not None else "").strip()

    if field.kind == "bool":
        return "1" if text in {"1", "true", "on", "yes"} else "0"

    if field.kind == "boolword":
        # disableBanlist and disablePrioritylist are spelled false/true in the
        # documented file. Writing 0/1 there would be a different value.
        return "true" if text in {"1", "true", "on", "yes"} else "false"

    if not text:
        if field.optional:
            # Writing an empty string is the point for a password that is being
            # cleared, so only skip keys the file does not have.
            return '""' if field.kind in {"text", "password"} else None
        raise CfgError(f"{field.label} must not be empty.")

    if field.kind in {"int", "float"}:
        try:
            number = int(text) if field.kind == "int" else float(text)
        except ValueError:
            raise CfgError(f"{field.label} must be a number.") from None
        if field.minimum is not None and number < field.minimum:
            raise CfgError(f"{field.label} must be at least {_trim(field.minimum)}.")
        if field.maximum is not None and number > field.maximum:
            raise CfgError(f"{field.label} must be at most {_trim(field.maximum)}.")
        # 12, not 12.0: the documented file writes whole numbers plainly, and a
        # value that looks rewritten invites the question what else changed.
        return _trim(number)

    if field.kind == "choice":
        allowed = {value for value, _label in field.choices}
        if text not in allowed:
            raise CfgError(f"{field.label}: {text!r} is not one of {sorted(allowed)}.")
        # timeStampFormat is a quoted string, the numeric ones are not.
        return f'"{text}"' if not text.isdigit() else text

    # text and password: quoted, and the two characters that would end the
    # statement early are not allowed through.
    if any(ch in text for ch in '";\n\r'):
        raise CfgError(f'{field.label} must not contain " or ;.')
    if field.max_length is not None and len(text) > field.max_length:
        raise CfgError(
            f"{field.label} must be at most {field.max_length} characters "
            f"({len(text)} given)."
        )
    return f'"{text}"'


def _trim(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)
