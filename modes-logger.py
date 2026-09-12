import csv
import json
import os
import socket
import sqlite3
import threading
import time
from datetime import datetime
from flask import Flask, jsonify, request, render_template

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

# Manually-maintained watchlists of military/government aircraft, from
# https://github.com/sdr-enthusiasts/plane-alert-db - matched by ICAO24.
# Loaded once at startup; edit the CSVs and restart to pick up changes.
ALERTDB_DIR = os.path.join(BASE_DIR, "alertdb")
ALERT_GOV_CSV = os.path.join(ALERTDB_DIR, "plane-alert-gov.csv")
ALERT_MIL_CSV = os.path.join(ALERTDB_DIR, "plane-alert-mil.csv")

LIVE_PAGE_REFRESH_SECONDS = 10   # how often liveflights.html polls /api/liveflights

# ---------------- Shared in-memory state ----------------
# ALERT_DB: ICAO24 -> {"category": "mil"/"gov", "registration":, "operator":, "type":}
# populated once at startup by load_alert_db(). Read-only after that, from
# both the poller thread and Flask request threads, so no lock is needed.
ALERT_DB = {}

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

# ---------------- Alert DB (military/government watchlist) ----------------
def load_alert_db():
    """Load the plane-alert-db gov/mil CSVs into the in-memory ALERT_DB dict.
    Each file is optional - a missing or unreadable one is logged and
    skipped rather than crashing the app, since these are manually-supplied
    reference files, not required for modes_logger to run."""
    global ALERT_DB
    alert_db = {}
    for path, category in ((ALERT_GOV_CSV, "gov"), (ALERT_MIL_CSV, "mil")):
        if not os.path.exists(path):
            print(f"Alert DB: {path} not found, skipping ({category})")
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
                    if not icao:
                        continue
                    alert_db[icao] = {
                        "category": category,
                        "registration": (row.get("$Registration") or "").strip(),
                        "operator": (row.get("$Operator") or "").strip(),
                        "type": (row.get("$ICAO Type") or "").strip(),
                    }
                    count += 1
            print(f"Alert DB: loaded {count} {category} entries from {path}")
        except Exception as e:
            print(f"Alert DB: failed to load {path}: {e}")
    ALERT_DB = alert_db

def sync_alert_db_to_basestation():
    """Push alert-db registrations/types into BaseStation.sqb at startup.
    These manually-curated lists are treated as more trustworthy than
    whatever the live feed happens to report, so they're written
    unconditionally via the same upsert the live feed itself uses - the live
    feed can still update an entry again later if it reports something
    different for that ICAO24 (see README for this trade-off)."""
    if not ALERT_DB:
        return
    conn_base = sqlite3.connect(SQB_DB_PATH)
    try:
        for icao24, info in ALERT_DB.items():
            upsert_basestation_registration(conn_base, icao24, info["registration"], info["type"])
        conn_base.commit()
    finally:
        conn_base.close()

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

            # Raw snapshot for the live flights page - independent of the
            # debounce/history logic below, always reflects this exact poll
            snapshot[icao24] = {
                "callsign": callsign, "squawk": squawk,
                "lat": lat, "lon": lon, "altitude": altitude,
                "track": track, "speed": speed,
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

@app.route("/")
def home():
    return render_template("index.html")

@app.route("/query", methods=["GET"])
def query():
    search_field = request.args.get("search_field", "icao24").strip().lower()
    search_value = request.args.get("search_value", "").strip()
    date = request.args.get("date", "").strip()
    max_altitude = request.args.get("max_altitude", "").strip()

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

    # ✅ Oldest first (original behavior)
    sql += " ORDER BY LastEpoch ASC"

    cur_main.execute(sql, params)
    rows = cur_main.fetchall()

    results = []
    alert_lookup = {}  # ICAO24 -> "mil"/"gov", for rows found in the alert watchlists
    for row in rows:
        icao = row[0]
        cur_base.execute("SELECT Registration, ICAOTypeCode FROM Aircraft WHERE ModeS = ?", (icao,))
        base = cur_base.fetchone()
        registration = base[0] if base and base[0] else "Not Found"
        ac_type = base[1] if base and base[1] else "Not Found"
        results.append((icao, registration, ac_type) + row[1:])
        alert = ALERT_DB.get(icao)
        if alert:
            alert_lookup[icao] = alert["category"]

    conn_main.close()
    conn_base.close()
    return render_template("results.html", results=results, alert_lookup=alert_lookup)

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
            "track": ac["track"],
            "speed": ac["speed"],
            "lat": ac["lat"],
            "lon": ac["lon"],
            "alert": alert["category"] if alert else None,
        })

    conn_base.close()
    return jsonify({
        "aircraft": aircraft_out,
        "count": len(aircraft_out),
        "updated_utc": datetime.utcnow().strftime("%H:%M:%S"),
    })

# ---------------- Main ----------------
if __name__ == "__main__":
    init_db()
    init_basestation_db()
    load_alert_db()
    sync_alert_db_to_basestation()
    from threading import Thread
    Thread(target=fetch_adsb_data, daemon=True).start()
    app.run(debug=True, host="172.26.1.162", port=5000)
