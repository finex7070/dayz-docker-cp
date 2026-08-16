"""The DayZ Launcher's `modlist.html` - reading one, writing one.

The launcher exports a preset as an HTML page: a table of rows, each carrying a
display name and a link to the workshop item. Players drag that file back into
their own launcher, which is what makes it the format worth speaking. The panel
reads one to take a preset over, and writes one so the operator can hand out
the set the server actually runs.

The file, not the mods: nothing here touches Steam or the registry. The name in
the file is only a label - what a mod is really called comes out of its own
`meta.cpp` after the download.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from html.parser import HTMLParser

# The launcher writes them as `.../filedetails/?id=1559212036`.
_ID_IN_LINK = re.compile(r"[?&]id=(\d{6,12})\b")

# What the launcher's own export starts with, kept byte for byte: the BOM and
# the XML declaration are what a strict reader looks at first.
_PROLOGUE = '﻿<?xml version="1.0" encoding="utf-8"?>'

_STYLE = """body {
\tmargin: 0;
\tpadding: 0;
\tcolor: #fff;
\tbackground: #000;\t
}

body, th, td {
\tfont: 95%/1.3 Roboto, Segoe UI, Tahoma, Arial, Helvetica, sans-serif;
}

td {
    padding: 3px 30px 3px 0;
}

h1 {
    padding: 20px 20px 0 20px;
    color: white;
    font-weight: 200;
    font-family: segoe ui;
    font-size: 3em;
    margin: 0;
}

em {
    font-variant: italic;
    color:silver;
}

.before-list {
    padding: 5px 20px 10px 20px;
}

.mod-list {
    background: #222222;
    padding: 20px;
}

.footer {
    padding: 20px;
    color:gray;
}

.whups {
    color:gray;
}

a {
    color: #C80004;
    text-decoration: underline;
}

a:hover {
    color:#F1AF41;
    text-decoration: none;
}

.from-steam {
    color: #449EBD;
}
.from-local {
    color: gray;
}
"""

WORKSHOP_LINK = "http://steamcommunity.com/sharedfiles/filedetails/?id="


class ModlistError(ValueError):
    """A file that is not a mod list, phrased for whoever picked it."""


@dataclass(frozen=True)
class ModlistEntry:
    workshop_id: int
    name: str


class _ListParser(HTMLParser):
    """Rows first, links as a fallback.

    A `<tr data-type="ModContainer">` is the launcher's own marking and gives a
    name along with the ID. Files that came out of somewhere else keep the links
    but not always the markings, so every workshop link is collected as well -
    used only when no proper row was found.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[ModlistEntry] = []
        self.links: list[ModlistEntry] = []
        self._name_parts: list[str] = []
        self._in_name = False
        self._row_link = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.lower(): (value or "") for key, value in attrs}
        marker = attributes.get("data-type", "").lower()

        if tag == "tr":
            self._name_parts, self._row_link, self._in_name = [], "", False
        elif tag == "td" and marker == "displayname":
            self._in_name = True
        elif tag == "a" and attributes.get("href"):
            href = attributes["href"]
            if not self._row_link:
                self._row_link = href
            found = _ID_IN_LINK.search(href)
            if found:
                self.links.append(ModlistEntry(int(found.group(1)), ""))

    def handle_endtag(self, tag: str) -> None:
        if tag == "td":
            self._in_name = False
        elif tag == "tr":
            found = _ID_IN_LINK.search(self._row_link)
            if found:
                name = " ".join("".join(self._name_parts).split())
                self.rows.append(ModlistEntry(int(found.group(1)), name))
            self._name_parts, self._row_link = [], ""

    def handle_data(self, data: str) -> None:
        if self._in_name:
            self._name_parts.append(data)


def parse_modlist(text: str) -> list[ModlistEntry]:
    """Every mod in a launcher preset, in the order the file lists them.

    Duplicates are dropped rather than refused - a list assembled by hand is
    allowed to name the same mod twice, and installing it twice is not a thing
    one can do anyway.
    """
    parser = _ListParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception as exc:  # noqa: BLE001 - a parser error is a bad file
        raise ModlistError(f"That file could not be read as HTML: {exc}") from exc

    found = parser.rows or parser.links
    seen: set[int] = set()
    entries: list[ModlistEntry] = []
    for entry in found:
        if entry.workshop_id in seen:
            continue
        seen.add(entry.workshop_id)
        entries.append(entry)

    if not entries:
        raise ModlistError(
            "No workshop links in that file. A mod list is the HTML the DayZ "
            "Launcher exports under Mods / Preset."
        )
    return entries


def render_modlist(entries: list[ModlistEntry]) -> str:
    """A file the DayZ Launcher will take back in."""
    rows = "\n".join(
        f"""        <tr data-type="ModContainer">
          <td data-type="DisplayName">{html.escape(entry.name)}</td>
          <td>
            <span class="from-steam">Steam</span>
          </td>
          <td>
            <a href="{WORKSHOP_LINK}{entry.workshop_id}" data-type="Link">"""
        f"""{WORKSHOP_LINK}{entry.workshop_id}</a>
          </td>
        </tr>"""
        for entry in entries
    )

    return f"""{_PROLOGUE}
<html>
  <!--Created by DayZ Launcher: https://dayz.com-->
  <head>
    <meta name="dayz:Type" content="list" />
    <meta name="generator" content="DayZ Launcher - https://dayz.com" />
    <title>DayZ Mods</title>
    <link href="https://fonts.googleapis.com/css?family=Roboto" rel="stylesheet" type="text/css" />
    <style>
{_STYLE}
</style>
  </head>
  <body>
    <h1>DayZ Mods</h1>
    <p class="before-list">
      <em>Drag this file or link to it to DayZ Launcher or open it Mods / Preset / Import.</em>
    </p>
    <div class="mod-list">
      <table>
{rows}
      </table>
    </div>
    <div class="footer">
      <span>Created by DayZ Launcher by Bohemia Interactive.</span>
    </div>
  </body>
</html>"""
