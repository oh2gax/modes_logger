import csv
import hmac
import json
import os
import secrets
import socket
import sqlite3
import threading
import time
from datetime import datetime
from functools import wraps
from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for

# ---------------- Configuration ----------------
ADSB_JSON_HOST = "127.0.0.1"
ADSB_JSON_PORT = 31009

# Both databases live next to this script, wherever it happens to be deployed
# (no hardcoded per-machine path — resolves the same way on the AWS box,
# a local dev machine, or anywhere else this repo gets checked out).
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(BASE_DIR, "adsb_data.db")
SQB_DB_PATH = os.path.join(BASE_DIR, "BaseStation.sqb")

FETCH_INTERVAL = 10          # seconds between polls
MIN_UPDATE_MINUTES = 2       # debounce: ignore Last*-updates inside this window
FLIGHT_GAP_SECONDS = 3600    # >1 hour means a new flight

# Both windows below allow filling in still-missing First* fields from a
# later message, without ever touching FirstDateTime/FirstEpoch and without
# overwriting a field that's already known — see fill_missing_first_identity
# / fill_missing_first_position. They're split because the two kinds of
# field behave very differently over time:
#   - Callsign/squawk rarely change mid-flight, so a long window is safe —
#     it's really just "keep trying until the receiver finally decodes it."
#   - Position/altitude/track/speed drift continuously, so filling them in
#     from a message too long after first contact would make "First" quietly
#     describe a materially different place/altitude than the true first
#     sighting — so this window is kept short.
IDENTITY_FILL_WINDOW_SECONDS = 600   # callsign + squawk: 10 minutes
POSITION_FILL_WINDOW_SECONDS = 60    # lat/lon/altitude/track/speed: 60 seconds

SOCKET_TIMEOUT = 10          # seconds to wait for the JSON snapshot

TS_FMT = "%d-%m-%Y %H:%M"    # stored human-readable format

# Manually-maintained watchlists of military/government/civil aircraft,
# from https://github.com/sdr-enthusiasts/plane-alert-db - matched by
# ICAO24. plane-alert-db.csv is the combined upstream database and is the
# primary source; plane-alert-gov.csv/plane-alert-mil.csv are kept only as
# a fallback (every row in them is a byte-identical subset of the combined
# file, so they contribute nothing once it's present, but nothing breaks if
# it's ever missing and only the older two files exist). Loaded at startup
# and reloadable on demand from the /admin page's Apply button.
ALERTDB_DIR = os.path.join(BASE_DIR, "alertdb")
ALERT_MASTER_CSV = os.path.join(ALERTDB_DIR, "plane-alert-db.csv")
ALERT_GOV_CSV = os.path.join(ALERTDB_DIR, "plane-alert-gov.csv")
ALERT_MIL_CSV = os.path.join(ALERTDB_DIR, "plane-alert-mil.csv")

# User-maintained watchlist, edited from the /admin page (see below). Same
# columns as the upstream CSVs, plus one of our own, "Enabled" - so it can
# also be opened and hand-edited in a spreadsheet. Only $ICAO is required
# in practice.
ALERT_USER_CSV = os.path.join(ALERTDB_DIR, "plane-alert-user.csv")
USER_CSV_FIELDS = [
    "$ICAO", "$Registration", "$Operator", "$Type", "$ICAO Type", "#CMPG",
    "$Tag 1", "$#Tag 2", "$#Tag 3", "Category", "$#Link", "Enabled",
]

# Maps a row's own #CMPG value (Mil/Gov/Civ/Pol, case-insensitive) to the
# color-classification bucket used for highlighting - this is what decides
# a row's color, not which file/source it was loaded from. Police (Pol) is
# grouped under Government; anything blank or unrecognized defaults to
# Civil, the least alarming bucket, rather than silently becoming
# Military/Government.
CMPG_CATEGORY_MAP = {"mil": "mil", "gov": "gov", "pol": "gov", "civ": "civ"}
DEFAULT_CMPG_CATEGORY = "civ"

# The three choices offered by the admin page's CMPG dropdown, written
# verbatim into plane-alert-user.csv's #CMPG column - matching the exact
# strings the real upstream data uses (confirmed by inspecting
# plane-alert-db.csv), so the file stays compatible with the standard
# format used by plane-alert-db.
ALLOWED_CMPG = ("Mil", "Gov", "Civ")
CMPG_DISPLAY_LABELS = {"Mil": "Military", "Gov": "Government", "Civ": "Civil"}

# ICAO24 addresses allocated to the Russian Federation: the entire block
# starting with hex digit "1" (100000-1FFFFF). Confirmed against two
# independent sources - neighboring former-Soviet states (Ukraine,
# Belarus, Kazakhstan, etc.) have entirely separate blocks elsewhere, so
# this range doesn't accidentally catch them too.
RUSSIA_ICAO24_MIN = 0x100000
RUSSIA_ICAO24_MAX = 0x1FFFFF

# Simple username:password gate for the /admin page. One line,
# "username:passwd", in the modes_logger root. Read fresh on every login
# attempt (never cached), so editing this file takes effect immediately.
DBAUTH_PATH = os.path.join(BASE_DIR, "dbauth.txt")

# Small admin-editable settings that need to persist across restarts (just
# the eastern-red toggle for now). Read fresh on every render, same as
# dbauth.txt, so a change takes effect immediately.
ADMIN_SETTINGS_PATH = os.path.join(BASE_DIR, "admin_settings.json")

LIVE_PAGE_REFRESH_SECONDS = 10   # how often liveflights.html polls /api/liveflights

