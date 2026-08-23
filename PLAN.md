# Plan: DayZ Server + Control Panel im Docker-Image

**Ziel:** Ein Docker-Image auf Basis von `python:slim-trixie`, in dem das **Control Panel die Hauptkomponente** ist. Das Panel startet als erstes und verwaltet von dort aus sowohl SteamCMD (Installation/Update der Serverdateien, Mod-Downloads) als auch den DayZ-Serverprozess selbst — inklusive Start/Neustart/Stopp, Mod-Verwaltung, Config-Bearbeitung und Live-Log.

**Stack:** Python 3.13 · Flask · Bootstrap 5 · SQLite · SteamCMD (Debian-Paket) · DayZ Linux Dedicated Server

---

## 1. Leitprinzip: Panel first

Das Panel ist **nicht** ein Beiwerk zum Server, sondern der Einstiegspunkt des Containers. Daraus folgt konkret:

- Der Container startet **ohne** vorhandene Serverdateien erfolgreich. Das Panel ist sofort erreichbar.
- SteamCMD wird **nicht** im Entrypoint ausgeführt, sondern vom Panel als Hintergrundjob gestartet — mit Live-Ausgabe im UI.
- Fehlt die Installation, zeigt das Dashboard eine Setup-Karte: „Serverdateien nicht installiert → Jetzt installieren".
- Steam-Guard-Abfragen landen nicht in einem toten interaktiven Prompt, sondern werden **im Panel abgefragt** (siehe §5.2).
- Der DayZ-Serverprozess ist ein Kindprozess des Panels. Es gibt keine zweite Kontrollinstanz (kein supervisord/s6) — Start/Stop/Restart sind die Kernfunktion des Panels, ein paralleler Supervisor würde nur Race Conditions und doppelte Log-Erfassung erzeugen.

---

## 2. Rahmenbedingungen

| Thema | Fakt | Konsequenz |
|---|---|---|
| Steam App-IDs | Server **223350**, Client/Workshop **221100** | Serverdateien über 223350, Mods über 221100 |
| Steam-Login | DayZ Server ist **nicht** anonym ladbar | Credentials via Env; Steam Guard über Panel-Dialog lösbar |
| Plattform | Natives **Linux**-Binary `DayZServer` | Kein Wine/Proton, läuft direkt auf Debian 13 |
| 32-Bit-Abhängigkeit | SteamCMD ist 32-Bit | `dpkg --add-architecture i386` + `lib32gcc-s1` |
| SteamCMD-Paket | Liegt in Debian in **`non-free`** | Sources um `contrib non-free` erweitern, Lizenz via debconf akzeptieren |
| `steamclient.so` | DayZServer sucht sie in `~/.steam/sdk64/` | sdk32/sdk64-Symlinks — **zur Laufzeit** anlegen (siehe §7.2) |
| Dateisystem | Linux ist case-sensitive, Workshop-Mods sind gemischt geschrieben | Mods nach Download rekursiv kleinschreiben |
| Prozess-State | Panel hält PID + Logpuffer im Speicher | Gunicorn mit **genau 1 Worker** |

---

## 3. Zielarchitektur

```
┌───────────────────── Container (python:slim-trixie) ─────────────────────┐
│                                                                          │
│  tini (PID 1)                                                            │
│    └─ entrypoint.sh                                                      │
│         ├─ /data-Struktur + Rechte (PUID/PGID)                           │
│         ├─ Steam-SDK-Symlinks in $STEAM_HOME anlegen                     │
│         └─ exec gunicorn -w 1 --threads 8 → FLASK-PANEL :8080  ◄── Start │
│                                                                          │
│  ┌─ Flask-Panel ────────────────────────────────────────────────────┐    │
│  │  SteamCmdService ── subprocess ──► steamcmd                      │    │
│  │     • App-Update 223350 (Install / Update / Validate)            │    │
│  │     • workshop_download_item 221100 <modid>                      │    │
│  │     • Steam-Guard-Erkennung → Code-Abfrage im UI                 │    │
│  │  ServerManager   ── subprocess ──► DayZServer                    │    │
│  │     • start / stop (SIGTERM→SIGKILL) / restart / Watchdog        │    │
│  │  LogStreamer     ── Ringpuffer + Datei-Tail ──► SSE              │    │
│  │  ModManager · ServerSettings · Auth · AuditLog                   │    │
│  └──────────────────────────────────────────────────────────────────┘    │
│                                                                          │
│  Volume /data: panel · steam · server (mit profiles + mods) · backup     │
└──────────────────────────────────────────────────────────────────────────┘
   8080/tcp (Panel) · 2302-2304/udp (Game) · 2305/udp (RCON)
   27016/udp (Query) · 8766/udp (Master)
```

**Job-Modell:** SteamCMD-Läufe (Serverinstallation, Mod-Download) sind langlaufend. Sie laufen als benannte Hintergrund-Jobs mit Status (`queued/running/needs_guard/success/failed`), eigenem Log-Kanal und Fortschrittsanzeige. Es läuft immer nur **ein** SteamCMD-Job gleichzeitig (Lock) — parallele Läufe würden sich über dieselbe Steam-Session in die Quere kommen.

---

## 4. Repository-Struktur

```
dayz-docker-cp/
├── PLAN.md
├── README.md
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── .dockerignore
├── docker/
│   └── entrypoint.sh
├── panel/
│   ├── requirements.txt
│   ├── wsgi.py
│   ├── gunicorn.conf.py
│   └── app/
│       ├── __init__.py            # create_app(), Blueprints, Login-Manager
│       ├── config.py              # Env → Settings
│       ├── models.py              # SQLite: Settings, Mods, Jobs, AuditLog
│       ├── auth.py                # Login/Logout, ADMIN_USERNAME/PASSWORD
│       ├── services/
│       │   ├── steamcmd.py        # SteamCMD-Wrapper + Guard-Erkennung
│       │   ├── jobs.py            # Hintergrundjobs, Status, Lock
│       │   ├── server.py          # Prozesslebenszyklus DayZServer, Watchdog
│       │   ├── server_config.py   # serverDZ.cfg/beserver_x64.cfg/dayzsetting.xml gezielt schreiben
│       │   ├── logs.py            # Logdateien auflisten und lesen
│       │   ├── query.py           # Steam-A2S-Abfrage (Spielerzahl)
│       │   ├── mod_manager.py     # Workshop-Download, Keys, Reihenfolge
│       │   ├── server_settings.py # Startparameter & serverDZ.cfg-Werte (JSON-Store)
│       │   ├── schedules.py       # Cron-Einträge, APScheduler, Aktionsketten
│       │   ├── files.py           # Dateibrowser, an /data/server verankert
│       │   ├── audit.py           # Wer hat was getan (JSON Lines)
│       │   ├── backup.py          # restic-Repository: Snapshot, Restore, Aufbewahrung
│       │   └── rcon.py            # BattlEye-RCON über UDP, eine Sitzung je Kommando
│       ├── routes/
│       │   ├── dashboard.py · server.py · console.py · jobs.py
│       │   ├── logs.py · settings.py · mods.py · schedules.py · files.py · backups.py
│       ├── templates/             # Jinja2 + Bootstrap 5
│       │   ├── base.html · login.html · dashboard.html
│       │   ├── logs.html · settings.html · placeholder.html
│       └── static/
│           ├── css/  (app.css)
│           ├── js/   (app.js)
│           └── vendor/ (bootstrap, im Build geladen — kein CDN)
└── defaults/
    └── serverDZ.cfg               # Fallback beim ersten Start
```

---

## 5. Docker-Ebene

### 5.1 Dockerfile

```dockerfile
FROM python:3.13-slim-trixie        # gepinnte Form von python:slim-trixie
```

Aufbaureihenfolge (Layer-Caching beachten):

1. **Steam-Repo vorbereiten** — Debian 13 nutzt das deb822-Format:
   `/etc/apt/sources.list.d/debian.sources` um `contrib non-free` in `Components:` erweitern.
2. **i386 aktivieren:** `dpkg --add-architecture i386`
3. **Lizenz vorab akzeptieren** (sonst blockiert die Installation auf einem Dialog):
   ```
   echo steam steam/question select "I AGREE"        | debconf-set-selections
   echo steam steam/license note ''                  | debconf-set-selections
   ```
4. **Pakete installieren** (`DEBIAN_FRONTEND=noninteractive`):
   `steamcmd`, `lib32gcc-s1`, `ca-certificates`, `locales`, `tini`, `procps`, `gosu`, `curl`, `rsync`, `restic` (§6.7c)
   → danach `rm -rf /var/lib/apt/lists/*`
5. **Locale:** `en_US.UTF-8` generieren, `LANG`/`LANGUAGE` setzen
6. **Symlink:** `ln -s /usr/games/steamcmd /usr/bin/steamcmd`
7. **Benutzer `steam`** (UID/GID via Build-Arg, zur Laufzeit über PUID/PGID anpassbar)
8. **Python-Deps:** `COPY panel/requirements.txt` → `pip install --no-cache-dir -r`
   (eigener Layer **vor** dem App-Code)
9. **App kopieren:** `panel/` → `/opt/panel`, `docker/` → `/opt/scripts`, `defaults/` → `/opt/defaults`
10. `VOLUME /data` · `EXPOSE 8080/tcp 2302-2306/udp 27016/udp 8766/udp`
11. `HEALTHCHECK` → `curl -f http://localhost:8080/healthz`
12. `ENTRYPOINT ["/usr/bin/tini","--","/opt/scripts/entrypoint.sh"]`

> **Bewusst nicht im Image:** `steamcmd +quit` als Warmup-Lauf und die sdk32/sdk64-Symlinks. Beides schreibt ins Steam-Home, das zur Laufzeit im Volume liegt und ein Image-Verzeichnis überdecken würde. Siehe §7.2.

> **Alternative,** falls die non-free-Komponente Probleme macht: SteamCMD direkt als Tarball von `https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz` nach `/opt/steamcmd` entpacken. Funktional gleichwertig, umgeht die Repo-Anpassung, erfordert aber weiterhin `lib32gcc-s1` und i386.

### 5.2 entrypoint.sh (bewusst schlank)

```
1. /data/{panel,steam,server,server/profiles,backup} anlegen
2. UID/GID von `steam` an PUID/PGID angleichen, chown -R /data
3. Steam-SDK-Symlinks in $STEAM_HOME anlegen (idempotent, siehe §7.2)
4. Fehlt /data/server/serverDZ.cfg → aus /opt/defaults kopieren
5. exec gosu steam gunicorn -c /opt/panel/gunicorn.conf.py wsgi:app
```

Kein SteamCMD-Aufruf, kein Serverstart — das übernimmt ausschließlich das Panel.

### 5.3 Volume-Layout `/data`

Vier Ordner direkt unter `/data`; alles Serverbezogene liegt gebündelt unter `server/`.

| Pfad | Inhalt |
|---|---|
| `/data/panel` | `server_settings.json` (Startparameter, RCON-Passwort), SQLite-DB für die Modliste, Session-Key |
| `/data/steam` | **Steam-Home**: Sentry-Datei, `config.vdf`, SteamCMD-Cache, sdk32/sdk64 |
| `/data/steam/steamapps/workshop/content/221100/<id>` | Workshop-Downloads (Rohzustand, von SteamCMD verwaltet) |
| `/data/server` | Serverdateien App 223350: `DayZServer`, `serverDZ.cfg`, `mpmissions/`, `keys/` |
| `/data/server/steamapps` | SteamCMDs eigene Buchführung zum Installationsverzeichnis: `appmanifest_223350.acf` (Build-ID) plus `downloading/` und `temp/`. Von SteamCMD angelegt, nicht vom Panel — `+force_install_dir` bringt es immer im Ziel mit, ein Schalter dafür existiert nicht. Ohne das Manifest ist der nächste `app_update` ein 4-GB-Neudownload statt eines inkrementellen Laufs |
| `/data/server/@<id>_<modname>` | Installierte Mods, aufbereitet (siehe §6.4) |
| `/data/server/profiles` | Profildaten & Serverlogs (`*.RPT`, `*.ADM`, `script*.log`) |
| `/data/backup` | Automatische Config-Backups (`files/`) und das restic-Repository (`repo/`, §6.7c) |

