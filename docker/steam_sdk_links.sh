#!/usr/bin/env bash
#
# Creates the Steam SDK symlinks inside the Steam home directory.
#
# Why a separate script instead of doing this in the Dockerfile:
# The DayZ server looks for steamclient.so under ~/.steam/sdk64/. Our Steam home
# lives in the /data/steam volume -- a directory created there at build time
# would be shadowed by the mount. On top of that, the target directories
# (linux32/linux64) only exist AFTER the first SteamCMD run.
#
# The script is therefore invoked idempotently:
#   - from entrypoint.sh at container start
#   - by the panel after every completed SteamCMD job
#
# Exits 0 even when there is nothing to link yet (first start).

set -euo pipefail

STEAM_HOME="${STEAM_HOME:-/data/steam}"

log() { echo "[sdk-links] $*"; }

# Possible locations SteamCMD extracts its runtime to.
CANDIDATES=(
    "${STEAM_HOME}/.local/share/Steam/steamcmd"
    "${STEAM_HOME}/.steam/steam/steamcmd"
    "${STEAM_HOME}/Steam/steamcmd"
    "${STEAM_HOME}/.local/share/Steam"
)

steamcmd_root=""
for candidate in "${CANDIDATES[@]}"; do
    if [[ -d "${candidate}/linux32" || -d "${candidate}/linux64" ]]; then
        steamcmd_root="${candidate}"
        break
    fi
done

if [[ -z "${steamcmd_root}" ]]; then
    log "No SteamCMD runtime found yet - skipping (expected before the first SteamCMD run)."
    exit 0
fi

log "SteamCMD runtime: ${steamcmd_root}"
mkdir -p "${STEAM_HOME}/.steam"

for arch in 32 64; do
    src="${steamcmd_root}/linux${arch}"
    dst="${STEAM_HOME}/.steam/sdk${arch}"

    if [[ ! -d "${src}" ]]; then
        log "linux${arch} missing - skipped."
        continue
    fi

    # -f -n: replace an existing symlink instead of linking into its target
    # (sdk64/linux64 rather than sdk64 -> linux64).
    ln -sfn "${src}" "${dst}"
    log "${dst} -> ${src}"

    # DayZ/Steam additionally expect steamservice.so; it is the same library.
    if [[ -f "${src}/steamclient.so" && ! -e "${src}/steamservice.so" ]]; then
        ln -sf "steamclient.so" "${src}/steamservice.so"
        log "${src}/steamservice.so -> steamclient.so"
    fi
done

log "Done."
