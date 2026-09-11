# Changelog

All notable changes to this project are documented here, grouped by date
(newest first). Multiple changes made on the same day are listed together
under that day's heading.

## 2026-09-11

### Added
- Live data source switched from the discontinued colon-separated MODE-S URL to the Jetvision Radarcape JSON feed on `localhost:31009` (plain TCP: connect, read the snapshot until the connection closes, parse as JSON).
- `BaseStation.sqb`'s `Aircraft` table is now auto-populated with Registration/ICAOTypeCode directly from the JSON feed's `reg`/`typ` fields on every poll — only writes when a value is new or changed, and never overwrites a known value with a blank one.
- Flight callsign (`fli` field) is now recorded per sighting as `FirstCallsign`/`LastCallsign` in `adsb_data.db` and shown in the results table. A momentarily blank callsign on a later ping no longer erases a previously known one.
- Search page now has a "Search by" dropdown to search by ICAO24, Registration, or Callsign (previously ICAO24 only); wildcard `*` works in all three modes.
- Added `README.md` (GitHub-style project overview), `requirements.txt`, and `.gitignore`.

### Changed
- Message timestamps now prefer the receiver's own per-aircraft capture time (`uti` in the JSON) over local poll time.
- `DB_NAME` and `SQB_DB_PATH` now resolve relative to the script's own location instead of a hardcoded `/home/bitnami/...` path, so the same code works unmodified wherever it's checked out.
- Search form fields (search, date, max altitude) are now stacked vertically, one per row, instead of inline on one row.
- Results page heading changed from "ADS-B Aircraft Query Results" to "MODE-S & ADS-B Query Results"; search page heading changed from "MODE-S database search" to "MODE-S & ADS-B Database Search".
- Results table font size and cell padding reduced to fit more rows on screen.

### Removed
- `readme.txt` (replaced by this changelog and `README.md`).
- Registration lookup tools no longer needed for normal operation: `update_reg_db.py`, `basestation_autoupdater_v2/`.
- Other unused helper scripts: `combine_databases.py`, `remove_day.py`, `remove_duplicates.py`.
- `adsb_json_data/` — the separate JSON relay project; not part of this repo.

## 2025-04-01

### Fixed
- Duplicate database entry problems.
- Database crash during month/date change.

## 2025-03-01

### Fixed
- Duplicate database entries.

## 2025-02-11

### Added
- Log the same ICAO24 again if more than 2 hours have passed since the last entry for it.
- Partial ICAO24 search (e.g. `15*`).
- Partial date search (e.g. `02-2025`).
- Line numbering on the query results page.
- Aircraft registration and type looked up from a separate `BaseStation.sqb` file.

### Changed
- Database storage optimized to store only first/last values per flight.
- Existing entries' "last" values no longer incorrectly touched when a plane is seen again later.
- First database values not written until 5 messages have been received for an aircraft.
