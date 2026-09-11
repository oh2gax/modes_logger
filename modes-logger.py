import json
import os
import socket
import sqlite3
import time
from datetime import datetime
from flask import Flask, request, render_template

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
MIN_UPDATE_MINUTES = 2       # debounce: ignore updates inside this window
FLIGHT_GAP_SECONDS = 3600    # >1 hour means a new flight
SOCKET_TIMEOUT = 10          # seconds to wait for the JSON snapshot

TS_FMT = "%d-%m-%Y %H:%M"    # stored human-readable format

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
               last_epoch INTEGER
           )'''
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
            "SELECT id, LastEpoch FROM aircraft WHERE ICAO24=? ORDER BY LastEpoch DESC LIMIT 1",
            (icao,)
        )
        r = cur.fetchone()
        if r:
            rid, le = r
            cur.execute(
                "INSERT OR REPLACE INTO current_flights (ICAO24, record_id, last_epoch) VALUES (?, ?, ?)",
                (icao, rid, le)
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

def process_aircraft_list(aircraft_list):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    conn_base = sqlite3.connect(SQB_DB_PATH)
    fallback_str, fallback_epoch = now_pair()

    try:
        for ac in aircraft_list:
            icao24 = (ac.get("hex") or "").strip().upper()
            if not icao24:
                continue

            callsign = (ac.get("fli") or "").strip()
            squawk = ac.get("squ") or "0"
            lat = ac.get("lat") if ac.get("lat") is not None else 0
            lon = ac.get("lon") if ac.get("lon") is not None else 0
            altitude = ac.get("alt") if ac.get("alt") is not None else 0
            track = ac.get("trk") if ac.get("trk") is not None else 0
            speed = ac.get("spd") if ac.get("spd") is not None else 0
            registration = (ac.get("reg") or "").strip()
            actype = (ac.get("typ") or "").strip()

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
            cur.execute("SELECT record_id, last_epoch FROM current_flights WHERE ICAO24=?", (icao24,))
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
                    "INSERT OR REPLACE INTO current_flights (ICAO24, record_id, last_epoch) VALUES (?, ?, ?)",
                    (icao24, rid, now_epoch)
                )
                continue

            # There is a current flight pointer
            record_id, last_epoch = ptr[0], (ptr[1] or 0)
            delta = now_epoch - last_epoch

            # Debounce: ignore if inside MIN_UPDATE_MINUTES
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
                    "UPDATE current_flights SET record_id=?, last_epoch=? WHERE ICAO24=?",
                    (new_id, now_epoch, icao24)
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
    for row in rows:
        icao = row[0]
        cur_base.execute("SELECT Registration, ICAOTypeCode FROM Aircraft WHERE ModeS = ?", (icao,))
        base = cur_base.fetchone()
        registration = base[0] if base and base[0] else "Not Found"
        ac_type = base[1] if base and base[1] else "Not Found"
        results.append((icao, registration, ac_type) + row[1:])

    conn_main.close()
    conn_base.close()
    return render_template("results.html", results=results)

# ---------------- Main ----------------
if __name__ == "__main__":
    init_db()
    init_basestation_db()
    from threading import Thread
    Thread(target=fetch_adsb_data, daemon=True).start()
    app.run(debug=True, host="172.26.1.162", port=5000)