# ---------------- Shared in-memory state ----------------
# ALERT_DB: ICAO24 -> {"source": "official"/"user", "cmpg": "mil"/"gov"/"civ",
# "registration":, "operator":, "type":}. "source" is the trust axis (does
# this get written into BaseStation.sqb - see sync_alert_db_to_basestation);
# "cmpg" is the color axis (which highlight color a row gets), independent
# of where the entry came from. Populated at startup by load_alert_db(),
# and reloadable on demand from the /admin page's Apply button - so, unlike
# before the admin page existed, this is no longer purely read-only after
# startup. ALERT_DB_LOCK guards the reassignment against the poller thread
# and other request threads reading it mid-reload.
ALERT_DB = {}
ALERT_DB_LOCK = threading.Lock()

# LIVE_SNAPSHOT: ICAO24 -> latest raw fields from the most recent successful
# poll, entirely separate from adsb_data.db - it exists only to answer "what
# is currently being received right now" for the live flights page, and is
# replaced wholesale each poll cycle (not merged), so an aircraft that drops
# out of range disappears from it as soon as one poll no longer reports it.
LIVE_SNAPSHOT = {}
LIVE_SNAPSHOT_LOCK = threading.Lock()

# ---------------- Helpers ----------------
def now_pair():
    now = datetime.now()
    return now.strftime(TS_FMT), int(now.timestamp())

def epoch_pair(epoch):
    dt = datetime.fromtimestamp(epoch)
    return dt.strftime(TS_FMT), int(epoch)

def parse_epoch_or_zero(dt_str: str) -> int:
    if not dt_str:
        return 0
    try:
        return int(datetime.strptime(dt_str, TS_FMT).timestamp())
    except Exception:
        return 0

def table_has_column(conn, table, col):
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    return any(row[1] == col for row in cur.fetchall())

def create_index_if_missing(conn, name, sql):
    cur = conn.cursor()
    try:
        cur.execute(sql)
        conn.commit()
    except sqlite3.OperationalError:
        pass

# ---------------- DB Init & Migration ----------------
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    # Main history table
    cur.execute(
        '''CREATE TABLE IF NOT EXISTS aircraft (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ICAO24 TEXT,
            FirstSquawk TEXT, FirstLat REAL, FirstLon REAL, FirstAltitude INTEGER,
            FirstTrack INTEGER, FirstSpeed INTEGER, FirstDateTime TEXT,
            LastSquawk  TEXT, LastLat  REAL, LastLon  REAL, LastAltitude INTEGER,
            LastTrack   INTEGER, LastSpeed INTEGER, LastDateTime TEXT,
            SeenCount INTEGER DEFAULT 0
        )'''
    )
    conn.commit()

    # Add epoch columns if missing
    added = False
    if not table_has_column(conn, "aircraft", "FirstEpoch"):
        cur.execute("ALTER TABLE aircraft ADD COLUMN FirstEpoch INTEGER DEFAULT 0")
        added = True
    if not table_has_column(conn, "aircraft", "LastEpoch"):
        cur.execute("ALTER TABLE aircraft ADD COLUMN LastEpoch INTEGER DEFAULT 0")
        added = True
    if added:
        conn.commit()

    # Add callsign columns if missing
    added = False
    if not table_has_column(conn, "aircraft", "FirstCallsign"):
        cur.execute("ALTER TABLE aircraft ADD COLUMN FirstCallsign TEXT DEFAULT ''")
        added = True
    if not table_has_column(conn, "aircraft", "LastCallsign"):
        cur.execute("ALTER TABLE aircraft ADD COLUMN LastCallsign TEXT DEFAULT ''")
        added = True
    if added:
        conn.commit()

    # Backfill epochs
    cur.execute("SELECT id, FirstDateTime, LastDateTime, FirstEpoch, LastEpoch FROM aircraft WHERE FirstEpoch=0 OR LastEpoch=0")
    rows = cur.fetchall()
    for rid, fdt, ldt, fe, le in rows:
        fe2 = fe if fe else parse_epoch_or_zero(fdt)
        le2 = le if le else parse_epoch_or_zero(ldt)
        cur.execute("UPDATE aircraft SET FirstEpoch=?, LastEpoch=? WHERE id=?", (fe2, le2, rid))
    conn.commit()

    # Hard duplicate guard
    create_index_if_missing(
        conn,
        "uniq_aircraft_by_epochs",
        "CREATE UNIQUE INDEX IF NOT EXISTS uniq_aircraft_by_epochs ON aircraft (ICAO24, FirstEpoch, LastEpoch)"
    )

    # Pointer table for current active flight per ICAO
    cur.execute(
        '''CREATE TABLE IF NOT EXISTS current_flights (
               ICAO24 TEXT PRIMARY KEY,
               record_id INTEGER,
               last_epoch INTEGER,
               first_epoch INTEGER
           )'''
    )
    conn.commit()

    # Add first_epoch if migrating from an older current_flights table. Unlike
    # last_epoch (which moves on every Last*-write), first_epoch is set once
    # at row creation and never touched again — it's what the fill-window
    # checks measure "time since first contact" against, independently of
    # how many debounced Last*-updates have happened since.
    if not table_has_column(conn, "current_flights", "first_epoch"):
        cur.execute("ALTER TABLE current_flights ADD COLUMN first_epoch INTEGER")
        conn.commit()
    cur.execute(
        '''UPDATE current_flights SET first_epoch = (
               SELECT FirstEpoch FROM aircraft WHERE aircraft.id = current_flights.record_id
           ) WHERE first_epoch IS NULL OR first_epoch = 0'''
    )
    conn.commit()

    # Backfill current_flights from latest per ICAO (by LastEpoch)
    cur.execute("SELECT DISTINCT ICAO24 FROM aircraft")
    icaos = [r[0] for r in cur.fetchall()]
    for icao in icaos:
        cur.execute("SELECT 1 FROM current_flights WHERE ICAO24=?", (icao,))
        if cur.fetchone():
            continue
        cur.execute(
            "SELECT id, FirstEpoch, LastEpoch FROM aircraft WHERE ICAO24=? ORDER BY LastEpoch DESC LIMIT 1",
            (icao,)
        )
        r = cur.fetchone()
        if r:
            rid, fe, le = r
            cur.execute(
                "INSERT OR REPLACE INTO current_flights (ICAO24, record_id, last_epoch, first_epoch) VALUES (?, ?, ?, ?)",
                (icao, rid, le, fe)
            )
    conn.commit()

    cur.execute("PRAGMA journal_mode=WAL;")
    cur.execute("PRAGMA busy_timeout=5000;")
    conn.commit()
    conn.close()

