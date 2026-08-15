# syntax=docker/dockerfile:1
#
# DayZ Server + Control Panel
# ---------------------------
# The control panel is the primary component: it is the only process started by
# the container and manages both SteamCMD (server files, mods) and the DayZ
# server process from there.
#
# The server files themselves do NOT live in the image, but in the /data volume.

FROM python:3.13-slim-trixie

ARG STEAM_UID=1000
ARG STEAM_GID=1000

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=en_US.UTF-8 \
    LANGUAGE=en_US:en \
    LC_ALL=en_US.UTF-8 \
    STEAM_HOME=/data/steam \
    DATA_DIR=/data \
    PANEL_PORT=8080 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# ---------------------------------------------------------------------------
# SteamCMD + system dependencies
#
# The steamcmd package lives in Debian's non-free component and is 32-bit, so:
# extend the sources, enable i386, and pre-accept the Steam license (otherwise
# the install blocks on a debconf dialog).
# lib32gcc-s1 is required by the DayZ server.
# restic is the backup engine behind the Backups page (services/backup.py):
# block level deduplication, so the second snapshot of a 4 GB server directory
# costs what actually changed instead of another 4 GB.
# ---------------------------------------------------------------------------
RUN set -eux; \
    sed -i 's/^Components: .*/Components: main contrib non-free non-free-firmware/' \
        /etc/apt/sources.list.d/debian.sources; \
    dpkg --add-architecture i386; \
    echo steam steam/question select "I AGREE" | debconf-set-selections; \
    echo steam steam/license note '' | debconf-set-selections; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        steamcmd \
        lib32gcc-s1 \
        ca-certificates \
        locales \
        tini \
        gosu \
        procps \
        curl \
        rsync \
        restic \
        tzdata; \
    rm -rf /var/lib/apt/lists/*; \
    ln -sf /usr/games/steamcmd /usr/bin/steamcmd

# Generate a UTF-8 locale (SteamCMD produces mojibake otherwise)
RUN set -eux; \
    sed -i 's/^# *\(en_US.UTF-8\)/\1/' /etc/locale.gen; \
    locale-gen

# ---------------------------------------------------------------------------
# Unprivileged user. Its home lives inside the volume (/data/steam) and is
# created by the entrypoint, hence deliberately no -m here.
# ---------------------------------------------------------------------------
RUN set -eux; \
    groupadd -g "${STEAM_GID}" steam; \
    useradd -u "${STEAM_UID}" -g steam -d "${STEAM_HOME}" -s /bin/bash -M steam

# ---------------------------------------------------------------------------
# Python dependencies in their own layer (before the app code) for build caching
# ---------------------------------------------------------------------------
COPY panel/requirements.txt /opt/panel/requirements.txt
RUN pip install --no-cache-dir -r /opt/panel/requirements.txt

# ---------------------------------------------------------------------------
# Vendor Bootstrap into the image.
#
# Fetched at build time rather than loaded from a CDN at runtime: the panel has
# to work on a host without outbound internet access, and an admin interface
# should not depend on a third party being reachable. Kept out of the
# repository so no minified bundles end up in version control.
# ---------------------------------------------------------------------------
ARG BOOTSTRAP_VERSION=5.3.7
RUN set -eux; \
    base="https://cdn.jsdelivr.net/npm/bootstrap@${BOOTSTRAP_VERSION}/dist"; \
    mkdir -p /opt/panel/app/static/vendor; \
    curl -fsSL "${base}/css/bootstrap.min.css" \
        -o /opt/panel/app/static/vendor/bootstrap.min.css; \
    curl -fsSL "${base}/js/bootstrap.bundle.min.js" \
        -o /opt/panel/app/static/vendor/bootstrap.bundle.min.js; \
    test -s /opt/panel/app/static/vendor/bootstrap.min.css; \
    test -s /opt/panel/app/static/vendor/bootstrap.bundle.min.js

# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------
COPY panel/    /opt/panel/
COPY docker/   /opt/scripts/
COPY defaults/ /opt/defaults/

RUN chmod +x /opt/scripts/*.sh

WORKDIR /opt/panel

VOLUME ["/data"]

EXPOSE 8080/tcp
EXPOSE 2302-2306/udp
EXPOSE 27016/udp
EXPOSE 8766/udp

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PANEL_PORT}/healthz" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/opt/scripts/entrypoint.sh"]