**Konsequenz für SteamCMD:** `profiles/` und die `@mod`-Ordner liegen innerhalb des `+force_install_dir`-Ziels. `app_update ... validate` prüft nur die Dateien der App und lässt fremde Unterverzeichnisse in Ruhe — sie überstehen ein Update also. Der Vorteil überwiegt: `-mod=` und `-profiles=` lassen sich relativ zum Serververzeichnis angeben, und die Mods liegen genau dort, wo der Server sie erwartet.

### 5.4 Environment-Variablen

**Steam**

| Variable | Default | Zweck |
|---|---|---|
| `STEAM_USERNAME` | – | Steam-Konto, das DayZ besitzt |
| `STEAM_PASSWORD` | – | Passwort |
| `STEAM_GUARD_CODE` | *(leer)* | **Optional.** Einmal-Code für den Erstlogin |

**Panel-Login**

| Variable | Default | Zweck |
|---|---|---|
| `ADMIN_USERNAME` | `admin` | Benutzername für das Panel |
| `ADMIN_PASSWORD` | – | Passwort im Klartext; wird beim Start zu einem Werkzeug-Hash verarbeitet und **nur gehasht** gehalten |
| `PANEL_SECRET_KEY` | autogeneriert, persistiert | Flask-Session |
| `PANEL_PORT` | `8080` | Panel-Port |

**Server & Betrieb**

| Variable | Default | Zweck |
|---|---|---|
| `SERVER_PORT` | `2302` | DayZ Game-Port |
| `STEAM_QUERY_PORT` | `27016` | Steam Query; wird vor jedem Start in `serverDZ.cfg` geschrieben |
| `RCON_PORT` | `2305` | BattlEye-RCON; wird vor jedem Start in `beserver_x64.cfg` geschrieben |

Zusätzlich wird **8766/udp** fest veröffentlicht (Steam-Master-Server). Der Port gehört dem Steam-Client, DayZ bietet keinen Schalter dafür — er ist deshalb eine Konstante im Code, keine Env-Variable. Fehlt er, läuft der Server, erscheint aber nicht in der Serverliste.
| `AUTO_INSTALL` | `true` | Serverdateien beim Containerstart installieren, falls nicht vorhanden |
| `AUTO_START` | `false` | DayZ-Server nach erfolgreicher Installation automatisch starten |

Nur diese beiden beschreiben, wie *dieser Container* hochkommt. Ob vor dem Serverstart aktualisiert wird, gehört zum Serverstart und steht deshalb auf der Settings-Seite — siehe §6.5a.
| `PUID` / `PGID` | `1000` | Dateirechte auf Bind-Mounts |
| `TZ` | `Europe/Berlin` | Zeitzone für Logs und Restart-Zeitplan |

`ADMIN_PASSWORD` und `STEAM_PASSWORD` sind Pflicht — fehlen sie, startet das Panel trotzdem, zeigt aber eine deutliche Warnung im Dashboard (Steam) bzw. verweigert den Start mit klarer Fehlermeldung (Admin).

### 5.5 docker-compose.yml

Ein Service `dayz`, `env_file: .env`, Bind-Mount oder Named Volume auf `/data`, `restart: unless-stopped`, UDP-Port-Mappings und `stop_grace_period: 60s`, damit der DayZ-Server beim `docker stop` sauber persistieren kann.

---

## 6. Panel — Funktionale Spezifikation

### 6.0 Seitenstruktur

Sieben Seiten, jede mit einer klaren Zuständigkeit. Es gibt bewusst **keine**
eigene Serverseite: Status und Steuerung gehören zusammen und stehen auf dem
Dashboard.

| Seite | Inhalt | Stand |
|---|---|---|
| **Dashboard** | Serverdateien, Statuskacheln, Steuerknöpfe, Live-Konsole mit RCON-Eingabe | ✅ |
| **Logs** | Logdateien nach Typ und Datei auswählen, Filter, Auto-Reload, Download | ✅ |
| **Settings** | Tabs: General (Startparameter), serverDZ | ✅ |
| **Mods** | Installation per ID oder Suche, Client/Server-Typ, Update/Reinstall/Delete | ✅ |
| **Schedules** | Aufgaben im Crontab-Format mit einer oder mehreren Aktionen | ✅ |
| **Files** | Dateibrowser über `/data/server`, Textdateien bearbeiten, Up-/Download | ✅ |
| **Backups** | Snapshots von `/data/server` verwalten: wiederherstellen, herunterladen, löschen, Aufbewahrung | ✅ |

### 6.1 Dashboard

Von oben nach unten in der Reihenfolge, in der man sie braucht:

1. **Serverdateien** — Installation/Update per SteamCMD, mit Live-Ausgabe und Steam-Guard-Dialog (§6.2). Fehlt die Installation, ist das die einzige sinnvolle Aktion und steht deshalb ganz oben.
2. **Statuskacheln** — Server State, Server Uptime, Players, CPU, Memory.
   - *Players* kommt aus einer **Steam-A2S-Abfrage** an `127.0.0.1:<STEAM_QUERY_PORT>` — dieselbe Anfrage, die auch der Serverbrowser stellt. Kein RCON und kein Passwort nötig, deshalb funktioniert die Zahl unabhängig davon, ob RCON eingerichtet ist. Antwort wird 5 s zwischengespeichert, damit mehrere offene Tabs den Server nicht mit Anfragen überziehen.
   - *CPU* und *Memory* liest der `ServerManager` aus `/proc/<pid>` — keine zusätzliche Abhängigkeit für zwei Zahlen.
3. **Steuerung** — Start · Restart · Stop · Lock · Unlock. Lock/Unlock sind RCON-Kommandos (`#lock`/`#unlock`); das Panel verbindet sich für den Klick selbst, sie brauchen also nur einen laufenden Server und ein gesetztes Passwort. Scheitert die Verbindung, steht der Grund im Fehlerband über den Knöpfen. Der Auto-Restart-Schalter steht **nicht** hier, sondern nur unter *Settings → General → Behaviour* — zwei Bedienelemente für denselben Wert lassen einen davon immer falsch aussehen.
4. **Live-Konsole** — stdout des Serverprozesses per SSE, darunter ein Eingabefeld mit *Send* für RCON-Kommandos. Eingegebene Kommandos **und** ihre Antworten landen im gemeinsamen Puffer, nicht nur in der Antwort des eigenen Requests: wer in einem zweiten Tab zusieht, muss sehen, dass gerade jemand einen Spieler gekickt hat. `↑`/`↓` blättern durch die zuletzt gesendeten Kommandos.

Aktualisierung: Statuskacheln über den gemeinsamen `/status.json`-Poll (10 s, Dauern ticken lokal weiter), Knopfzustände über `/server/status.json` (5 s), Konsole über SSE.

### 6.2 SteamCMD-Verwaltung (`SteamCmdService`)
- Aufruf immer mit Argumentliste, nie `shell=True`:
  ```
  steamcmd +force_install_dir /data/server \
           +login <user> <pass> [<guard>] \
           +app_update 223350 validate +quit
  ```
- `HOME`/`STEAM_HOME` zeigt auf `/data/steam` → Sentry-Datei überlebt Container-Neustarts, alle Folgelogins sind nicht-interaktiv
- **Steam-Guard-Handling:** Die Ausgabe wird zeilenweise auf Marker wie `Two-factor code`, `Steam Guard code` bzw. `FAILED (Two-factor code mismatch)` geprüft. Trifft einer zu, geht der Job in Status `needs_guard`, das Panel zeigt ein Eingabefeld, und der Lauf wird mit dem eingegebenen Code wiederholt. `STEAM_GUARD_CODE` aus der Env wird beim ersten Versuch automatisch verwendet.
- Passwörter und Codes werden vor dem Schreiben ins Log **maskiert**
- Jeder Lauf ist ein Job mit eigenem Log-Kanal (`steamcmd`), sichtbar auf der Log-Seite
- Warnung + Bestätigung, wenn ein Update angestoßen wird, während der Server läuft

### 6.3 Serversteuerung (`ServerManager`)
- **Vor dem Start:** `steamQueryPort` und die Mission in `class Missions` in `serverDZ.cfg`, `RConPort`/`RConPassword` in `beserver_x64.cfg` und `maxcores` in `dayzsetting.xml` schreiben, alte `beserver_x64_active_*.cfg` löschen (§6.5)
- **Start** mit generierten Parametern:
  ```
  ./DayZServer -config=serverDZ.cfg -port=$SERVER_PORT \
    -BEpath=/data/server/battleye -profiles=/data/server/profiles \
    -mod="@<id>_<clientmod1>;@<id>_<clientmod2>" \
    -serverMod="@<id>_<servermod1>" \
    -mission=dayzOffline.chernarusplus -cpuCount=2 \
    -doLogs -adminLog -freezeCheck
  ```
  Die letzte Zeile stammt aus den Einstellungen (§6.5); abgeschaltete Schalter und leere Werte werden weggelassen, nicht mit Leerwert gesetzt.
  Arbeitsverzeichnis `/data/server`, stdout/stderr in Pipe → `LogStreamer`
- **Stop:** `SIGTERM` → bis zur eingestellten Frist warten → `SIGKILL`; Statusmaschine mit Lock verhindert parallele Aktionen. Die Frist gehört dem Betreiber (`stop_timeout_seconds`, Standard 30 s, 5–600 s): ein voller Server schreibt seine Persistenz länger als ein leerer. Derselbe Knopf bietet während des Wartens **Kill** an, das sofort `SIGKILL` schickt — der Ausweg aus einem Shutdown, der nicht vorankommt, bezahlt mit dem, was noch nicht geschrieben war. Beim Herunterfahren des Containers gilt stattdessen `CONTAINER_STOP_TIMEOUT` (30 s), siehe §7.5.
- **Statuserkennung:** `running` heißt *Mission geladen*, nicht *Prozess existiert*. Bis die Engine `Player connect enabled` schreibt, steht der Status auf `starting`; der Watcher liest die Zeile aus dem stdout, das er ohnehin zeilenweise puffert — kein zweiter Leser auf der RPT. Die FPS-Zeile (`Average server FPS:`) dient als Ersatzmarker, falls ein Build die erste umbenennt, und `READY_TIMEOUT_SECONDS` (900 s) als Notausgang: ein dauerhaft in `starting` hängendes Panel hätte RCON und die Kommandozeile permanent gesperrt.
- **Restart:** Stop + Wartezeit + Start als Hintergrundjob mit Statusanzeige
- **Watchdog:** prüft `Popen.poll()`; unerwarteter Exit → Status `crashed`, optionaler Auto-Restart mit Backoff gegen Crash-Loops
- Start wird blockiert, solange ein SteamCMD-Job läuft, und umgekehrt

### 6.4 Mod-Verwaltung (`ModManager`)

**Zweistufiger Ablauf: herunterladen ins Steam-Home, aufbereitet ins Serververzeichnis kopieren.**

```
1. Download   steamcmd +force_install_dir /data/steam \
                       +workshop_download_item 221100 <id>
              → /data/steam/steamapps/workshop/content/221100/<id>   (Rohzustand)

2. Aufbereiten und kopieren
              → /data/server/@<id>_<modname>
```

Warum kopieren statt verschieben oder verlinken: der Download bleibt im von SteamCMD verwalteten Zustand, sodass ein späteres `workshop_download_item` **inkrementell** aktualisieren kann statt jedes Mal alles neu zu laden. Verschieben würde diesen Cache zerstören, ein Symlink würde die Kleinschreibung (siehe unten) im Cache erzwingen und ihn damit ebenfalls entwerten.

