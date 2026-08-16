# Project rules

## Language

Everything **in** the project is English: code, comments, docstrings, log
lines, commit messages, UI strings, README. `PLAN.md` is the one exception —
it is the German design document and stays German.

The conversation with the user is German.

## Never show a `/data` path in the panel

`/data` is where the container keeps things, not where the operator finds them
on the host. Paths in the UI and in messages are relative to the volume:
`server/mpmissions/`, `panel/backup_key`. This includes messages that came out
of another tool — see `BackupService.clean()`.

## Keep the text in the panel short

Help texts, hints and card footers are one or two lines. Say what the control
does, not why it was built that way — the reasoning belongs in a code comment,
where it is read once by whoever changes the code, not on every page load.

```
The actions run top to bottom. Delay is waited out before the action runs, so
a restart with warnings reads as one list: announce, wait, announce again,
wait, restart. A failure ends the rest unless Continue after fail is set …
```

is three lines too long for

```
Top to bottom. Delay waits before the action, Continue after fail carries on
past an error.
```

## Test after every change

Reason about it, then **check it against the running container**. A change is
not done because it looks right.

```bash
docker compose build && docker compose up -d      # source changed
docker cp <file> dayz:/opt/panel/<path>           # quick iteration, JS/CSS only
docker restart dayz                               # templates are cached: restart
docker cp test.py dayz:/tmp/ && docker exec dayz python /tmp/test.py
```

Tests are integration scripts against the live panel, kept in the scratchpad,
not pytest in the build. They log in over HTTP, drive the real endpoints and
print one `PASS`/`FAIL` line per check, so a run reads as a list of claims.
Write them that way.

Cover the failure the change was about, and re-run the existing suite for the
area you touched — several bugs in this project were caught by a test that was
written for something else.

Clean up after a run: temporary files in `/data/server`, test snapshots,
settings the test changed.

## When a task is finished

Two questions, in this order, every time:

1. **Bump the version?** Offer *Major*, *Minor*, *Patch*, *no change*.
   - Major: breaking — an existing setup needs manual work after the update
   - Minor: a new feature or page
   - Patch: bug fix, wording, documentation
   - The version lives in `panel/app/__init__.py` (`__version__`) and shows up
     in the sidebar and in `/healthz`. A bump also gets a line under *Release
     notes* in `README.md`.
2. **Commit and push?** Ask before doing either.

## Branching

Push to **`dev`**, never to `main`. The user opens the pull request to `main`
themselves on GitHub — that is what triggers the image build and publish.

## Things that bite

- **`.env` comments belong on their own line.** Docker Compose only strips a
  trailing `# ...` from a value that is not empty; on an empty value the
  comment becomes the value.
- **One gunicorn worker, by necessity.** Services hold state in memory
  (job registry, console buffers, RCON session). Scale threads, never workers.
- **One job at a time.** SteamCMD runs, mod downloads and backups share a
  single slot on purpose — two writers in the server directory is a class of
  bug worth designing out.
- **`.gitattributes` forces LF** for `*.sh`, `*.py`, `*.yml` and the
  Dockerfile. CRLF in `entrypoint.sh` makes the container fail to start with a
  misleading message.
- **Never commit `.env` or `data/`.** Both are ignored; check the staged list
  anyway before a commit.
