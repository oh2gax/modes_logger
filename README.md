# modes_logger

A small, simple MODE-S/ADS-B logger and query tool. It watches a live feed of
aircraft seen by a local receiver and records, per aircraft, the first and
last time each one was seen — not every individual position update — into a
SQLite database. A minimal Flask web UI lets you query that history by
ICAO24 address, date, or altitude.

The project intentionally stays small: one main script, one SQLite database
for flight history, and the industry-standard `BaseStation.sqb` file for
aircraft registration/type lookups so other ADS-B tools can share that data.

## How it works

```
Radarcape receiver ──(JSON)──> JSON stream server ──(TCP :31009)──> modes-logger.py ──> adsb_data.db
      (separate project, run independently — not part of this repo)         │
                                                                              └──> BaseStation.sqb
```

- **Data source**: a [Jetvision Radarcape](https://jetvision.de/) receiver's
  JSON aircraft list, made available on `localhost:31009` by a separate
  relay process that isn't part of this repo (it's the receiver's own
  server, or a small Pi→server JSON forwarder). The protocol is plain TCP:
  connect, receive the latest full JSON snapshot as an array of aircraft
  objects, connection closes. Each aircraft object includes (among other
  fields) `hex` (ICAO24), `fli` (callsign), `lat`/`lon`/`alt`/`spd`/`trk`,
  `squ` (squawk), `reg` (registration), `typ` (ICAO type code), and `uti`
  (the receiver's own capture timestamp, unix epoch seconds).
- **modes-logger.py** polls that port every `FETCH_INTERVAL` seconds (10s by
  default), and for each aircraft in the snapshot:
  - tracks a "current flight" pointer per ICAO24 so repeated sightings update
    the same row instead of creating new ones;
  - starts a **new** row if the aircraft hasn't been seen for over
    `FLIGHT_GAP_SECONDS` (1 hour by default) — treated as a new flight;
  - debounces updates so the same flight's row is touched at most once every
    `MIN_UPDATE_MINUTES` (2 minutes by default);
  - uses the receiver's own per-message timestamp (`uti` in the JSON) rather
    than local poll time, so logged times reflect when the aircraft was
    actually seen. All timestamps are stored and displayed in UTC (the
    results page notes this under its title);
  - automatically keeps `BaseStation.sqb`'s `Aircraft` table (Registration,
    ICAOTypeCode) up to date from the JSON feed's own `reg`/`typ` fields —
    only writing when a value is new or changed, and never overwriting a
    known value with a blank one. Other tools that read `BaseStation.sqb`
    keep working unmodified;
  - backfills still-blank `First*` fields from a later, richer message for
    the same flight, without ever moving `FirstDateTime`/`FirstEpoch` and
    without overwriting a field that's already known. This matters because
    a plane first picked up at long range or low altitude often doesn't have
    everything (callsign, position) decoded yet on the very first message.
    Callsign/squawk get a longer window since they rarely change mid-flight;
    position/altitude/track/speed get a much shorter one since they drift
    continuously — see `IDENTITY_FILL_WINDOW_SECONDS` /
    `POSITION_FILL_WINDOW_SECONDS` below.
- The Flask app then serves a simple search form and results table over that
  history, joining in Registration/Aircraft Type from `BaseStation.sqb` at
  query time.

## Requirements