**Ordnername:** `@<workshop-id>_<modname>` — der Name aus der `meta.cpp` des Mods, **kleingeschrieben**, Leerzeichen durch `_` ersetzt (ebenso alle weiteren pfadproblematischen Zeichen). Beispiel: Workshop-Item `1559212036`, dessen `meta.cpp` sich „CF" nennt → `@1559212036_cf`. Die ID im Namen macht den Ordner eindeutig, auch wenn zwei Mods gleich heißen, und erlaubt die Zuordnung ohne Datenbankabfrage.

Der Name kommt aus der `meta.cpp` und nicht aus den Workshop-Metadaten, weil er dann ohne Netzwerkabfrage feststeht — und weil es der Name ist, unter dem sich der Mod selbst kennt. Der Ordnername wird bei der **ersten** Installation festgelegt und danach nicht mehr geändert: benennt sich ein Mod im Workshop um, entstünde sonst ein zweiter Ordner, während die Startparameter noch auf den alten zeigen.

**Nachbearbeitung nach jedem Download (auf Linux Pflicht):**
1. Nach `/data/server/@<id>_<modname>` kopieren
2. Dateien und Ordner darin rekursiv **kleinschreiben**
3. Signaturschlüssel setzen — **abhängig vom Modtyp**, siehe unten

Zum Kleinschreiben eine Fußangel, die beim Test aufgefallen ist: Prüft man vor dem Umbenennen nur, ob der kleingeschriebene Name schon existiert, passiert auf einem **case-insensitiven** Dateisystem gar nichts mehr — und genau das ist ein Bind-Mount von einem Windows-Host. Dort meldet `Keys/Mod.bikey` und `keys/mod.bikey` dieselbe Datei. Entscheidend ist deshalb `samefile()`: nur wenn es wirklich zwei verschiedene Einträge sind, ist es eine Kollision. Auf dem Windows-Mount fällt der Fehler nicht weiter auf — auf einem Linux-Host lädt der Mod dann aber nicht.

Da die Mods direkt im Serververzeichnis liegen, genügt für den Start der relative Pfad: `-mod="@1559212036_community_framework;@..."`.

#### Modtyp: Client-Mod oder reiner Server-Mod

Beim Hinzufügen (und jederzeit änderbar) wählt der Benutzer, in welche Startparameter-Kette der Mod gehört:

| Typ | Parameter | Bedeutung | Signaturschlüssel |
|---|---|---|---|
| **Client-Mod** | `-mod=` | Spieler müssen den Mod ebenfalls laden | `keys/*.bikey` → `/data/server/keys/` **kopieren** |
| **Server-Mod** | `-serverMod=` | Läuft nur serverseitig (Admin-Tools, Anti-Cheat) | **kein** Key nötig |

Der Grund für die Unterscheidung beim Key: Mit `verifySignatures = 2` prüft der Server die von **Clients** geladenen Mods gegen die Dateien in `keys/`. Fehlt der Key eines Client-Mods, wird jeder Spieler beim Verbinden abgewiesen — das ist die häufigste Ursache für „Mod installiert, aber niemand kommt rein". Ein Server-Mod wird von keinem Client geladen, sein Key gehört deshalb nicht in `keys/`: er würde dort nur unnötig zusätzliche Signaturen erlauben.

Daraus ergibt sich eine Invariante, an der alles andere hängt: **ein Key liegt genau so lange in `keys/`, wie der Mod, der ihn mitbringt, aktiviert *und* Client-Mod ist.** Deaktiviert heißt nicht auf der Kommandozeile, also lädt ihn kein Client, also hat seine Signatur dort nichts zu suchen — dasselbe Argument wie beim Server-Mod.

Abgeglichen wird deshalb an jeder Stelle, an der sich die Antwort ändern kann: nach jedem Download, beim Typwechsel und beim Umlegen des Aktiv-Schalters.

Dazu **ein** Knopf über der Liste, *Sync keys*, der `keys/` nicht abgleicht, sondern **neu baut**: leeren, dann von jedem aktivierten Client-Mod neu kopieren. Er ist für die Fälle, die das Panel nicht mitbekommt — ein im Dateibrowser gelöschter Key, von Hand ausgetauschte Moddateien, ein aus einem älteren Backup zurückgeholtes `keys/`, Reste einer Panel-Version, die noch nicht aufgeräumt hat. Neu bauen statt abgleichen, weil die aktivierten Client-Mods die Antwort *sind*; ein Abgleich müsste raten, woher ein unbekannter Key kommt. Weil er löscht, fragt er vorher nach und sagt hinterher, was dabei herauskam — auch, welche Mods gar keinen Key mitbringen, was sonst niemand sieht, bis sich der erste Spieler nicht verbinden kann.

**`dayz.bikey` bleibt liegen.** Der Key des Basisspiels kommt mit den Serverdateien und gehört zu keinem Mod; ihn mit wegzuräumen hieße, jeden Spieler abzuweisen, bis die Serverdateien erneut validiert werden.

Vor dem Kopieren werden immer erst die bisherigen Keys des Eintrags entfernt (außer ein anderer installierter Mod bringt denselben mit): eine neue Version kann ihren Key unter anderem Namen mitbringen, und der alte bliebe sonst liegen. Beim **Entfernen** eines Mods wird der Ordner samt zugehöriger Keys gelöscht.

Bestehende Einträge werden dabei nicht beim Start durchgeputzt: ein deaktivierter Mod, dessen Key aus der Zeit vor dieser Regel noch in `keys/` liegt, behält ihn, bis ihn jemand anfasst. Beim Start Dateien zu löschen, um die man nicht gebeten hat, wäre die unangenehmere Überraschung — *Sync keys* räumt es auf, wenn der Betreiber es will.

Das Zielverzeichnis ist `keys/` — falls die Serverdateien es in anderer Schreibweise mitbringen, wird das vorhandene Verzeichnis verwendet statt ein zweites anzulegen (case-sensitives Dateisystem).

**Weitere Funktionen:**
- Liste: Name, Workshop-ID, **Typ (Client/Server)**, Größe, Aktualisierungsdatum, **aktiv/inaktiv**, Reihenfolge (Ziehen am Griff → bestimmt die Reihenfolge in der jeweiligen Kette)
- **Reihenfolge per Drag & Drop.** Vorher zwei Pfeilknöpfe je Zeile, also ein Request pro Schritt: einen frisch installierten Mod von unten nach oben zu bekommen waren zwölf Klicks. Gezogen wird nur am Griff — wäre die ganze Zeile das Ziehobjekt, verschluckte sie jeden Klick darin. Beim Loslassen geht die **ganze** Liste an `/mods/order`; „eins hoch" pro Schritt bräuchte dieselbe Rechnung auf beiden Seiten, und die wären beim ersten Sprung über drei Zeilen verschieden. Passt die gesendete Liste nicht zum Bestand (zweiter Tab, gelöschter Mod), wird sie abgelehnt und die Seite lädt neu, statt eine Reihenfolge zu speichern, die niemand so gesehen hat. Der Griff ist fokussierbar, `↑`/`↓` tun dasselbe ohne Maus.
- **Hinzufügen** per Workshop-ID oder -URL, als Job mit Live-Log; Typ wird dabei gewählt (Default: Client-Mod)
- **Update** einzeln oder „alle"; **Reinstall** löscht den Zielordner vorher; **Entfernen** löscht Ordner + Keys (mit Bestätigung), lässt den Workshop-Cache aber stehen, damit eine Neuinstallation schnell bleibt
- **Mods beim Start aktualisieren** (Settings → General, Default aus): aktualisiert vor jedem Serverstart alle installierten Mods — ausgeführt **nach** dem `app_update` der Serverdateien, da ein Serverupdate ohnehin passende Mods verlangt und die Reihenfolge sonst zu einem kurzzeitig inkonsistenten Stand führt (siehe §6.5a)
- Warnhinweis im UI, wenn ein Client-Mod ohne auffindbaren Key installiert wurde — der Server startet dann zwar, weist aber alle Spieler ab

#### Hochgeladene Mods

