#!/usr/bin/env bash
#
# Container entrypoint.
#
# Deliberately slim: this only prepares the environment. Neither SteamCMD nor
# the DayZ server are started here -- the panel manages both itself. At the end
# gunicorn takes over the process (via exec).

set -euo pipefail

DATA_DIR="${DATA_DIR:-/data}"
STEAM_HOME="${STEAM_HOME:-${DATA_DIR}/steam}"
PANEL_PORT="${PANEL_PORT:-8080}"
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
FIX_PERMS="${FIX_PERMS:-auto}"   # auto | always | never

log() { echo "[entrypoint] $*"; }
die() { echo "[entrypoint] ERROR: $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 1. Timezone
# ---------------------------------------------------------------------------
if [[ -n "${TZ:-}" && -f "/usr/share/zoneinfo/${TZ}" ]]; then
    ln -sf "/usr/share/zoneinfo/${TZ}" /etc/localtime
    echo "${TZ}" > /etc/timezone
    log "Timezone: ${TZ}"
fi

# ---------------------------------------------------------------------------
# 2. Validate required configuration (fail fast)
#
# Deliberately BEFORE fixing permissions: a recursive chown over several GB
# should not run through only to fail on a missing variable afterwards.
# The check only applies to starting the panel -- debug commands
# (docker compose run --rm dayz bash) do not need a panel password.
# ---------------------------------------------------------------------------
if [[ $# -eq 0 ]]; then
    if [[ -z "${ADMIN_PASSWORD:-}" ]]; then
        die "ADMIN_PASSWORD is not set. The panel will not start without a password.
     Set ADMIN_PASSWORD in your .env file (see .env.example)."
    fi

    if [[ -z "${STEAM_USERNAME:-}" || -z "${STEAM_PASSWORD:-}" ]]; then
        log "WARNING: STEAM_USERNAME/STEAM_PASSWORD are not set."
        log "         The panel will start, but cannot download server files or mods."
    fi
fi

# ---------------------------------------------------------------------------
# 3. Directory structure inside the volume
#
# Four top level directories. Everything the DayZ server touches lives under
# server/ -- profiles and the @mod directories included, so the server can
# address them with paths relative to its own install directory. The @mod
# directories are created by the panel when a mod is installed.
# ---------------------------------------------------------------------------
log "Preparing ${DATA_DIR} ..."
mkdir -p \
    "${DATA_DIR}/panel" \
    "${STEAM_HOME}" \
    "${DATA_DIR}/server" \
    "${DATA_DIR}/server/profiles" \
    "${DATA_DIR}/backup"

# ---------------------------------------------------------------------------
# 4. Align UID/GID with the host (bind mounts)
# ---------------------------------------------------------------------------
current_uid="$(id -u steam)"
current_gid="$(id -g steam)"

if [[ "${PGID}" != "${current_gid}" ]]; then
    log "Changing GID of steam: ${current_gid} -> ${PGID}"
    groupmod -o -g "${PGID}" steam
fi
if [[ "${PUID}" != "${current_uid}" ]]; then
    log "Changing UID of steam: ${current_uid} -> ${PUID}"
    usermod -o -u "${PUID}" steam
fi

# ---------------------------------------------------------------------------
# 5. Permissions
#
# A recursive chown over /data costs noticeable startup time once the server
# files reach several GB. Therefore it runs only once by default (marker file),
# or when the UID changed. Force it with FIX_PERMS=always, disable with never.
# ---------------------------------------------------------------------------
perm_marker="${DATA_DIR}/.perms-${PUID}-${PGID}"

case "${FIX_PERMS}" in
    never)
        log "Skipping permission fix (FIX_PERMS=never)."
        ;;
    always)
        log "Fixing permissions recursively (FIX_PERMS=always) ..."
        chown -R steam:steam "${DATA_DIR}"
        touch "${perm_marker}"
        ;;
    auto)
        chown steam:steam "${DATA_DIR}"
        if [[ -f "${perm_marker}" ]]; then
            log "Permissions already applied - only touching new files."
            # Cheaper than chown -R: only fix the entries that differ.
            find "${DATA_DIR}" \( ! -user steam -o ! -group steam \) \
                -exec chown -h steam:steam {} + 2>/dev/null || true
        else
            log "First initialization - fixing permissions recursively ..."
            chown -R steam:steam "${DATA_DIR}"
            rm -f "${DATA_DIR}"/.perms-* 2>/dev/null || true
            touch "${perm_marker}"
            chown steam:steam "${perm_marker}"
        fi
        ;;
    *)
        die "Invalid value for FIX_PERMS: ${FIX_PERMS} (allowed: auto|always|never)"
        ;;
esac

# ---------------------------------------------------------------------------
# 6. Steam SDK symlinks (see steam_sdk_links.sh)
# ---------------------------------------------------------------------------
gosu steam:steam /opt/scripts/steam_sdk_links.sh || \
    log "WARNING: could not create SDK symlinks - the panel retries after the next SteamCMD run."

# ---------------------------------------------------------------------------
# 7. Provide a default server config (only if none exists yet)
# ---------------------------------------------------------------------------
if [[ ! -f "${DATA_DIR}/server/serverDZ.cfg" && -f /opt/defaults/serverDZ.cfg ]]; then
    log "Installing default serverDZ.cfg."
    install -o steam -g steam -m 0644 \
        /opt/defaults/serverDZ.cfg "${DATA_DIR}/server/serverDZ.cfg"
fi

# ---------------------------------------------------------------------------
# 8. Start the panel
# ---------------------------------------------------------------------------
# Special case for debugging: run an arbitrary command inside the container,
#   docker compose run --rm dayz steamcmd +quit
if [[ $# -gt 0 ]]; then
    log "Running command as steam: $*"
    exec gosu steam:steam "$@"
fi

log "Starting control panel on port ${PANEL_PORT} ..."
cd /opt/panel
exec gosu steam:steam \
    env HOME="${STEAM_HOME}" \
    gunicorn --config /opt/panel/gunicorn.conf.py "wsgi:app"