def init_basestation_db():
    """Make sure BaseStation.sqb has the Aircraft table modes-logger writes to,
    and turn on WAL mode so other programs can keep reading it while we write."""
    conn = sqlite3.connect(SQB_DB_PATH)
    cur = conn.cursor()
    cur.execute(
        '''CREATE TABLE IF NOT EXISTS Aircraft (
               AircraftID INTEGER PRIMARY KEY AUTOINCREMENT,
               ModeS TEXT UNIQUE,
               Registration TEXT,
               ICAOTypeCode TEXT
           )'''
    )
    cur.execute("PRAGMA journal_mode=WAL;")
    cur.execute("PRAGMA busy_timeout=5000;")
    conn.commit()
    conn.close()

# ---------------- Alert DB (military/government/civil watchlist) --------
def cmpg_to_category(cmpg_value):
    """Map a raw #CMPG field value (Mil/Gov/Civ/Pol, case-insensitive) to
    the internal color-classification bucket - see CMPG_CATEGORY_MAP."""
    key = (cmpg_value or "").strip().lower()
    return CMPG_CATEGORY_MAP.get(key, DEFAULT_CMPG_CATEGORY)

def is_russian_icao24(icao24_hex):
    """True if the ICAO24 hex address falls in Russia's allocated block
    (see RUSSIA_ICAO24_MIN/MAX above)."""
    try:
        n = int(icao24_hex, 16)
    except (TypeError, ValueError):
        return False
    return RUSSIA_ICAO24_MIN <= n <= RUSSIA_ICAO24_MAX

def effective_alert_category(icao24, cmpg, eastern_red_enabled):
    """The category actually used for row highlighting. Normally just the
    entry's own cmpg bucket (mil/gov/civ), but recolored to "eastern" when
    the eastern-red setting is on and this ICAO24 is Russian-registered -
    regardless of its own CMPG classification. Only ever called for an
    aircraft that's already flagged (has an ALERT_DB entry), so this never
    flags a plane that wasn't already on a watchlist."""
    if eastern_red_enabled and is_russian_icao24(icao24):
        return "eastern"
    return cmpg