Nicht jeder Mod kommt aus dem Workshop: private Mods, selbst gebaute, oder solche, die der Betreiber als Zip in den Dateibrowser lädt. Erkannt wird ein Mod an dem, was die [Modding Basics](https://community.bistudio.com/wiki/DayZ:Modding_Basics) vorgeben — Ordner beginnt mit `@` und enthält `addons/` (Groß-/Kleinschreibung egal, denn gebaut wird auf Windows). Alles andere unter `@*` bleibt der Hinweis „kein Mod": eine abgebrochene Übertragung oder ein Ordner, der zufällig mit @ anfängt.

Zwei Wege dorthin: die Karte **Upload a mod** nimmt eine Zip entgegen, und der Dateibrowser nimmt ohnehin alles. Die Zip muss den `@Name`-Ordner selbst enthalten — lose PBOs in einen nach der Zip benannten Ordner zu packen hieße, den Modnamen zu raten, und ein Mod unter falschem Namen lädt auf keinem Server. Vor dem ersten geschriebenen Byte wird jeder Pfad im Archiv geprüft: ein Zip-Eintrag, der aus dem Serververzeichnis zeigt, wird abgelehnt, bevor er einen bestehenden Mod verdrängen kann. Eine zweite Zip mit demselben Ordnernamen ersetzt den Mod — so aktualisiert man einen hochgeladenen — und der Eintrag behält dabei Platz, Typ und Zustand. Was das Panel selbst auspackt, schreibt es klein: es sind seine eigenen Dateien, dieselbe Regel wie beim Download.

Ein so gefundener Mod wird beim Aufbau der Liste **übernommen** und ist danach ein Eintrag wie jeder andere: Typ, Reihenfolge, Keys, Löschen. Zwei Entscheidungen dazu:

- **Er kommt deaktiviert herein.** Ein Ordner, der auf der Platte auftaucht, darf sich nicht selbst auf die Kommandozeile des nächsten Starts setzen — das Übernehmen ist ein Fund, keine Anweisung.
- **Die ID wird abgeleitet, nicht gezählt.** Registry, Routen und Knöpfe kennen einen Mod an seiner Workshop-ID; ein hochgeladener hat keine. Er bekommt `10^14 + crc32(ordnername)` — ein Bereich, den Steam nicht erreicht (Published IDs sind heute zehnstellig, das Eingabemuster nimmt höchstens zwölf), und derselbe Ordner kommt nach einem Neustart mit derselben ID zurück. *Update* und *Reinstall* werden für ihn gar nicht erst angeboten: es gibt nichts, wovon man ihn holen könnte.

Kleingeschrieben wird bei einem hochgeladenen Mod **nichts**. Das Panel schreibt beim Download die Dateien um, weil es sie selbst dorthin gelegt hat; fremde Dateien beim Betrachten einer Seite umzubenennen wäre eine Änderung, um die niemand gebeten hat. Die Seite sagt stattdessen, dass DayZ unter Linux Kleinschreibung braucht.

#### Suche: nur mit API-Key

Steam bietet **keinen schlüsselfreien Such-Endpunkt** an. Die Auflösung einer ID oder URL zu Titel und Größe geht ohne Key (`ISteamRemoteStorage/GetPublishedFileDetails`) und prüft dabei gleich mit, ob das Item überhaupt zu DayZ gehört. Die eigentliche Suche (`IPublishedFileService/QueryFiles`) verlangt einen Key aus `STEAM_API_KEY`; fehlt er, sagt die Seite das und verweist auf die Installation per ID. Die Alternative wäre, die Store-Seite zu scrapen — das bricht, sobald Valve am Markup etwas ändert, und zwar unangekündigt.

#### Persistenz: JSON statt SQLite

Ursprünglich war SQLite vorgesehen. Es sind am Ende eine Handvoll Datensätze ohne Beziehungen, deren Reihenfolge einfach die Listenreihenfolge ist — dafür ist `/data/panel/mods.json` das bessere Format: der Betreiber hat `/data` ohnehin gemountet und kann die Datei mit einem Texteditor lesen und reparieren, ohne einen Datenbank-Client zu brauchen. Beide Startparameter-Ketten werden daraus erzeugt und in `server_settings.json` geschrieben, sodass die Settings-Seite jederzeit zeigt, womit der nächste Start tatsächlich läuft.

Dabei werden nur Mods aufgenommen, deren Ordner auch wirklich existiert: ein von Hand gelöschter Mod darf keinen toten `-mod=`-Eintrag hinterlassen, mit dem der Server nicht mehr startet.

### 6.5 Einstellungen (`ServerSettings`)

**Kein Datei-Editor, sondern ein Formular.** Eine einzige Seite mit den wichtigsten Werten aus `serverDZ.cfg` und den frei wählbaren Startparametern. Der Benutzer soll den Server konfigurieren können, ohne Syntax zu lernen oder eine kaputte Datei riskieren zu können.

#### Startparameter

Die Parameter zerfallen in zwei Gruppen — nur die zweite gehört ins UI:

| Vom Panel gesetzt (nicht editierbar) | Woher |
|---|---|
| `-config=serverDZ.cfg` · `-profiles=` · `-BEpath=` | feste Pfade des Volume-Layouts |
| `-port=` | Env (`SERVER_PORT`) |
| `-mod=` · `-serverMod=` | aus der Modliste generiert (§6.4) |

| Im UI einstellbar | Typ | Default | Wirkung |
|---|---|---|---|
| `-mission=` | Auswahl | `dayzOffline.chernarusplus` | Mission/Karte; Auswahl aus den Ordnern in `mpmissions/`. Wird zusätzlich in `class Missions` der `serverDZ.cfg` geschrieben — das ist der Wert, den die Engine tatsächlich lädt (§6.5) |
| `-cpuCount=` | Zahl | 2 | Logische Kerne für parallele Verarbeitung; ≤ verfügbare Kerne. Setzt zusätzlich `maxcores` in `dayzsetting.xml` (§6.5) |
| `-doLogs` | Schalter | an | Alle Logmeldungen in die RPT-Datei |
| `-adminLog` | Schalter | an | Admin-Log (`*.ADM`) — Grundlage für die Log-Ansicht |
| `-netLog` | Schalter | aus | Netzwerk-Traffic protokollieren; erzeugt viel Volumen |
| `-freezeCheck` | Schalter | an | Beendet den Server nach 5 min Einfrieren und schreibt einen Dump |
| `-filePatching` | Schalter | aus | Lädt ausschließlich PBOs, keine entpackten Daten; manche Mods verlangen die Gegeneinstellung |
| `-limitFPS=` | Zahl, leer erlaubt | leer | Begrenzt die Server-FPS (max. 200) und senkt die CPU-Last bei wenig Spielern |

**Leere Werte erzeugen keinen Parameter.** Ist `-limitFPS` leer, taucht der Parameter in der Kommandozeile gar nicht erst auf — `-limitFPS=` ohne Wert oder mit `0` wäre nicht dasselbe wie „keine Begrenzung". Gleiches gilt für Schalter, die auf „aus" stehen: sie werden weggelassen, nicht negiert.

#### serverDZ.cfg

Gruppiert im Formular, nicht als Textfeld:

Der Formularumfang ist der vollständige **main**-Block aus der DayZ-Dokumentation — alle 23 Schlüssel, nichts darüber hinaus. Die Hilfetexte folgen den dortigen Beschreibungen; wo ein Kommentar einen Wertebereich oder eine Folge erklärt, steht er sinngemäß im Feld.

- **Identität und Zugang:** `hostname`, `description`, `password`, `passwordAdmin`, `enableWhitelist`, `disableBanlist`, `disablePrioritylist`
- **Spieler und Warteschlange:** `maxPlayers`, `loginQueueConcurrentPlayers`, `loginQueueMaxPlayers`
- **Welt und Spielgefühl:** `disable3rdPerson`, `disableCrosshair`, `disableVoN`, `vonCodecQuality`
- **Weltzeit:** `serverTime`, `serverTimeAcceleration`, `serverNightTimeAcceleration`, `serverTimePersistent`
- **Sicherheit:** `verifySignatures`, `forceSameBuild`
- **Persistenz und Protokoll:** `instanceId`, `storageAutoFix`, `guaranteedUpdates`
- **RCON:** `rconPassword` — landet nicht in `serverDZ.cfg`, sondern in der BattlEye-Konfiguration (siehe unten); steht deshalb im Tab *General*

**Zwei Schreibweisen für Wahrheitswerte.** Die meisten Schalter stehen im
dokumentierten Block als `0`/`1`, `disableBanlist` und `disablePrioritylist`
aber als `false`/`true`. Das Panel hält beide auseinander — eine `0` wäre dort
nicht derselbe Wert.

Jedes Feld trägt Typ und Wertebereich, sodass eine Fehleingabe im Formular abgewiesen wird und nicht erst beim nächsten Start auffällt. Text- und Passwortfelder lehnen `"` und `;` ab: beide würden die Anweisung in der Datei vorzeitig beenden und damit eine gültige Konfiguration in eine kaputte verwandeln.

**Warum eine feste Feldliste und kein generischer Editor:** In `serverDZ.cfg` steht neben Skalaren auch der Block `class Missions`, und ein falscher Wert verhindert den Start. Ein Freitextfeld über dieser Datei lädt genau zu den Fehlern ein, die sich später am schwersten aus einem Log zurückverfolgen lassen.

#### Vor jedem Serverstart automatisch geschrieben

Fünf Werte werden nicht vom Benutzer gepflegt, sondern vom Panel unmittelbar vor jedem Start in die jeweilige Datei geschrieben. Sie stehen in Konfigurationsdateien, müssen aber zwingend zu dem passen, was der Container nach außen veröffentlicht oder was auf der Einstellungsseite gewählt wurde — würde man sie von Hand pflegen, wäre eine Abweichung nur eine Frage der Zeit.

| Datei | Schlüssel | Quelle |
|---|---|---|
| `serverDZ.cfg` | `steamQueryPort` | Env `STEAM_QUERY_PORT` |
| `serverDZ.cfg` | `template` in `class Missions` | Einstellungsseite, Feld *Mission* |
| `battleye/beserver_x64.cfg` | `RConPort` | Env `RCON_PORT` (Default `2305`) |
| `battleye/beserver_x64.cfg` | `RConPassword` | Einstellungsseite |
| `dayzsetting.xml` | `maxcores` (in `<jobsystem><pc>`) | Einstellungsseite, Feld *CPU count* |

**`class Missions`:** `-mission=` auf der Kommandozeile ist nicht das, was die Engine lädt — geladen wird das `template` aus diesem Block. Eine im Panel umgestellte Mission würde ohne diesen Schritt stillschweigend die alte Karte starten. Der Block wird über **Klammerzählung** gefunden, nicht über einen Zeilentreffer auf `template =`: der Schlüssel ist generisch genug, dass ein von Hand ergänzter zweiter Block sonst mitgeschrieben würde. Ersetzt wird ausschließlich der Name zwischen den Anführungszeichen, Kommentar und Struktur bleiben stehen. Fehlt der Block ganz, wird ein vollständiger angehängt (ohne ihn hätte der Server keine Mission); sind die Klammern unbalanciert, bleibt die Datei unangetastet — ein zweiter Block würde eine kaputte Datei nur schlimmer machen.

**`maxcores`:** `-cpuCount` betrifft nur die Simulationsthreads, die Größe des Worker-Pools im Jobsystem der Engine steht in `dayzsetting.xml`. Ein Feld, das nur die Hälfte davon setzt, wäre eine Falle — also schreibt *CPU count* beides: direkt beim Speichern (damit die Datei im Dateieditor sofort zum Formular passt) und noch einmal vor jedem Start, weil `dayzsetting.xml` mit den Serverdateien kommt und ein vor der Erstinstallation gesetzter Wert die neue Kopie sonst nie erreichen würde. Bearbeitet wird die Datei als Text mit gezieltem Ersetzen dieses einen Attributs statt über einen XML-Parser: ein Round-Trip durch ElementTree schreibt Attributreihenfolge, Anführungszeichen und Einrückung der gesamten Datei um — und diese Datei steht dem Betreiber im Dateieditor offen. Fehlt das Attribut, wird es ergänzt; fehlt der `<jobsystem><pc>`-Block ganz, bleibt die Datei unangetastet und es gibt eine Warnung im Log (eine DayZ-Version, die den Wert verschoben hat, ist besser gemeldet als geraten).

`steamQueryPort` ist der Fall, der sonst am ehesten schiefgeht: stimmt der Wert in der Datei nicht mit dem gemappten UDP-Port überein, läuft der Server normal, ist per Direktverbindung erreichbar — und bleibt in der Serverliste unsichtbar.

**BattlEye-Besonderheit:** Der DayZ-Server erzeugt beim Start aus `beserver_x64.cfg` eine Kopie namens `beserver_x64_active_<zufall>.cfg` und benutzt *diese*. Bleiben alte `*_active_*.cfg` liegen, läuft der Server unter Umständen weiter mit dem alten RCON-Passwort. Das Panel löscht sie deshalb vor jedem Start, bevor es `beserver_x64.cfg` schreibt.

#### Schreibweise: gezielt ersetzen statt neu erzeugen

Beim Speichern wird `serverDZ.cfg` **zeilenweise aktualisiert**: bekannte Schlüssel werden ersetzt, alles andere — Kommentare, Blöcke wie `class Missions`, manuell ergänzte Optionen — bleibt unverändert stehen. Die Datei komplett aus einer Vorlage neu zu schreiben wäre einfacher, würde aber jede Handanpassung stillschweigend verwerfen; und genau solche Anpassungen macht man an einem DayZ-Server ständig.

- **Kein Backup beim Speichern.** Das Formular ersetzt ausschließlich die Werte, die es anzeigt, Zeile für Zeile; alles andere in der Datei bleibt unberührt. Es gibt nichts, was das Speichern weggenommen hätte und ein Backup zurückholen müsste.
- Geschrieben wird erst, **nachdem alle Werte validiert sind** — ein abgelehnter Wert darf keine halb aktualisierte Datei hinterlassen
- Validierung im Formular (Zahlenbereiche) statt beim Server-Start
- Hinweis nach dem Speichern: „wirkt erst beim nächsten Serverstart"
- Passwortfelder werden maskiert angezeigt, mit Umschalter zum Sichtbarmachen

**Nicht abgehakte Checkbox ≠ unverändert.** Ein Browser überträgt eine nicht angehakte Checkbox überhaupt nicht. Würde das Formular „fehlt" als „unverändert" lesen, ließe sich kein Schalter je wieder ausschalten. Das Formular nennt deshalb in einem versteckten Feld die von ihm gerenderten Schlüssel; nur diese werden geschrieben, und ein fehlender Schalter darunter bedeutet `0`.

**Nicht enthalten:** ein allgemeiner Datei-Editor für `types.xml`, `events.xml` oder Mod-Configs. Diese Dateien sind über den Bind-Mount (`./data/server/...`) direkt vom Host aus zugänglich.

### 6.5a Aktualisieren vor dem Serverstart

Zwei Schalter unter *Settings → General*, beide unabhängig voneinander:

| Schalter | Wirkung |
|---|---|
| **Update server files on start** | `app_update 223350` vor dem Start |
| **Update mods on start** | `workshop_download_item` für jeden installierten Mod, **nach** dem Serverupdate |

**Gebunden an den Serverstart, nicht an den Containerstart.** Der Container wäre
gleich doppelt der falsche Moment: Mit `AUTO_START=false` startet beim Boot
überhaupt nichts, die Updates liefen also für niemanden — und ein Server, den
man Stunden später aus dem Dashboard startet, käme mit dem Stand von damals
hoch. Ausgelöst wird die Kette deshalb von jedem Start, den der Betreiber
anstößt: Knopf *Start*, Knopf *Restart*, `AUTO_START` beim Boot und später die
Schedules.

**Beide Schalter sind unabhängig.** Nur Mods, nur Serverdateien, beides oder
nichts — ein ausgeschalteter Schritt wird übersprungen, nicht als Fehler
behandelt. Sind beide aus, startet der Server sofort und synchron, damit ein
Fehlschlag in der Antwort steht statt in einem Thread zu verschwinden.

**Bei *Restart* wird zuerst gestoppt**, dann aktualisiert, dann gestartet.
Dateien unter einem laufenden Server auszutauschen ist genau das, was das Panel
an anderer Stelle mit einer Rückfrage verhindert.

**Ein Absturz-Neustart aktualisiert nicht.** Der Watchdog existiert, um den
Server wieder hochzubekommen; ein mehrminütiger Download davor macht aus einem
kurzen Ausfall einen langen, und ein fehlschlagender Download würde den Neustart
ganz verhindern.

**Ein fehlgeschlagenes Update verhindert den Start.** Sonst liefe der Server auf
halb geschriebenen Dateien — und der Betreiber muss sehen, warum. Die Meldung
landet in der Live-Konsole, die Job-Ausgabe auf der jeweiligen Seite.

### 6.6 Logs

**Zwei Dropdowns statt einer Kanalliste.** DayZ legt bei jedem Start neue
Dateien an (`DayZServer_<zeitstempel>.RPT`), sodass nach einer Woche Dutzende
herumliegen — und genau die von gestern will man lesen, wenn gestern etwas
kaputtging.

1. **Typ:** Admin Log (`*.ADM`), Server Report (`*.RPT`), Network Log (`*.net.log`, nur mit `-netLog`), Script Log (`script*.log`)
2. **Datei:** alle Dateien dieses Typs, neueste zuerst und vorausgewählt, mit Größe

**Nicht live, sondern Auto-Reload.** Ein Tail auf eine Datei, in die niemand
mehr schreibt, produziert nichts — die alten Dateien sind aber der Normalfall.
Der Schalter *Reload automatically* pollt stattdessen alle 3 s nur Größe und
Änderungszeit und lädt die Datei erst neu, wenn sich etwas bewegt hat. Das
Live-Bild des laufenden Servers ist die Konsole auf dem Dashboard.

- Filter/Suche über die geladenen Zeilen, Fehler und Warnungen eingefärbt
- **Download** liefert die vollständige Datei; die Anzeige ist auf die letzten 2 MB begrenzt, sonst blockiert eine 400-MB-RPT den Browser
- Der Dateiname aus dem Request wird gegen die tatsächlich vorhandenen Dateien geprüft, nie an das Verzeichnis angehängt — ein Pfad kann damit gar nicht erst entstehen

### 6.6a RCON (`RconService`)

BattlEye-RCON ist **nicht** das bekannte Source-RCON: eigenes Protokoll, und es
läuft über **UDP**. Daraus folgt alles Weitere.

- **Es gibt keine Verbindung.** „Eingeloggt" heißt: der Server hat ein Passwort
  akzeptiert und hat innerhalb der letzten 45 s etwas von uns gehört. Wer länger
  schweigt, wird kommentarlos vergessen.
- **Pakete gehen verloren, kommen doppelt oder überholen sich.** Jedes Kommando
  trägt eine Sequenznummer, die Antwort wird darüber zugeordnet. Die
  CRC32-Prüfsumme wird geprüft, nicht übersprungen: ein verfälschtes
  Sequenzbyte würde die Antwort des einen Kommandos an den Aufrufer eines
  anderen liefern.
- **Lange Antworten kommen zerlegt.** `players` auf einem vollen Server verteilt
  sich auf mehrere Datagramme, die nach Index wieder zusammengesetzt werden.
- **Servermeldungen müssen quittiert werden**, sonst schickt der Server dieselbe
  Meldung so lange erneut, bis er den Client aufgibt.

**Die Sitzung dauert ein Kommando.** Einloggen, fragen, Antwort lesen, Socket zu
— alles synchron im Thread des Aufrufers, kein Empfänger-Thread, kein Supervisor.
Eine gehaltene Sitzung müsste alle 18 s am Leben erhalten werden, und jedes
verlorene Keepalive — auf UDP ein normaler Vorgang, und beim Laden der Mission
antwortet BattlEye minutenlang gar nicht — steht als Verbindungsabbruch in der
Konsole. Was sie einbrachte, war ein dauerhafter Zuhörer für Servermeldungen;
daran hängt nichts: die Ausgabe des Servers kommt über den Prozess, nicht über
RCON. Meldungen, die während eines Kommandos eintreffen, landen weiterhin im
Konsolenpuffer.

**Ein fester Quellport.** BattlEye merkt sich einen Client an Adresse *und*
Port und hält den Eintrag 45 s nach dem letzten Paket. Zwanzig Kommandos von
zwanzig Ports sind für ihn zwanzig Clients — ab dem zehnten antwortet er nicht
mehr (gemessen). Vom selben Port sind es Anmeldungen desselben Clients: 20
Kommandos in 0,16 s. Der Port kommt einmal vom Betriebssystem und wird danach
wiederverwendet; ist er zwischendurch belegt, tut es auch ein flüchtiger.

**Sequenznummer 0.** Ein Login setzt die Zählung zurück: der Server beantwortet
0 und ignoriert alles andere (gemessen). Bei einem Kommando pro Sitzung gibt es
nie eine zweite Nummer. Weil sich die Sitzungen einen Quellport teilen, wird der
Socket vor dem Login leergelesen — eine sehr späte Antwort auf das vorige
Kommando trüge sonst die Nummer des aktuellen.

**Kommandos laufen nacheinander.** Zwei gleichzeitige Sitzungen wären zwei
Sockets auf demselben Port, die einander die Datagramme wegläsen.

**Ein abgelehntes Passwort wird nicht wiederholt**: es gibt keine Schleife mehr,
die es könnte — ein Loginversuch entsteht nur noch durch einen Klick.

Was das Panel anzeigt, ist `ready`: Passwort gesetzt und Server im Zustand
*running*. Mehr lässt sich vor dem Klick nicht wissen, ohne genau die
Dauerverbindung zu unterhalten, die hier abgeschafft wurde. Scheitert ein
Kommando, steht der Grund am Kommando — und bis zum nächsten Erfolg neben dem
RCON-Punkt unter der Konsole.

### 6.7 Schedules

Wiederkehrende Aufgaben im Crontab-Format. Ein Eintrag besteht aus einem
Zeitausdruck (`0 4 * * *`) und **einer oder mehreren** Aktionen, die
nacheinander laufen: Start, Stop, Restart, Lock, Unlock, RCON-Kommando.

Mehrere Aktionen pro Eintrag, weil ein Neustart in der Praxis selten allein
kommt: erst eine Ankündigung per RCON, dann sperren, dann neu starten. Drei
getrennte Einträge um 03:55, 03:58 und 04:00 würden dasselbe ausdrücken — und
still auseinanderfallen, sobald jemand einen davon verschiebt.

- Ausführung über **APScheduler** im Panel-Prozess, Persistenz in
  `/data/panel/schedules.json`
- Jeder Eintrag zeigt nächste Ausführung und Ergebnis des letzten Laufs; *Run
  now* führt ihn sofort aus — die einzige ehrliche Art, einen Eintrag zu testen
- Die Aktionen laufen durch dieselben Wege wie die Knöpfe auf dem Dashboard — kein zweiter Pfad, der sich anders verhalten kann
- **Abbruch bei der ersten fehlgeschlagenen Aktion.** Der Sinn von „ankündigen →
  sperren → neu starten" ist die Reihenfolge; ein Neustart nach einem
  fehlgeschlagenen Sperren ist nicht das, was der Betreiber gewollt hat
- **Nur ein Eintrag gleichzeitig** (`max_instances=1` plus ein Lock über alle
  Einträge): zwei Einträge zur selben Minute würden sonst einen Stopp mit dem
  Start des anderen verschränken
- **Verpasste Läufe werden nicht nachgeholt** (`misfire_grace_time` 120 s). Ein
  4-Uhr-Neustart, der um 4:20 nachkommt, weil der Container gerade aktualisiert
  hat, ist schlimmer als einer, der ausfällt
- Zeiten werden **serverseitig formatiert**: Cron feuert in der Zeitzone des
  Containers, ein Betreiber aus einer anderen läse sonst eine Uhrzeit, die für
  niemanden stimmt
- Ein Start oder Neustart kehrt zurück, sobald er angestoßen ist — vor ihm kann
  ein mehrminütiges Update liegen. Aktionen, die **vorher** passieren sollen,
  gehören deshalb darüber in die Liste
- **„Run while the server is stopped"** je Eintrag, per Vorgabe an. Aus
  übersprungen der Eintrag den Lauf, statt einen Server zu starten, den gerade
  niemand haben will — die Neustartkette während einer Wartung ist der Fall.
  Der Eintrag wird trotzdem gestempelt (`last_ok = null`, in der Liste ein
  graues *skipped*): ausgefallen und ausgelassen sehen sonst gleich aus. Gilt
  auch für *Run now* — ein Testlauf, der die Regel ignoriert, testet etwas
  anderes als das, was um vier Uhr feuert

### 6.7a Files

Dateibrowser über `/data/server`, wie man es von einem Dateimanager erwartet:
Ordner anklicken, Breadcrumb zurück, Textdateien bearbeiten.

- Bearbeitbar ist alles, was sich als Text öffnen lässt (`types.xml`, Missionsdateien, Mod-Configs); Binärdateien werden erkannt und nur zum Download angeboten
- Backup vor dem **Löschen** und vor dem **Überschreiben durch einen Upload** nach `/data/backup/files/`, dort in derselben Ordnerstruktur — sonst überschreiben sich drei verschiedene `types.xml` in einem flachen Verzeichnis
- **Kein Backup beim Speichern aus dem Editor.** Der vorherige Inhalt stand gerade auf dem Bildschirm und die Änderung war Absicht; zweimal Speichern beim Arbeiten an einer Datei füllte sonst `/data/backup` mit Kopien desselben Edits und begrübe die Kopien, auf die es ankommt — die vom Löschen und Überschreiben, wo Inhalt verschwindet, den niemand gesehen hat
- Der Editor hat zwei vollständige Wege hinaus (*Save and close*, *Discard and close*) statt getrenntem Speichern und Schließen: als zwei Schritte sah der zweite optional aus. Geschlossen wird erst, wenn die Antwort da ist — sonst behauptete der Klick den Erfolg, bevor ihn jemand kennt
- Up- und Download einzelner oder mehrerer Dateien
- Jeder Pfad wird nach `realpath` gegen `/data/server` geprüft — auch ein Symlink kann nicht hinausführen
- Kopfzeile: links der Breadcrumb, rechts die Aktionen. Pro Zeile ein `⋯`-Menü (Rename, Move, Edit, Download, Delete) statt einer Knopfreihe — bei fünf Einträgen wären die Knöpfe breiter als die Namen, zu denen sie gehören
- **Kein „Up"-Knopf**, sondern immer eine erste Zeile `..` wie in WinSCP: dort ist der Blick ohnehin, und „diesen Ordner verlassen" gehört in dieselbe Liste wie „jenen betreten"
- **Mehrfachauswahl** über Checkboxen, mit Bulk-Aktionen *Move* und *Delete*. Sie melden Teilerfolge als solche — ein nicht leerer Ordner unter zehn Löschungen ist der Normalfall, und „alles erledigt" wäre dort so falsch wie „fehlgeschlagen"
- Ein Verzeichnis in sich selbst zu verschieben wird abgewiesen: Python tut es klaglos und hängt damit den ganzen Teilbaum aus dem Dateisystem aus
- *New file* legt eine leere Textdatei an und öffnet sie sofort im Editor — eine leere Datei ist nie das Ziel, sondern der Schritt davor
- **Keine nativen Dialoge.** Alle Rückfragen laufen über einen eigenen Dialog. Ein Browser lässt den Besucher `prompt()`/`confirm()` für eine Seite abschalten; danach liefert `prompt()` stillschweigend `null`, und der Knopf tut nichts mehr — ohne Fehlermeldung, ohne Spur im Log

**Nicht bereinigt, sondern aufgelöst.** `..` aus einer Zeichenkette zu
schneiden ist ein Spiel, das man irgendwann verliert — und gegen einen Symlink
hilft es gar nicht: `/data/server/mpmissions/link -> /etc` enthält kein `..`
und ist trotzdem ein Ausgang. `Path.resolve()` folgt ihm, und das Ergebnis
liegt entweder unter der Wurzel oder eben nicht.

**Text oder nicht entscheidet der Inhalt**, nicht die Endung: ein NUL-Byte in
den ersten 8 KB genügt — derselbe Test, den `file` und `git` benutzen. In einem
DayZ-Serververzeichnis lügen Endungen in beide Richtungen. Eine Datei, die
nicht als UTF-8 dekodiert, wird ebenfalls abgelehnt: mit `errors="replace"`
sähe sie lesbar aus, und das Speichern schriebe die Ersatzzeichen über die
Bytes zurück.

**Rekursives Löschen gibt es nicht.** Ein nicht leerer Ordner wird abgelehnt.
Einen Mod- oder Missionsordner mit einem Klick zu verlieren ist ein zu
leichter Unfall; dafür ist der Bind-Mount da.

**Die Wurzel ist `/data/server`, nicht `/data`** — sonst lägen
`server_settings.json` und die Steam-Sentry-Datei in Reichweite eines
Texteditors im Browser.

Das Upload-Limit ist `MAX_UPLOAD_MB` (Default 64) und gilt als
`MAX_CONTENT_LENGTH` für **jede** Anfrage — es ist die einzige Schranke dafür,
was eine angemeldete Sitzung überhaupt in den Container schieben kann.

### 6.7b Audit-Log

Die Serverlogs sagen, was das *Spiel* getan hat. Sie sagen nichts über das
Panel: wer den Server um 3 Uhr neu gestartet hat, wer `verifySignatures`
geändert, wer einen Mod gelöscht hat. Auf einem Server, den mehrere verwalten,
ist das die Lücke, aus der „der Server ist seit gestern komisch" entsteht.

- Eine JSON-Zeile pro Eintrag in `/data/panel/audit.log`: Zeit, Benutzer, IP,
  Aktion, Ziel, Erfolg, Detail. JSON Lines statt Datenbank, weil die Datei mit
  `tail` und `grep` auf dem Host lesbar sein soll — und weil Anhängen die
  einzige Operation ist, die dem nächsten Leser keinen halben Datensatz
  hinterlassen kann
- **Nur Änderungen, keine Abrufe.** Jedes Verzeichnislisting mitzuschreiben
  begräbt die Handvoll Einträge, die etwas aussagen
- **Fehlversuche gehören dazu**, besonders bei der Anmeldung: eine Serie von
  einer Adresse ist das einzige Zeichen, das das Panel gibt, dass es jemand
  versucht
- Werte werden **nicht** mitgeschrieben, nur die Namen der geänderten Felder —
  eines davon ist das RCON-Passwort, und die Datei liegt im Bind-Mount
- Angezeigt als zweiter Tab auf der Log-Seite. Kein siebter Menüpunkt: beide
  Tabs beantworten „was ist passiert", einmal aus Sicht des Spiels und einmal
  aus Sicht des Panels
- Kein Sicherheitsmerkmal: wer Zugriff auf das Volume hat, kann die Datei
  ändern. Ein Protokoll für Leute, die einen Server gemeinsam betreiben

### 6.7c Backups

Gesichert wird der komplette `/data/server` — Serverdateien, Mods, Missionen samt
Persistenz, Profile, Configs. Ein zurückgespielter Snapshot ergibt damit einen
sofort startfähigen Server, ohne dass SteamCMD noch einmal laufen muss.

#### Warum restic und nicht ein Archiv pro Lauf

`/data/server` sind ~3,8 GB. Ein `tar.gz` pro Sicherung heißt: 3,8 GB pro Lauf,
für einen Stand, der sich zwischen zwei Läufen meist nur in der Persistenz
unterscheidet. Zehn Sicherungen wären 38 GB für vielleicht 300 MB echte
Unterschiede — das ist der Grund, warum an dieser Stelle eine Engine mit
Deduplizierung steht und kein `tarfile`-Aufruf.

**restic 0.18** (Debian trixie, `main`, 8 MB Download / 24,8 MB installiert)
dedupliziert auf **Block**ebene und komprimiert mit zstd: der erste Snapshot
kostet den vollen Umfang, jeder weitere nur die geänderten Blöcke. Gemessen an
einem Testrepository: nach dem Anhängen einer Zeile an eine Datei belegte der
zweite Snapshot **773 Byte**.

Ausschlaggebend für die Wahl war aber `--json` auf **jedem** Kommando:
Fortschritt, Snapshot-Liste, Aufräum-Ergebnis und Restore-Zusammenfassung
kommen strukturiert zurück statt als Text, den das Panel parsen müsste.

**Verworfene Alternativen:**

- **rsync `--link-dest`** (rsync ist bereits im Image, Hardlinks funktionieren
  auf dem Bind-Mount — geprüft). Jede Sicherung wäre ein vollständiger,
  begehbarer Baum, unveränderte Dateien nur Hardlinks; wiederherstellbar mit
  `cp`, ganz ohne Werkzeug. Scheitert an der Granularität: gespart wird pro
  **Datei**, nicht pro Block. Eine geänderte 200-MB-Persistenzdatei kostet
  jedes Mal volle 200 MB. Dazu käme eine selbstgeschriebene Aufbewahrungslogik
- **borgbackup** kann dasselbe wie restic, hat aber keine brauchbare
  JSON-Ausgabe und steckt in Debian auf 1.4 fest

#### Ablage und Schlüssel

| Pfad | Inhalt |
|---|---|
| `/data/backup/repo` | restic-Repository |
| `/data/backup/backup_key` | Repository-Schlüssel, automatisch erzeugt, `chmod 600` — liegt neben dem Repository, wird beim Kopieren von `/data/backup` also mitgenommen |
| `/data/panel/backup.json` | Aufbewahrungsregeln und Ausschlussliste |

Die Sicherungen sind **verschlüsselt**, der Schlüssel liegt neben dem
Repository in `/data/backup`. Das ist eine bewusste Entscheidung des Betreibers
und schränkt den Nutzen ein: Wer den ganzen Ordner wegkopiert, nimmt den
Schlüssel mit — geschützt ist damit nur der Fall, dass jemand allein an die
`repo/`-Dateien gerät. Was in jedem Fall bleibt: Die Snapshots hängen an einer
einzelnen Datei, die man aufbewahren kann — und deren Verlust die Sicherungen
kostet, weshalb das Panel nie stillschweigend eine neue erzeugt. Der Preis ist ehrlich zu
nennen: **ohne die Schlüsseldatei ist das Repository unlesbar.** Die
Backups-Seite weist darauf hin und bietet den Schlüssel zum Sichern an.

#### Auslösen

- **Dashboard:** Knopf *Backup* rechts neben *Unlock*
- **Schedules:** neue Aktion `backup`. Anders als `start` wartet sie auf ihr
  Ende — sonst liefe der Server mitten in den Snapshot hinein. Die Kette, um
  die es eigentlich geht: `say -1 Backup in 60s` → `lock` → `stop` → `backup`
  → `start`
- Ein Backup läuft als **Job** über den vorhandenen Job-Manager. Der hat genau
  einen Platz, also kann eine Sicherung nie gleichzeitig mit einem
  SteamCMD-Update laufen — genau das ist hier erwünscht
- Vor dem Start wird der freie Plattenplatz geprüft. Ein abgebrochener Snapshot
  wegen voller Platte ist der unangenehmste Weg, das zu erfahren

**Snapshot vom laufenden Server:** unvermeidlich in sich zerrissen, weil die
Persistenz gerade geschrieben wird. Solche Snapshots bekommen den Tag `hot` und
in der Liste ein Abzeichen. Nicht verbieten — eine zerrissene Sicherung ist
immer noch besser als keine —, aber sichtbar machen. Weitere Tags: `manual`,
`scheduled`, `pre-restore`.

#### Seite *Backups*

- Kopfzeile: Repository-Größe, Anzahl Snapshots, freier Plattenplatz
- Tabelle: Zeitpunkt, Tags, Größe des Stands und **was der Snapshot tatsächlich
  belegt** (`data_added_packed`). Die zweite Zahl ist die interessante: sie
  zeigt, dass die zehnte Sicherung ein paar MB kostet und nicht 3,8 GB
- **Download** als `.tar`, direkt aus `restic dump --archive tar` gestreamt —
  kein Temporärarchiv, kein doppelter Plattenbedarf. Neben dem ganzen Snapshot
  auch gezielt Unterordner (`mpmissions/`, `profiles/`): 3,8 GB durch den
  Browser will niemand, die 293 MB Missionsdaten schon
- **Aufbewahrung:** *keep last N* und *delete after X days*, ausgeführt nach
  jedem erfolgreichen Backup als `restic forget --prune`. Wichtig für den
  Hilfetext: sind beide gesetzt, behält restic einen Snapshot, wenn **eine** der
  Regeln ihn behält — ein Oder, kein Und
- **Ausschlussliste**, Standard leer. Wer das Repository klein halten will,
  kann `addons/` und `dta/` ausnehmen (2,7 GB, jederzeit über SteamCMD
  nachladbar). Bewusst nicht Standard: die Vorgabe soll die Sicherung sein, aus
  der ein Server ohne Steam-Login wieder hochkommt

#### Restore

```
restic restore <id> --target / --include /data/server --delete
```

Zwei Details, die beim Ausprobieren aufgefallen sind und sonst beim
Implementieren aufgefallen wären:

- `--delete` ist **nötig**. Ohne das Flag ist ein „Restore" nur ein
  Drüberkopieren: alles, was seit dem Snapshot dazugekommen ist, bliebe liegen —
  und genau das ist bei einem verkorksten Mod-Update der Punkt der Übung
- restic **verweigert** `--target / --delete` ohne Include-Filter (*„must be
  combined with an include or exclude filter"*). Das Include ist also kein
  Beiwerk, sondern die Bedingung, unter der restic das Löschen überhaupt zulässt

Ablauf beim Klick auf *Restore*: Bestätigungsdialog, der genau ankündigt was
passiert → Server stoppen → `pre-restore`-Snapshot (durch die Deduplizierung
fast kostenlos und die einzige Rückfahrkarte, wenn man den falschen Eintrag
erwischt hat) → zurückspielen → Server wieder starten. Ein Restore ist immer ein
Notfall; in dieser Lage noch drei Knöpfe in der richtigen Reihenfolge zu
verlangen, ist der Moment, in dem Fehler passieren.

#### Gemessen an diesem Server

| | Gelesen | Im Repository abgelegt |
|---|---|---|
| Erste Sicherung | 3,78 GB | 1,6 GB (23 s) |
| Zweite, eine Datei geändert | 3,78 GB | 4,8 KB |
| Mit `addons`/`dta` ausgeschlossen | 1,18 GB | 72 KB |

#### Warum die Seite den Snapshot-Stand zwischenspeichert

Jeder restic-Aufruf kostet rund **0,75 s, bevor er irgendetwas tut**: das
Öffnen des Repositories leitet den Schlüssel mit scrypt ab, einer absichtlich
teuren Funktion. Selbst ein `restic cat config` braucht so lange. Zwei Aufrufe
je Seitenaufruf (Snapshot-Liste und Repository-Größe) ergaben **1,9 s** für die
Backups-Seite, während die übrigen Seiten bei 30 ms liegen.

Beide Werte werden deshalb gemeinsam geholt und im Service gehalten. Das Panel
ist der einzige Schreiber, also verwirft es den Zwischenspeicher selbst nach
**jedem** Job (im `finally`, nicht nur bei Erfolg — ein zur Hälfte
gescheitertes Backup kann trotzdem einen Snapshot geschrieben haben) und füllt
ihn sofort im Hintergrund wieder. Die Altersgrenze von 60 s ist nur der
Rückfall für Änderungen, die jemand mit restic auf dem Host macht. Beim Start
des Panels wird einmal vorgewärmt, damit nicht der erste Besuch die Rechnung
zahlt. Ergebnis: 1856 ms → 4 ms, und nach einem Job 311 ms statt 1959 ms.

Der freie Plattenplatz wird bewusst *nicht* mitgespeichert: ein Syscall, und er
ändert sich, ohne dass sich am Repository etwas tut.

**Ein unverschlüsseltes Repository wäre nicht schneller** — nachgemessen, weil
die Frage naheliegt: `cat config` 626 ms mit Schlüssel gegen 697 ms mit
`--insecure-no-password`, bei `snapshots` und `stats` dasselbe Bild. Beide
Key-Dateien tragen dieselben Parameter (`scrypt N=32768 r=8 p=7/8`). Ein
restic-Repository ist *immer* verschlüsselt; der Schalter bedeutet nur „das
Passwort ist leer", und scrypt läuft dann eben darüber. Die Verschlüsselung
kostet also nichts, und der Zwischenspeicher war die einzige Stellschraube.

#### Die Aufbewahrung zählt Snapshots, nicht Containerleben

`restic forget` gruppiert seine Regeln standardmäßig nach **Host und Pfad**.
Docker vergibt bei jedem `up`/Recreate einen neuen Hostnamen, also wäre
„behalte die letzten 5" in Wahrheit „behalte 5 pro Container, in dem das Panel
je gelaufen ist" — und ältere Snapshots würden nie aufgeräumt. Im Test blieben
nach `--keep-last 1` drei Snapshots von drei Hostnamen übrig.

Zwei Gegenmaßnahmen, weil eine allein nicht reicht: Jeder Snapshot wird unter
dem festen Hostnamen `dayz` geschrieben (`backup --host`), und `forget` läuft
mit `--group-by ""`. Das Erste sorgt für eine saubere Liste in Zukunft, das
Zweite erfasst auch die Snapshots, die vorher unter wechselnden Namen entstanden
sind.

#### Zwei Dinge, die erst beim Bauen sichtbar wurden

**Ein Fehler beim Zurückspielen darf den Server nicht unten lassen.** Eine
einzige Datei, die dem Panelbenutzer nicht gehört, reicht: restic spielt alles
andere zurück und beendet sich trotzdem mit einem Fehler
(`chmod ...: operation not permitted`). Würde die Ausnahme den Ablauf abbrechen,
bliebe ein gestoppter Server zurück — aus einer Kleinigkeit wird ein Ausfall.
Der Neustart läuft deshalb auch im Fehlerfall, und der Fehler wird danach
gemeldet.

**restic schreibt seine Fehler mit `--json` ebenfalls als JSON**, und zwar auf
stderr. Unverändert weitergereicht stünde beim Betreiber
`{"message_type":"exit_error",...}` — ausgepackt steht dort der Satz, auf den es
ankommt: welche Datei, und warum. Die Abschlussmeldung („There were 1 errors")
wird dabei bewusst *nicht* als Fehlertext genommen; sie sagt nichts.

### 6.8 Sicherheit
- Flask-Login; `ADMIN_PASSWORD` wird beim Start gehasht (Werkzeug), Klartext nur im Prozessspeicher der Startphase
- CSRF-Schutz (Flask-WTF) für alle mutierenden Requests, Rate-Limiting auf Login (Flask-Limiter)
- Audit-Log für jede Aktion (Benutzer, Zeitstempel, Ergebnis)
- `subprocess` ausschließlich mit Argumentliste, nie `shell=True`
- Secrets werden in Logs und UI maskiert
- README-Hinweis: Panel-Port nicht ungeschützt ins Internet, Reverse-Proxy + TLS vorschalten

### 6.9 TLS — bewusst außerhalb des Containers

Das Panel spricht **nur HTTP** und terminiert kein TLS. Zertifikate übernimmt ein vorgelagerter Reverse-Proxy (Traefik, Caddy, nginx Proxy Manager …), der das ohnehin für alle Dienste auf dem Host löst.

Der Grund ist nicht nur Bequemlichkeit: Ein Zertifikats-Renewal im Container hätte einen Reload des Webservers erfordert. Da das Panel den DayZ-Serverprozess, die Logpuffer und den Job-Status im Arbeitsspeicher hält, hätte jeder Reload diesen Zustand verworfen und den laufenden Serverprozess verwaist zurückgelassen — man hätte entweder einen zweiten Prozess (nginx) als TLS-Terminator davorsetzen oder eine PID-Wiederanbindung bauen müssen. Beides ist unnötig, wenn der Proxy außerhalb liegt.

**Was das Panel dafür können muss** (Umsetzung in Phase 2):

- **Eigene Middleware `TrustedProxyFix`** für `X-Forwarded-For/-Proto/-Host/-Port`, konfiguriert über **`TRUSTED_PROXY_IPS`** (IP-Adressen und CIDR-Blöcke, komma-getrennt; leer = Header werden ignoriert; `*` = jedem Peer vertrauen).

  Bewusst **nicht** Werkzeugs `ProxyFix`: das zählt nur Hops und prüft nie, *wer* die Header geschickt hat — steht kein Proxy davor oder ist das Panel auch direkt erreichbar, glaubt es jedem Client. Ein gefälschter `X-Forwarded-For` umgeht damit das Login-Rate-Limit, ein gefälschter `X-Forwarded-Proto` lässt einen unverschlüsselten Request als HTTPS gelten. `TrustedProxyFix` wertet die Header nur aus, wenn die Verbindung selbst von einer erlaubten Adresse kommt.

  Die Client-IP wird aus der `X-Forwarded-For`-Kette **von rechts** ermittelt, unter Überspringen bekannter Proxys — der linke Teil der Kette stammt vom Client und kann beliebig gefüllt sein.
- **Session-Cookies:** `HttpOnly` und `SameSite=Lax` immer; `Secure` schaltbar über `SESSION_COOKIE_SECURE` (Default `true`, für den Betrieb ohne TLS abschaltbar).
- **SSE-Verträglichkeit dokumentieren:** Der Log-Stream darf vom Proxy nicht gepuffert werden. Traefik puffert standardmäßig nicht; bei nginx sind `proxy_buffering off` und ein hohes `proxy_read_timeout` nötig.
- Der Container veröffentlicht den Panel-Port dann nicht auf dem Host, sondern hängt im Netz des Proxys.

---

## 7. Bekannte Stolpersteine & geplante Lösungen

**7.1 Steam Guard**
Der Erstlogin verlangt einen Code. Gelöst über drei Wege, in dieser Reihenfolge: `STEAM_GUARD_CODE` aus der Env → Code-Eingabe im Panel bei Job-Status `needs_guard` → Fallback `docker compose exec dayz steamcmd +login <user>` für einen manuellen Lauf. Danach persistiert die Sentry-Datei in `/data/steam`.

**7.2 sdk32/sdk64-Symlinks zur Laufzeit**
Der DayZServer erwartet `steamclient.so` unter `~/.steam/sdk64/`. Diese Symlinks im Dockerfile anzulegen funktioniert **nicht**, weil das Steam-Home im Volume `/data/steam` liegt und ein zur Buildzeit erzeugtes Verzeichnis beim Mount überdeckt wird. Deshalb legt `entrypoint.sh` sie idempotent an, nachdem das Volume gemountet ist:
```
$STEAM_HOME/.steam/sdk32 → $STEAM_HOME/.local/share/Steam/steamcmd/linux32
$STEAM_HOME/.steam/sdk64 → $STEAM_HOME/.local/share/Steam/steamcmd/linux64
sdk{32,64}/steamservice.so → sdk{32,64}/steamclient.so
```
Da die Ziele erst nach dem ersten SteamCMD-Lauf existieren, wird der Schritt nach jedem SteamCMD-Job erneut ausgeführt.

**7.3 Gunicorn-Worker**
Mehr als ein Worker bedeutet mehrere `ServerManager`-Instanzen → falscher Status, verwaiste Prozesse. Fest: `--workers 1 --threads 8 --worker-class gthread`. In `gunicorn.conf.py` verankern, nicht nur im Startbefehl.

**7.4 Case-Sensitivity bei Mods**
Häufigste Fehlerursache auf Linux-DayZ-Servern. Die Normalisierung ist Pflichtbestandteil von `ModManager`, kein Komfortfeature.

**7.5 Sauberes Herunterfahren**
Bei `docker stop` muss das Panel den DayZ-Server zuerst beenden, damit Spielerdaten persistiert werden: `atexit`-Hook im Panel + `stop_grace_period: 60s`, Tini als PID 1 gegen Zombies. Entscheidend ist dabei die Reihenfolge der Zeitlimits — `graceful_timeout` von Gunicorn (Default 30 s) muss **über** dem eigenen Stop-Timeout (30 s + Nachlauf) und **unter** der `stop_grace_period` liegen, sonst killt Gunicorn den Worker mitten im Herunterfahren und der DayZ-Prozess bleibt verwaist zurück. Gesetzt: 50 s. Seit der Stop-Timeout einstellbar ist, hängt diese Kette **nicht** an ihm: der atexit-Pfad nimmt `min(Einstellung, CONTAINER_STOP_TIMEOUT)`. Länger zu warten würde dem Server keine Zeit schenken, sondern nur den Kill von Gunicorn zu Docker verschieben.

**7.6 Rechte auf Bind-Mounts**
`chown -R` auf `/data` gemäß `PUID`/`PGID` im Entrypoint, **bevor** per `gosu` auf `steam` gewechselt wird.

**7.7 Image-Größe**
Serverdateien (mehrere GB) gehören ins Volume, nicht ins Image. Das Image bleibt damit im niedrigen dreistelligen MB-Bereich.

**7.8 `serverDZ.cfg` schreiben, ohne Handanpassungen zu verlieren**
Die Einstellungsseite ersetzt gezielt einzelne Zeilen, statt die Datei aus einer Vorlage neu zu erzeugen (§6.5). Sonst verschwinden Kommentare, `class Missions` und manuell ergänzte Optionen beim ersten Speichern. Backup vor jedem Schreibvorgang; eigene Unit-Tests gegen die Ersetzungslogik.

**7.9 SteamCMD-Prompts haben keinen Zeilenumbruch** *(in Phase 3 aufgetreten und gelöst)*
SteamCMD schreibt `Steam Guard code:` **ohne** abschließenden Zeilenumbruch und blockiert dann auf stdin. Ein zeilenweiser Leseloop läuft damit in einen Deadlock: die Zeile wird nie fertig, und der Prozess läuft nie weiter, weil niemand antwortet. `SteamCmdService` liest deshalb rohe Blöcke mit `os.read`, behält den unvollständigen Rest und prüft **diesen** gegen die bekannten Prompts. Zusätzlich nutzt SteamCMD `\r` zum Neuzeichnen der Fortschrittsanzeige — beide Zeichen gelten als Zeilentrenner.

**7.10a Inline-Kommentare in der `.env`** *(beim ersten echten Steam-Login aufgetreten)*
Docker Compose entfernt ein nachgestelltes `# …` in einer `env_file` **nur bei nicht-leeren Werten**. Bei `STEAM_GUARD_CODE=   # Einmalcode` wurde der Kommentartext zum Wert, landete als vierter Parameter im `+login`-Aufruf und Steam antwortete mit dem irreführenden `Invalid Password`. Dasselbe traf `PANEL_SECRET_KEY` — der Session-Schlüssel war damit ein öffentlich bekannter Text aus dem Template. Konsequenz: In `.env.example` stehen Kommentare grundsätzlich auf eigenen Zeilen, und `config.py` erkennt hereingerutschte Kommentare (`_looks_like_comment`) und ignoriert sie mit Warnung. Der Guard-Code wird zusätzlich auf sein tatsächliches Format geprüft.

**7.10b `-BEpath` zeigt auf das mitgelieferte `battleye/`** *(bei der echten Installation festgestellt)*
Die Serverdateien bringen `/data/server/battleye` mit `beserver_x64.so` mit. `-BEpath` muss auf dieses Verzeichnis zeigen — ein eigenes unter `profiles/` wäre leer, und BattlEye fände seine Bibliothek nicht. `beserver_x64.cfg` mit `RConPort`/`RConPassword` gehört ebenfalls dorthin.

**7.10c BattlEye *verschiebt* seine Konfiguration** *(in Phase 4 am laufenden Server beobachtet)*
Beim Start wird `beserver_x64.cfg` nicht kopiert, sondern in `beserver_x64_active_<zufall>.cfg` **umbenannt** — nach dem ersten Start existiert das Original also nicht mehr. Eine frisch erzeugte Datei würde damit bei jedem Neustart alle Handanpassungen (`MaxPing`, `RestrictRCon`, …) verlieren. `server_config.py` benennt deshalb die neueste aktive Kopie zuerst zurück, löscht erst danach die restlichen und schreibt anschließend `RConPort`/`RConPassword` hinein. Ein leeres `RConPassword` wird nicht geschrieben, sondern die Zeile entfernt: ein leerer Wert würde RCON ohne Passwort aktivieren, und ein gelöschtes Passwort muss auch wirklich verschwinden.

**7.10d Ein Stopp ist erst vorbei, wenn der Manager es gemerkt hat** *(in Phase 4 aufgetreten)*
`proc.wait()` kehrt zurück, sobald der Prozess weg ist — der Watcher-Thread hat den Zustandswechsel dann aber noch nicht eingetragen. Ein Restart, der direkt danach `start()` aufruft, sieht den Server deshalb noch als „läuft" und bricht ab. Jeder Start legt daher ein `threading.Event` an, das der Watcher nach dem Aufräumen setzt; `stop(wait=True)` wartet darauf.

**7.10e DayZ legt bei jedem Start neue Logdateien an** *(in Phase 5 festgestellt)*
Die Serverlogs heißen `DayZServer_<zeitstempel>.RPT` bzw. `.ADM` — nach jedem Neustart also anders. Ein Kanal, der eine feste Datei liest, wäre nach dem ersten Neustart stumm, ohne dass man es merkt. Die Kanäle zeigen deshalb auf ein Glob-Muster und folgen immer der **neuesten** Datei; wechselt sie, meldet der Stream das (`--- following <datei> ---`) und der Client ersetzt seine Ansicht. Dasselbe gilt für abgeschnittene Dateien (Offset größer als die Datei) und für In-Memory-Puffer, die bei Serverneustart oder neuem Job ausgetauscht werden — jeder Puffer hat dafür eine eigene ID im Cursor.

**7.10f BattlEye öffnet den RCON-Port erst am Ende des Serverstarts** *(in Phase 7 gemessen)*
Zwischen `Popen` und der ersten Antwort auf Port 2305 liegen beim Testserver
**gut zwei Minuten** — BattlEye kommt erst hoch, wenn die Mission geladen und
`Player connect enabled` erreicht ist. Der erste Testlauf wartete 120 s und
schloss daraus fälschlich auf einen Konfigurationsfehler.

Genauer gemessen (Umbau auf Sitzung-je-Kommando): der Port öffnet nach etwa
40 s, **beantwortet einen Login** — und schweigt danach ein bis zwei Minuten,
während der Server weiterlädt. Genau das produzierte bei der gehaltenen Sitzung
die Kette *connected · no answer to the keepalive · disconnected · connected* in
der Konsole. Ein Kommando in diesem Fenster läuft in den Login-Timeout und sagt
„BattlEye may still be starting" — mehr ist ehrlich nicht zu holen, und
Testskripte warten deshalb auf **mehrere** Antworten hintereinander, nicht auf
die erste.

**7.10 Secret-Masking darf die Ausgabe nicht zerstören** *(in Phase 3 durch Tests gefunden)*
Ein kurzes Secret blind durch `***` zu ersetzen beschädigt den Log: mit `ADMIN_PASSWORD=x` wurde aus `Update state (0x61)` ein `Update state (0***61)` — und **dadurch** griff die Fortschritts-Erkennung nicht mehr, weil ihr Muster auf der maskierten Zeile lief. Zwei Konsequenzen: (a) für SteamCMD werden nur Steam-Secrets maskiert, nicht das Panel-Passwort, das dort ohnehin nie auftaucht; (b) Secrets unter vier Zeichen werden übersprungen — sie wären ohnehin nicht schützbar, sondern würden nur ihre Position verraten. Steam-Passwörter haben mindestens 8, Guard-Codes 5 Zeichen.

**7.11 Gunicorn wertet `X-Forwarded-Proto` selbst aus** *(in Phase 2 aufgetreten und behoben)*
Gunicorn setzt über `secure_scheme_headers` in Verbindung mit `forwarded_allow_ips` (Default `127.0.0.1`) `wsgi.url_scheme` auf `https`, sobald ein solcher Header eintrifft — noch bevor die Anwendung ihn sieht. Damit galt ein Request intern als HTTPS, obwohl `TRUSTED_PROXY_HOPS=0` zusicherte, Weiterleitungs-Header zu ignorieren; die SSL-strikte CSRF-Prüfung von Flask-WTF wies den Login daraufhin mit `400` ab. Lösung: `secure_scheme_headers = {}` und `forwarded_allow_ips = ""` in `gunicorn.conf.py`. Weiterleitungs-Header werden damit an genau einer Stelle ausgewertet — in `TrustedProxyFix`, gesteuert von `TRUSTED_PROXY_IPS`.

---

## 8. Implementierungsphasen

| Phase | Inhalt | Definition of Done |
|---|---|---|
| **1 — Image & Boot** ✅ | Dockerfile (python:slim-trixie + steamcmd + lib32gcc-s1), entrypoint.sh, compose, `.env.example` | Container startet, `steamcmd +quit` läuft im Container durch |
| **2 — Panel-Skelett** ✅ | Flask-App, Blueprints, Bootstrap-Base, Login über `ADMIN_USERNAME`/`ADMIN_PASSWORD`, `/healthz`, `TrustedProxyFix` + Cookie-Flags (§6.9) | Panel unter `:8080` erreichbar, Login funktioniert, korrekte Client-IP und Redirects hinter einem Reverse-Proxy |
| **3 — SteamCMD im Panel** ✅ | `SteamCmdService`, Job-System, Setup-Karte, Guard-Dialog, sdk-Symlink-Refresh | Serverdateien lassen sich vollständig aus dem Browser installieren, inkl. Steam Guard |
| **4 — Serversteuerung** ✅ | `ServerManager`, Start/Stop/Restart, Watchdog mit Auto-Restart, Status-API, Serverseite + Dashboard-Karte | Server startet/stoppt aus dem Browser, Status stimmt, Spieler kann verbinden |
| **5 — Logs & Konsole** ✅ | Live-Konsole per SSE auf dem Dashboard, Log-Seite mit Typ-/Dateiauswahl, Auto-Reload, Filter, Download | Serverausgabe läuft live mit, ältere Logdateien sind lesbar und herunterladbar |
| **5a — Seitenstruktur** ✅ | Sechs Seiten (§6.0), Statuskacheln inkl. A2S-Spielerzahl, Steuerung aufs Dashboard, Settings-Tabs | Navigation und Dashboard stehen in ihrer endgültigen Form |
| **6 — Mods & Einstellungen** ✅ | `ModService` (Download/Update/Reinstall/Typ/Reihenfolge/Keys), Settings-Formulare für Startparameter und `serverDZ.cfg` inkl. Backups | Mod per Workshop-ID installierbar und aktiv; Servereinstellungen über das Formular änderbar |
| **7 — RCON & Schedules** ✅ | BattlEye-RCON-Client (§6.6a), Konsoleneingabe, Lock/Unlock, Schedules-Seite (§6.7) | Kommandos aus dem Panel, geplante Aufgaben laufen |
| **8 — Files & Härtung** ✅ | Dateibrowser mit Editor und Up-/Download (§6.7a), Audit-Log (§6.7b), README, Troubleshooting | Dateien im Browser bearbeitbar, reproduzierbarer Build, dokumentiert |
| **9 — Backups** ✅ | `restic` im Image, `BackupService` als Job, Dashboard-Knopf, Scheduler-Aktion `backup`, Seite *Backups* mit Restore/Download/Löschen, Aufbewahrung und Ausschlussliste (§6.7c) | Sicherung aus Browser und Zeitplan; ein zurückgespielter Snapshot startet den Server im Zustand von damals; zehnte Sicherung belegt Bruchteile der ersten |

Streng sequenziell. Phase 3 und 4 tragen das Hauptrisiko (externe Prozesse, Steam-Login) und sollten früh stabil sein.

---

## 9. Test- und Abnahmeplan

- **Unit-Tests** (pytest): Startparameter-Generierung, Pfad-Allowlist, Mod-Namensnormalisierung, Secret-Maskierung im Log, Guard-Marker-Erkennung
- **Integrationstests:** `ServerManager` und `SteamCmdService` gegen Dummy-Skripte statt echter Binaries (Start/Stop/Crash/Guard-Abfrage simulieren)
- **Manuelle Abnahme:**
  - [ ] Container startet **ohne** Serverdateien, Panel sofort erreichbar
  - [ ] Login mit `ADMIN_USERNAME`/`ADMIN_PASSWORD`, unangemeldeter Zugriff auf alle Routen abgewiesen
  - [ ] Installation der Serverdateien aus dem Panel, inkl. Steam-Guard-Abfrage
  - [ ] Live-Ausgabe des SteamCMD-Jobs sichtbar, Passwort darin maskiert
  - [ ] Server startet, Spieler kann verbinden (UDP-Ports korrekt gemappt)
  - [ ] Stop beendet sauber (kein Zombie, keine Datenverluste), Restart mehrfach hintereinander
  - [x] Mod per Workshop-ID installieren → `.bikey` kleingeschrieben in `keys/`, Mod auf `-mod=`
  - [x] Mod deaktivieren → verschwindet aus den Startparametern
  - [x] Typwechsel Client→Server → Key wird wieder entfernt, Mod wandert auf `-serverMod=`
  - [x] `serverDZ.cfg` ändern → Backup vorhanden, `class Missions` und Kommentare unversehrt
  - [x] RCON verbindet sich für das Kommando selbst, `players` antwortet, Lock/Unlock laufen durch
  - [x] Kommando und Antwort erscheinen in der Live-Konsole, nicht nur im eigenen Tab
  - [x] Falsches RCON-Passwort → Fehlermeldung statt Loginschleife
  - [x] Schedule feuert zur eingetragenen Minute, Kette bricht bei der ersten fehlgeschlagenen Aktion ab
  - [x] Files: `..`, absoluter Pfad und Symlink nach außen werden abgewiesen
  - [x] Textdatei bearbeiten → Backup in `backup/files/` mit dem alten Inhalt
  - [x] Binärdatei ist nur herunterladbar, nicht bearbeitbar
  - [x] Upload mit einem Pfad im Dateinamen wird abgewiesen, nichts landet außerhalb
  - [x] Audit-Log zeigt Anmeldung, Fehlversuch, Konsolenkommando, Datei- und Settings-Änderung
  - [ ] Server startet mit installiertem Mod, Spieler kann verbinden
  - [ ] Container-Neustart: Mods, Configs, Settings und Steam-Sentry bleiben erhalten

---

## 10. Stand

Alle neun Phasen sind umgesetzt. Was offenbleibt, sind keine Phasen mehr,
sondern einzelne Punkte:

- **Ein fehlgeschlagener Restore bleibt ein Fehlschlag**, auch wenn nur die
  Rechte einer einzigen Datei nicht gesetzt werden konnten. Der Server wird in
  dem Fall trotzdem wieder gestartet und der betroffene Pfad benannt — aber ob
  der Rest vollständig zurückgespielt wurde, muss man selbst nachsehen.
- **Das RCON-Passwort liegt im Klartext** in `/data/panel/server_settings.json`
  (chmod 600, aber im Bind-Mount). Es muss im Klartext in die
  `beserver_x64.cfg` geschrieben werden — ein Hash wäre dort wertlos. Ein
  leeres Feld schaltet RCON ab.
- **Unit-Tests** (§9) sind bisher Integrationsskripte gegen den laufenden
  Container statt pytest im Build.
