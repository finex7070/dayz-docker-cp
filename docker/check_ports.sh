#!/usr/bin/env bash
#
# Validate SERVER_PORT before anything is started.
#
# docker-compose.yml publishes SERVER_PORT verbatim, so it has to be a shape
# Docker accepts: one port, or a range. Compose cannot add, which is why the
# operator writes the whole thing rather than a base port.
#
# DayZ uses three consecutive UDP ports - game, reserved, BattlEye - so the span
# is checked here. One port is a deliberate choice (game only, no BattlEye),
# three is what DayZ expects; wider is a typo. A server on the wrong ports fails
# in the worst way there is: it runs, the dashboard shows the port, and nobody
# can connect.
#
# Its own file rather than a block in entrypoint.sh so that a test can run every
# case in one container instead of booting one per value.
set -euo pipefail

fail() { echo "[entrypoint] ERROR: $*" >&2; exit 1; }

ports="${SERVER_PORT:-2302-2304}"

if [[ "${ports}" =~ ^([0-9]+)-([0-9]+)$ ]]; then
    first="${BASH_REMATCH[1]}"
    last="${BASH_REMATCH[2]}"
    span=$(( last - first + 1 ))
    if (( span < 1 || span > 3 )); then
        fail "SERVER_PORT is '${ports}', which spans ${span} port(s).
     DayZ uses at most three: game, reserved and BattlEye.
     Set SERVER_PORT=${first}-$((first + 2)) in .env, or a shorter range."
    fi
elif [[ ! "${ports}" =~ ^[0-9]+$ ]]; then
    fail "SERVER_PORT is '${ports}', which is neither a port nor a range.
     Write one port (2302) or a range of up to three (2302-2304).
     A comma separated list cannot be published by Docker."
fi