def load_alert_db():
    """Load the alert-db watchlists into the in-memory ALERT_DB dict.

    plane-alert-db.csv (the combined upstream database) is the primary
    source - it's a strict superset of the older plane-alert-gov.csv /
    plane-alert-mil.csv files (every row in those two is byte-identical to
    its counterpart here), so those two are only kept as a fallback for
    anything the master file might be missing, and contribute nothing once
    it's present. Whichever official file an ICAO24 is first found in
    wins over a later one, since they're never expected to disagree.

    plane-alert-user.csv is always loaded last and always overwrites, so a
    personal watchlist entry takes precedence over an official one for the
    same ICAO24 if you ever add one that's already on an official list.
    Disabled user entries (Enabled=0) are skipped entirely.

    Each entry's color classification ("cmpg") comes from its own #CMPG
    field, not from which file it was read from - so a Civil entry in the
    master file and a Civil entry you add yourself look identical. Each
    entry also separately records its "source" (official vs user), which
    is the different question of whether it's trustworthy enough to write
    into BaseStation.sqb - see sync_alert_db_to_basestation.

    Every file here is optional - a missing or unreadable one is logged
    and skipped rather than crashing the app."""
    global ALERT_DB
    alert_db = {}

    for path in (ALERT_MASTER_CSV, ALERT_GOV_CSV, ALERT_MIL_CSV):
        if not os.path.exists(path):
            print(f"Alert DB: {path} not found, skipping")
            continue
        try:
            count = 0
            with open(path, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    # A couple of entries in the upstream CSVs are missing a
                    # leading zero (e.g. "10166" instead of "010166"), most
                    # likely lost to spreadsheet auto-formatting at some
                    # point - zero-pad to the full 6 hex digits so they still
                    # match a real ICAO24.
                    icao = (row.get("$ICAO") or "").strip().upper().zfill(6)
                    if not icao or icao in alert_db:
                        continue
                    alert_db[icao] = {
                        "source": "official",
                        "cmpg": cmpg_to_category(row.get("#CMPG")),
                        "registration": (row.get("$Registration") or "").strip(),
                        "operator": (row.get("$Operator") or "").strip(),
                        "type": (row.get("$ICAO Type") or "").strip(),
                    }
                    count += 1
            print(f"Alert DB: loaded {count} entries from {path}")
        except Exception as e:
            print(f"Alert DB: failed to load {path}: {e}")

    try:
        count = 0
        for row in read_user_entries():
            icao = (row.get("$ICAO") or "").strip().upper().zfill(6)
            if not icao or not is_user_entry_enabled(row):
                continue
            alert_db[icao] = {
                "source": "user",
                "cmpg": cmpg_to_category(row.get("#CMPG")),
                "registration": (row.get("$Registration") or "").strip(),
                "operator": (row.get("$Operator") or "").strip(),
                "type": (row.get("$ICAO Type") or "").strip(),
            }
            count += 1
        print(f"Alert DB: loaded {count} user entries from {ALERT_USER_CSV}")
    except Exception as e:
        print(f"Alert DB: failed to load {ALERT_USER_CSV}: {e}")

    with ALERT_DB_LOCK:
        ALERT_DB = alert_db

def sync_alert_db_to_basestation():
    """Push official alert-db registrations/types into BaseStation.sqb at
    startup (and again whenever /admin's Apply button reloads the alert
    DBs). These manually-curated lists are treated as more trustworthy than
    whatever the live feed happens to report, so they're written
    unconditionally via the same upsert the live feed itself uses - the live
    feed can still update an entry again later if it reports something
    different for that ICAO24 (see README for this trade-off).

    User-defined watchlist entries ("source": "user") are deliberately
    skipped here - that list can include perfectly ordinary aircraft the
    user just wants to keep an eye on, so unlike the curated official
    lists, it must never overwrite BaseStation.sqb."""
    if not ALERT_DB:
        return
    conn_base = sqlite3.connect(SQB_DB_PATH)
    try:
        for icao24, info in ALERT_DB.items():
            if info["source"] == "user":
                continue
            upsert_basestation_registration(conn_base, icao24, info["registration"], info["type"])
        conn_base.commit()
    finally:
        conn_base.close()

def read_eastern_red_enabled():
    """Read the "Show flagged eastern planes as red" setting from
    admin_settings.json. Read fresh on every render - never cached - so a
    change from /admin takes effect immediately. Defaults to off, and any
    problem reading the file (missing, corrupt) also defaults to off
    rather than failing the page."""
    try:
        with open(ADMIN_SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return bool(data.get("eastern_red_enabled", False))
    except Exception:
        return False

def write_eastern_red_enabled(value):
    """Persist the eastern-red setting, atomically (temp file + replace)."""
    tmp_path = ADMIN_SETTINGS_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump({"eastern_red_enabled": bool(value)}, f)
    os.replace(tmp_path, ADMIN_SETTINGS_PATH)

# ---------------- Admin: user watchlist + login ----------------
def read_dbauth():
    """Read the admin username/password from dbauth.txt (one line,
    "username:passwd"). Returns (username, password), or None if the file
    is missing or malformed. Read fresh on every login attempt - never
    cached - so editing the file takes effect immediately, no restart."""
    if not os.path.exists(DBAUTH_PATH):
        return None
    try:
        with open(DBAUTH_PATH, "r", encoding="utf-8") as f:
            line = f.readline().strip()
        if ":" not in line:
            return None
        username, password = line.split(":", 1)
        return (username, password)
    except Exception as e:
        print(f"dbauth.txt: failed to read: {e}")
        return None

def check_admin_credentials(username, password):
    creds = read_dbauth()
    if not creds:
        return False
    real_username, real_password = creds
    # compare_digest avoids leaking timing info from the password compare;
    # the username compare can short-circuit normally since only the
    # password half is meant to be secret.
    return username == real_username and hmac.compare_digest(password, real_password)

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin_logged_in"):
            flash("Please log in first.")
            return redirect(url_for("admin"))
        return view(*args, **kwargs)
    return wrapped

def ensure_user_csv():
    """Create alertdb/plane-alert-user.csv with just the header row if it
    doesn't already exist. If it exists with an older/shorter header (e.g.
    from before the Enabled column was added), migrate it in place -
    existing rows keep their data, and any newly-added column defaults to
    a safe value (Enabled defaults to "1", i.e. unchanged behavior)."""
    if not os.path.exists(ALERT_USER_CSV):
        os.makedirs(ALERTDB_DIR, exist_ok=True)
        with open(ALERT_USER_CSV, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(USER_CSV_FIELDS)
        return
    with open(ALERT_USER_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames == USER_CSV_FIELDS:
            return  # already up to date
        rows = list(reader)
    for row in rows:
        row.setdefault("Enabled", "1")
    tmp_path = ALERT_USER_CSV + ".tmp"
    with open(tmp_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=USER_CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in USER_CSV_FIELDS})
    os.replace(tmp_path, ALERT_USER_CSV)

def is_user_entry_enabled(row):
    """True unless the row's Enabled column is explicitly falsy. Missing
    or blank (e.g. a row saved before the Enabled column existed) counts
    as enabled, so nothing silently stops being flagged after upgrading."""
    val = (row.get("Enabled") or "").strip().lower()
    return val not in ("0", "false", "no", "off")

def read_user_entries():
    """Read alertdb/plane-alert-user.csv as a list of dicts, in file order."""
    ensure_user_csv()
    with open(ALERT_USER_CSV, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))

def lookup_basestation(cur_base, icao24):
    """Look up Registration/ICAOTypeCode for one ICAO24 from an existing
    BaseStation.sqb cursor. Returns (registration, icao_type), each None
    if unknown. Used by /admin to show a friend's-plane-style watchlist
    entry's real registration/type even when the user only typed in an
    ICAO24 - the same data the Results/Live Flights pages already show
    for that aircraft independently of the watchlist itself."""
    cur_base.execute("SELECT Registration, ICAOTypeCode FROM Aircraft WHERE ModeS = ?", (icao24,))
    row = cur_base.fetchone()
    if not row:
        return (None, None)
    return (row[0] or None, row[1] or None)

def write_user_entries(entries):
    """Rewrite plane-alert-user.csv from a list of dicts, atomically (write
    to a temp file, then replace it) so a crash mid-write can't corrupt it."""
    ensure_user_csv()
    tmp_path = ALERT_USER_CSV + ".tmp"
    with open(tmp_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=USER_CSV_FIELDS)
        writer.writeheader()
        for row in entries:
            writer.writerow({k: row.get(k, "") for k in USER_CSV_FIELDS})
    os.replace(tmp_path, ALERT_USER_CSV)

# ---------------- Fetch & Process ----------------
def fetch_json_snapshot(host, port, timeout=SOCKET_TIMEOUT):
    """Connect to the ADS-B JSON stream server, read the full snapshot it
    sends before closing the connection, and parse it as a JSON list."""
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    raw = b"".join(chunks).decode("utf-8", errors="replace").strip()
    if not raw:
        return []
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError("Expected a JSON list of aircraft")
    return data

def fetch_adsb_data():
    while True:
        try:
            aircraft_list = fetch_json_snapshot(ADSB_JSON_HOST, ADSB_JSON_PORT)
            process_aircraft_list(aircraft_list)
        except Exception as e:
            print(f"Error fetching ADS-B JSON data: {e}")
        time.sleep(FETCH_INTERVAL)

def upsert_basestation_registration(conn, icao24, registration, actype):
    """Insert/update Registration + ICAOTypeCode in BaseStation.sqb from the
    live JSON feed. Only writes when something is actually new or different,
    and never overwrites an existing value with a blank one from the feed."""
    if not registration and not actype:
        return
    cur = conn.cursor()
    cur.execute("SELECT Registration, ICAOTypeCode FROM Aircraft WHERE ModeS = ?", (icao24,))
    row = cur.fetchone()
    if row is None:
        cur.execute(
            "INSERT INTO Aircraft (ModeS, Registration, ICAOTypeCode) VALUES (?, ?, ?)",
            (icao24, registration, actype)
        )
        return

    cur_reg, cur_type = row
    new_reg = registration if registration else cur_reg
    new_type = actype if actype else cur_type
    if new_reg != cur_reg or new_type != cur_type:
        cur.execute(
            "UPDATE Aircraft SET Registration = ?, ICAOTypeCode = ? WHERE ModeS = ?",
            (new_reg, new_type, icao24)
        )

def fill_missing_first_identity(cur, record_id, callsign, squawk):
    """Fill FirstCallsign/FirstSquawk if still blank, never overwriting an
    already-known value. Safe over a long window since these rarely change
    mid-flight — see IDENTITY_FILL_WINDOW_SECONDS."""
    if not callsign and squawk == "0":
        return  # this message has nothing new to offer either
    cur.execute(
        '''UPDATE aircraft SET
            FirstCallsign = CASE WHEN (FirstCallsign IS NULL OR FirstCallsign = '') AND ? <> '' THEN ? ELSE FirstCallsign END,
            FirstSquawk   = CASE WHEN (FirstSquawk IS NULL OR FirstSquawk = '0') AND ? <> '0' THEN ? ELSE FirstSquawk END
           WHERE id = ?''',
        (callsign, callsign, squawk, squawk, record_id)
    )

def fill_missing_first_position(cur, record_id, lat, lon, altitude, track, speed):
    """Fill First position/altitude/track/speed if still NULL, never
    overwriting an already-known value. Kept to a short window since these
    drift continuously — see POSITION_FILL_WINDOW_SECONDS."""
    if lat is None and lon is None and altitude is None and track is None and speed is None:
        return  # this message has nothing new to offer either
    cur.execute(
        '''UPDATE aircraft SET
            FirstLat      = COALESCE(FirstLat, ?),
            FirstLon      = COALESCE(FirstLon, ?),
            FirstAltitude = COALESCE(FirstAltitude, ?),
            FirstTrack    = COALESCE(FirstTrack, ?),
            FirstSpeed    = COALESCE(FirstSpeed, ?)
           WHERE id = ?''',
        (lat, lon, altitude, track, speed, record_id)
    )

def process_aircraft_list(aircraft_list):
    global LIVE_SNAPSHOT
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    conn_base = sqlite3.connect(SQB_DB_PATH)
    fallback_str, fallback_epoch = now_pair()
    snapshot = {}  # this poll's aircraft, for the live flights page - see LIVE_SNAPSHOT

    try:
        for ac in aircraft_list:
            icao24 = (ac.get("hex") or "").strip().upper()
            if not icao24:
                continue

            callsign = (ac.get("fli") or "").strip()
            squawk = ac.get("squ") or "0"
            # None (SQL NULL) means "not known yet" — kept distinct from a
            # genuine 0 (e.g. track due north, speed 0) so it can safely be
            # backfilled later without ever clobbering a real reading.
            lat = ac.get("lat")
            lon = ac.get("lon")
            altitude = ac.get("alt")
            track = ac.get("trk")
            speed = ac.get("spd")
            registration = (ac.get("reg") or "").strip()
            actype = (ac.get("typ") or "").strip()
            # Vertical rate (ft/min) and MODE-S EHS autopilot-selected altitude
            # ("alts") - only decoded from some messages, so often absent;
            # left as None (blank cell on the live page) when not present.
            vertical_rate = ac.get("vrt")
            selected_altitude = ac.get("alts")

            # Raw snapshot for the live flights page - independent of the
            # debounce/history logic below, always reflects this exact poll
            snapshot[icao24] = {
                "callsign": callsign, "squawk": squawk,
                "lat": lat, "lon": lon, "altitude": altitude,
                "track": track, "speed": speed,
                "vertical_rate": vertical_rate, "selected_altitude": selected_altitude,
                "reg": registration, "typ": actype,
            }

            # Prefer the receiver's own capture time (uti) over poll time
            msg_uti = ac.get("uti")
            if isinstance(msg_uti, (int, float)) and msg_uti > 0:
                now_str, now_epoch = epoch_pair(msg_uti)
            else:
                now_str, now_epoch = fallback_str, fallback_epoch

            # Keep BaseStation.sqb in sync with whatever registration/type
            # the receiver reports for this aircraft
            upsert_basestation_registration(conn_base, icao24, registration, actype)

            # Get or create pointer
            cur.execute("SELECT record_id, last_epoch, first_epoch FROM current_flights WHERE ICAO24=?", (icao24,))
            ptr = cur.fetchone()

            if ptr is None:
                # First time this ICAO — create new record & pointer
                cur.execute(
                    '''INSERT OR IGNORE INTO aircraft (
                        ICAO24, FirstCallsign, FirstSquawk, FirstLat, FirstLon, FirstAltitude,
                        FirstTrack, FirstSpeed, FirstDateTime, LastCallsign, LastSquawk, LastLat, LastLon,
                        LastAltitude, LastTrack, LastSpeed, LastDateTime, SeenCount,
                        FirstEpoch, LastEpoch
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                    (icao24, callsign, squawk, lat, lon, altitude, track, speed, now_str,
                     callsign, squawk, lat, lon, altitude, track, speed, now_str, 1,
                     now_epoch, now_epoch)
                )
                cur.execute("SELECT id FROM aircraft WHERE ICAO24=? AND FirstEpoch=? AND LastEpoch=?", (icao24, now_epoch, now_epoch))
                rid = cur.fetchone()[0]
                cur.execute(
                    "INSERT OR REPLACE INTO current_flights (ICAO24, record_id, last_epoch, first_epoch) VALUES (?, ?, ?, ?)",
                    (icao24, rid, now_epoch, now_epoch)
                )
                continue

            # There is a current flight pointer
            record_id, last_epoch, first_epoch = ptr[0], (ptr[1] or 0), (ptr[2] or ptr[1] or now_epoch)
            delta = now_epoch - last_epoch
            age = now_epoch - first_epoch  # time since this row's true first contact

            # These run independently of the debounce below and never move
            # last_epoch/first_epoch, so they can't interfere with debounce
            # timing or with FirstDateTime/FirstEpoch's own truthfulness.
            if age <= IDENTITY_FILL_WINDOW_SECONDS:
                fill_missing_first_identity(cur, record_id, callsign, squawk)
            if age <= POSITION_FILL_WINDOW_SECONDS:
                fill_missing_first_position(cur, record_id, lat, lon, altitude, track, speed)

            # Debounce: ignore Last*-updates if inside MIN_UPDATE_MINUTES
            if delta < MIN_UPDATE_MINUTES * 60:
                continue

            if delta > FLIGHT_GAP_SECONDS:
                # New flight: insert fresh row, move pointer
                cur.execute(
                    '''INSERT OR IGNORE INTO aircraft (
                        ICAO24, FirstCallsign, FirstSquawk, FirstLat, FirstLon, FirstAltitude,
                        FirstTrack, FirstSpeed, FirstDateTime, LastCallsign, LastSquawk, LastLat, LastLon,
                        LastAltitude, LastTrack, LastSpeed, LastDateTime, SeenCount,
                        FirstEpoch, LastEpoch
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                    (icao24, callsign, squawk, lat, lon, altitude, track, speed, now_str,
                     callsign, squawk, lat, lon, altitude, track, speed, now_str, 0,
                     now_epoch, now_epoch)
                )
                cur.execute("SELECT id FROM aircraft WHERE ICAO24=? AND FirstEpoch=? AND LastEpoch=?", (icao24, now_epoch, now_epoch))
                new_id = cur.fetchone()[0]
                cur.execute(
                    "UPDATE current_flights SET record_id=?, last_epoch=?, first_epoch=? WHERE ICAO24=?",
                    (new_id, now_epoch, now_epoch, icao24)
                )
            else:
                # Same flight: update last* and pointer.
                # LastCallsign only changes when this ping actually has one,
                # so a momentary blank doesn't erase a known callsign.
                cur.execute(
                    '''UPDATE aircraft SET
                        LastCallsign=CASE WHEN ? <> '' THEN ? ELSE LastCallsign END,
                        LastSquawk=?, LastLat=?, LastLon=?, LastAltitude=?,
                        LastTrack=?, LastSpeed=?, LastDateTime=?, LastEpoch=?,
                        SeenCount=CASE WHEN SeenCount < 5 THEN SeenCount+1 ELSE SeenCount END
                       WHERE id=?''',
                    (callsign, callsign, squawk, lat, lon, altitude, track, speed, now_str, now_epoch, record_id)
                )
                cur.execute(
                    "UPDATE current_flights SET last_epoch=? WHERE ICAO24=?",
                    (now_epoch, icao24)
                )

        conn.commit()
        conn_base.commit()
    except Exception as e:
        conn.rollback()
        conn_base.rollback()
        print(f"DB error during process_aircraft_list: {e}")
    finally:
        conn.close()
        conn_base.close()
        # Publish this poll's snapshot for the live flights page, win or
        # lose on the DB writes above - it's independent, in-memory state.
        with LIVE_SNAPSHOT_LOCK:
            LIVE_SNAPSHOT = snapshot

# ---------------- Web ----------------
app = Flask(__name__)
# Random per-run secret key for the /admin login session cookie. It's
# regenerated on every restart, so an admin session doesn't survive one -
# an acceptable trade-off for a single-operator local tool, and simpler
# than adding yet another secret file to manage.
app.secret_key = secrets.token_hex(32)

@app.route("/")
def home():
    return render_template("index.html")

@app.route("/query", methods=["GET"])
def query():
    search_field = request.args.get("search_field", "icao24").strip().lower()
    search_value = request.args.get("search_value", "").strip()
    date = request.args.get("date", "").strip()
    max_altitude = request.args.get("max_altitude", "").strip()
    flagged_only = request.args.get("flagged_only", "").strip()

    conn_main = sqlite3.connect(DB_NAME)
    cur_main = conn_main.cursor()
    conn_base = sqlite3.connect(SQB_DB_PATH)
    cur_base = conn_base.cursor()

    sql = (
        "SELECT ICAO24, FirstCallsign, FirstSquawk, FirstLat, FirstLon, FirstAltitude, "
        "       FirstTrack, FirstSpeed, FirstDateTime, LastCallsign, LastSquawk, LastLat, LastLon, "
        "       LastAltitude, LastTrack, LastSpeed, LastDateTime "
        "FROM aircraft WHERE 1=1"
    )
    params = []

    if search_value and search_field == "registration":
        # Registration lives in BaseStation.sqb, not in aircraft — look up
        # matching ICAO24 hexes there first, then filter the main query.
        cur_base.execute(
            "SELECT ModeS FROM Aircraft WHERE Registration LIKE ?",
            (search_value.replace("*", "%"),)
        )
        matched = [r[0] for r in cur_base.fetchall()] or ["__NONE__"]
        sql += f" AND ICAO24 IN ({','.join('?' for _ in matched)})"
        params.extend(matched)
    elif search_value and search_field == "callsign":
        sql += " AND (FirstCallsign LIKE ? OR LastCallsign LIKE ?)"
        v = search_value.replace("*", "%")
        params.extend([v, v])
    elif search_value:
        # Default: ICAO24
        sql += " AND ICAO24 LIKE ?"
        params.append(search_value.replace("*", "%"))
    if date:
        sql += " AND (FirstDateTime LIKE ? OR LastDateTime LIKE ?)"
        params.extend([f"%{date}%", f"%{date}%"])
    if max_altitude:
        sql += " AND LastAltitude < ?"
        params.append(max_altitude)
    if flagged_only:
        # ALERT_DB lives in memory (loaded from the alert-db CSVs), not in
        # adsb_data.db, so filter by the matching ICAO24s directly - same
        # pattern as the registration search above. An empty ALERT_DB (no
        # CSVs loaded) falls back to a clause that matches nothing, rather
        # than accidentally returning everything.
        alert_icaos = list(ALERT_DB.keys()) or ["__NONE__"]
        sql += f" AND ICAO24 IN ({','.join('?' for _ in alert_icaos)})"
        params.extend(alert_icaos)

    # ✅ Oldest first (original behavior)
    sql += " ORDER BY LastEpoch ASC"

    cur_main.execute(sql, params)
    rows = cur_main.fetchall()

    eastern_enabled = read_eastern_red_enabled()

    results = []
    alert_lookup = {}  # ICAO24 -> "mil"/"gov"/"civ"/"eastern", for flagged rows
    for row in rows:
        icao = row[0]
        cur_base.execute("SELECT Registration, ICAOTypeCode FROM Aircraft WHERE ModeS = ?", (icao,))
        base = cur_base.fetchone()
        registration = base[0] if base and base[0] else "Not Found"
        ac_type = base[1] if base and base[1] else "Not Found"
        results.append((icao, registration, ac_type) + row[1:])
        alert = ALERT_DB.get(icao)
        if alert:
            alert_lookup[icao] = effective_alert_category(icao, alert["cmpg"], eastern_enabled)

    conn_main.close()
    conn_base.close()
    return render_template(
        "results.html", results=results, alert_lookup=alert_lookup,
        flagged_only=bool(flagged_only)
    )

@app.route("/liveflights")
def liveflights():
    return render_template("liveflights.html", refresh_ms=LIVE_PAGE_REFRESH_SECONDS * 1000)

@app.route("/api/liveflights")
def api_liveflights():
    """JSON snapshot of what's currently being received, for liveflights.html
    to poll. Registration/Type fall back to BaseStation.sqb when the live
    feed's own reg/typ is blank for that aircraft, same as the results page."""
    conn_base = sqlite3.connect(SQB_DB_PATH)
    cur_base = conn_base.cursor()
    eastern_enabled = read_eastern_red_enabled()

    with LIVE_SNAPSHOT_LOCK:
        snapshot_items = sorted(LIVE_SNAPSHOT.items())

    aircraft_out = []
    for icao24, ac in snapshot_items:
        reg = ac["reg"]
        typ = ac["typ"]
        if not reg or not typ:
            cur_base.execute("SELECT Registration, ICAOTypeCode FROM Aircraft WHERE ModeS = ?", (icao24,))
            base = cur_base.fetchone()
            if base:
                reg = reg or base[0] or ""
                typ = typ or base[1] or ""
        alert = ALERT_DB.get(icao24)
        aircraft_out.append({
            "icao24": icao24,
            "registration": reg,
            "type": typ,
            "callsign": ac["callsign"],
            "squawk": ac["squawk"],
            "altitude": ac["altitude"],
            "vertical_rate": ac.get("vertical_rate"),
            "selected_altitude": ac.get("selected_altitude"),
            "track": ac["track"],
            "speed": ac["speed"],
            "lat": ac["lat"],
            "lon": ac["lon"],
            "alert": effective_alert_category(icao24, alert["cmpg"], eastern_enabled) if alert else None,
        })

    conn_base.close()
    return jsonify({
        "aircraft": aircraft_out,
        "count": len(aircraft_out),
        "updated_utc": datetime.utcnow().strftime("%H:%M:%S"),
    })

@app.route("/admin")
def admin():
    logged_in = bool(session.get("admin_logged_in"))
    # Everything below - the watchlist entries themselves, the DB search
    # tool, and the eastern-red toggle's current state - is only built and
    # sent to the template when logged in. This isn't just a template-level
    # hiding decision: a logged-out request never even reads
    # plane-alert-user.csv or the eastern-red setting, so a crafted request
    # can't recover them either.
    db_query = ""
    db_results = []
    rows = []
    eastern_red_enabled = None

    if logged_in:
        entries = read_user_entries()
        eastern_red_enabled = read_eastern_red_enabled()
        db_query = request.args.get("db_q", "").strip()

        # Display only - never written back to plane-alert-user.csv. An entry
        # that only specifies an ICAO24 (or leaves Registration/Type blank)
        # gets those fields filled in from BaseStation.sqb when that aircraft
        # has already been logged, e.g. a friend's plane that's flown past
        # before. That way "just watch this ICAO24" still shows something
        # meaningful here, and if the entry is later removed, nothing about
        # it was ever added to the CSV beyond what was actually typed in.
        conn_base = sqlite3.connect(SQB_DB_PATH)
        try:
            cur_base = conn_base.cursor()
            for e in entries:
                reg_lookup = type_lookup = None
                if not e.get("$Registration") or not (e.get("$Type") or e.get("$ICAO Type")):
                    reg_lookup, type_lookup = lookup_basestation(cur_base, e.get("$ICAO", ""))
                raw_cmpg = (e.get("#CMPG") or "").strip()
                rows.append({
                    "raw": e,
                    "display_registration": e.get("$Registration") or reg_lookup or "",
                    "display_type": e.get("$Type") or e.get("$ICAO Type") or type_lookup or "",
                    "reg_from_basestation": bool(reg_lookup) and not e.get("$Registration"),
                    "type_from_basestation": bool(type_lookup) and not (e.get("$Type") or e.get("$ICAO Type")),
                    "cmpg_label": CMPG_DISPLAY_LABELS.get(raw_cmpg, raw_cmpg or "—"),
                    "enabled": is_user_entry_enabled(e),
                })

            if db_query:
                # Same ICAO24-or-Registration, *-wildcard search as the main
                # Search page, just narrower output (ICAO24/Registration/Type)
                # and capped, since this is meant to help fill in the add-entry
                # form above, not be a general query tool.
                pattern = db_query.replace("*", "%")
                cur_base.execute(
                    "SELECT ModeS, Registration, ICAOTypeCode FROM Aircraft "
                    "WHERE ModeS LIKE ? OR Registration LIKE ? LIMIT 50",
                    (pattern, pattern),
                )
                db_results = cur_base.fetchall()
        finally:
            conn_base.close()

    return render_template(
        "admin.html",
        rows=rows,
        logged_in=logged_in,
        auth_configured=os.path.exists(DBAUTH_PATH),
        eastern_red_enabled=eastern_red_enabled,
        allowed_cmpg=ALLOWED_CMPG,
        cmpg_display_labels=CMPG_DISPLAY_LABELS,
        db_query=db_query,
        db_results=db_results,
    )

@app.route("/admin/login", methods=["POST"])
def admin_login():
    username = request.form.get("username", "")
    password = request.form.get("password", "")
    if check_admin_credentials(username, password):
        session["admin_logged_in"] = True
        flash("Logged in.")
    else:
        flash("Incorrect username or password.")
    return redirect(url_for("admin"))

@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    session.pop("admin_logged_in", None)
    flash("Logged out.")
    return redirect(url_for("admin"))

@app.route("/admin/save", methods=["POST"])
@login_required
def admin_save():
    icao = request.form.get("icao", "").strip().upper().zfill(6)
    if not icao or len(icao) != 6:
        flash("ICAO24 is required and must be a valid hex code.")
        return redirect(url_for("admin"))

    cmpg = request.form.get("cmpg", "").strip()
    if cmpg not in ALLOWED_CMPG:
        cmpg = "Civ"

    row = {
        "$ICAO": icao,
        "$Registration": request.form.get("registration", "").strip(),
        "$Operator": request.form.get("operator", "").strip(),
        "$Type": request.form.get("type", "").strip(),
        "$ICAO Type": request.form.get("icao_type", "").strip(),
        "#CMPG": cmpg,
        "$Tag 1": request.form.get("tag1", "").strip(),
        "$#Tag 2": request.form.get("tag2", "").strip(),
        "$#Tag 3": request.form.get("tag3", "").strip(),
        "Category": request.form.get("category", "").strip(),
        "$#Link": request.form.get("link", "").strip(),
        "Enabled": "1",
    }

    entries = read_user_entries()
    for i, existing in enumerate(entries):
        if (existing.get("$ICAO") or "").strip().upper().zfill(6) == icao:
            # Editing an existing entry shouldn't silently re-enable a row
            # the user had deliberately disabled - carry its current
            # Enabled state forward instead of resetting to "1".
            row["Enabled"] = existing.get("Enabled") or "1"
            entries[i] = row
            break
    else:
        entries.append(row)
    write_user_entries(entries)
    flash(f"Saved {icao}. Click Apply to make it active for flagging.")
    return redirect(url_for("admin"))

@app.route("/admin/remove", methods=["POST"])
@login_required
def admin_remove():
    icao = request.form.get("icao", "").strip().upper().zfill(6)
    entries = read_user_entries()
    remaining = [e for e in entries if (e.get("$ICAO") or "").strip().upper().zfill(6) != icao]
    write_user_entries(remaining)
    flash(f"Removed {icao}. Click Apply to make it active for flagging.")
    return redirect(url_for("admin"))

@app.route("/admin/toggle_enabled", methods=["POST"])
@login_required
def admin_toggle_enabled():
    icao = request.form.get("icao", "").strip().upper().zfill(6)
    entries = read_user_entries()
    for e in entries:
        if (e.get("$ICAO") or "").strip().upper().zfill(6) == icao:
            currently_enabled = is_user_entry_enabled(e)
            e["Enabled"] = "0" if currently_enabled else "1"
            write_user_entries(entries)
            flash(f"{icao} {'disabled' if currently_enabled else 'enabled'}. Click Apply to make it active for flagging.")
            break
    return redirect(url_for("admin"))

@app.route("/admin/settings", methods=["POST"])
@login_required
def admin_settings():
    enabled = request.form.get("eastern_red_enabled") == "1"
    write_eastern_red_enabled(enabled)
    flash(f"\"Show flagged eastern planes as red\" {'enabled' if enabled else 'disabled'}.")
    return redirect(url_for("admin"))

@app.route("/admin/reload", methods=["POST"])
@login_required
def admin_reload():
    load_alert_db()
    sync_alert_db_to_basestation()
    flash(f"Reloaded - {len(ALERT_DB)} total watchlist entries active.")
    return redirect(url_for("admin"))

# ---------------- Main ----------------
if __name__ == "__main__":
    init_db()
    init_basestation_db()
    ensure_user_csv()
    load_alert_db()
    sync_alert_db_to_basestation()
    from threading import Thread
    Thread(target=fetch_adsb_data, daemon=True).start()
    app.run(debug=True, host="172.26.1.162", port=5000)