- Python 3.8+
- [Flask](https://flask.palletsprojects.com/)

```bash
pip install -r requirements.txt
```

A virtual environment isn't strictly required for this project — it's one
script with a single dependency (Flask) and no exotic version pinning, so a
system-wide `pip install flask` works fine. That said, using a venv costs
nothing and keeps this project isolated from your other Python projects:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Running

Make sure whatever exposes the JSON stream on `localhost:31009` (your
Radarcape's own server, or a forwarder process) is already running, then:

```bash
python3 modes-logger.py
```

This creates/migrates `adsb_data.db` and `BaseStation.sqb` as needed, starts
the background poller in a daemon thread, and serves the web UI (by default
on `172.26.1.162:5000` — edit the `app.run(...)` call at the bottom of
`modes-logger.py` to change this).

Open the site and search by ICAO24 (wildcards allowed, e.g. `15*`), by date
(full `DD-MM-YYYY` or a partial match like `02-2025`), and/or by a maximum
last-seen altitude.

The results page also works reasonably well on a phone: the table scrolls
within its own box (vertically, and horizontally on narrow screens) with the
column header row staying locked in place, and the Registration column
frozen on the left, so it stays readable for a quick check on the go, not
just at a desktop.

## Configuration

All tunable settings live as constants near the top of `modes-logger.py`:

| Constant | Default | Meaning |
|---|---|---|
| `ADSB_JSON_HOST` / `ADSB_JSON_PORT` | `127.0.0.1` / `31009` | Where to read the live aircraft JSON snapshot from |
| `DB_NAME` | `<script dir>/adsb_data.db` | Flight history database — always resolved next to `modes-logger.py`, not the working directory, so it doesn't matter where you launch it from |
| `SQB_DB_PATH` | `<script dir>/BaseStation.sqb` | Shared aircraft registration/type database — same resolution as above |
| `FETCH_INTERVAL` | `10` (seconds) | How often to poll the JSON source |
| `MIN_UPDATE_MINUTES` | `2` | Debounce window per aircraft |
| `FLIGHT_GAP_SECONDS` | `3600` | Gap after which a new sighting starts a new flight row |
| `IDENTITY_FILL_WINDOW_SECONDS` | `600` | How long after true first contact a still-blank `FirstCallsign`/`FirstSquawk` can be backfilled |
| `POSITION_FILL_WINDOW_SECONDS` | `60` | How long after true first contact a still-blank `FirstLat`/`FirstLon`/`FirstAltitude`/`FirstTrack`/`FirstSpeed` can be backfilled |

## Database schema

**`adsb_data.db` → `aircraft` table** — one row per flight/sighting:

- `ICAO24` — aircraft's 24-bit Mode S address (hex)
- `FirstCallsign` / `LastCallsign` — flight callsign as reported by the receiver
- `First*` / `Last*` — Squawk, Lat, Lon, Altitude, Track, Speed, DateTime, Epoch at first and most recent sighting of this flight. The numeric position fields are `NULL` (not `0`) until an actual reading is received, so a genuine `0` (e.g. track due north) is never mistaken for "unknown"; still-`NULL` `First*` fields can be backfilled from a later message — see Configuration above.
- `SeenCount` — capped at 5, just a rough "how many updates" indicator
- `current_flights` — internal pointer table (ICAO24 → active `aircraft` row, plus `first_epoch`, the true first-contact time used for the backfill windows) used to route incoming updates to the right row

**`BaseStation.sqb` → `Aircraft` table** — shared registration/type lookup, keyed by `ModeS` (= ICAO24), auto-populated by modes-logger.py and readable by any other ADS-B tool that expects this standard file.

## Repo layout

This repo intentionally contains only what modes_logger itself needs to run:

- [`modes-logger.py`](modes-logger.py) — the whole application (poller + Flask web UI)
- [`templates/`](templates/) — the two Jinja templates for the web UI (search form, results table)
- `requirements.txt` — the one dependency (Flask)
- `adsb_data.db`, `BaseStation.sqb` — local SQLite data files, created/updated at runtime (not meant to be committed — see `.gitignore`)

The JSON relay that feeds port 31009 and any registration-lookup helper
tools are separate, optional projects and intentionally don't live in this
repo.

## Status / roadmap

This project intentionally started as simple as possible: log first/last
seen times, query them back. Planned next steps include GUI improvements to
the web UI. See [CHANGELOG.md](CHANGELOG.md) for the dated history of changes.

## Contributing

This project is developed and maintained by Otso Laakso / OH2GAX. Feedback, observations and bug reports — especially from UI testing and real-world operational use — are welcome.

## License

This project is licensed under the GNU General Public License v3.0. You are free to use, study, modify and distribute this software under the terms of the GPLv3. Any derivative work must also be distributed under the same license.

See the [LICENSE](https://github.com/oh2gax/mode_s_wind/blob/master/LICENSE) file for the full license text, or visit https://www.gnu.org/licenses/gpl-3.0.html.
