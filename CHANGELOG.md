# Changelog

All notable changes to this project are documented here, grouped by date
(newest first). Multiple changes made on the same day are listed together
under that day's heading.

## 2026-09-12

### Added
- Military and government aircraft watchlist support, from the [plane-alert-db](https://github.com/sdr-enthusiasts/plane-alert-db) project's `plane-alert-gov.csv`/`plane-alert-mil.csv` (placed in a new `alertdb/` folder), matched by ICAO24. Flagged rows are highlighted light blue (military) or light green (government) on both the Results page and the new Live Flights page.
- At startup, both alert-db CSVs are loaded into memory and used to populate `BaseStation.sqb`'s Registration/ICAOTypeCode for those aircraft, since these manually-curated lists are treated as more trustworthy than the live feed. A later live sighting of the same aircraft can still update that entry again afterward, same as any other aircraft.
- New Live Flights page (`/liveflights`) showing currently-received aircraft (Registration, ICAO24, Callsign, Type, Squawk, Altitude, Track, Speed, Latitude, Longitude) with the same sticky header, frozen Registration column, and alert-row coloring as the Results page. Unlike the Results/Search pages, this page auto-refreshes via JavaScript (polling a new `/api/liveflights` JSON endpoint every `LIVE_PAGE_REFRESH_SECONDS`, 10s by default) rather than a full page reload.
- Live Flights page: Registration, ICAO24, and Altitude column headers are now clickable to sort (click again to reverse direction); the page opens sorted by altitude ascending (lowest first) by default, and the chosen sort is kept across each auto-refresh. Aircraft with an unknown altitude or blank registration always sort to the bottom regardless of direction.
- Search page: a small calendar icon next to the date field opens a native date picker and fills the field in `dd-mm-yyyy` format when a day is picked; the field stays freely editable afterward (including partial searches like `02-2025`) and the box itself is narrower to match the fixed date format.
- Added the missing `LICENSE` file (GNU General Public License v3.0) to the repo, and a README "Acknowledgments" section crediting [plane-alert-db](https://github.com/sdr-enthusiasts/plane-alert-db) as the source of the watchlist CSV data.
- Live Flights page: two new columns, Selected Alt (autopilot/FCU-selected altitude, from MODE-S EHS data when available) and Vert Rate (vertical rate, ft/min), placed right after Altitude in that order; both are sortable like the existing columns.
- Live Flights page: Altitude and Selected Alt now display in aviation shorthand instead of raw feet — a flight level (e.g. `F370`) at or above a 5000ft transition altitude, and below it, exact feet for Altitude (e.g. `2800`) or a QNH-style altitude for Selected Alt (e.g. `A030`). The raw Selected Alt value from the feed is rounded to the nearest 100ft first, since it's often reported slightly off a round number (e.g. `36992` instead of `37000`). Sorting on either column still uses the exact underlying value, unaffected by the display formatting.
- Added `aircraftlist.json`, a sample snapshot of the receiver's raw JSON output, as a reference for the field names/units used by the live feed (e.g. `vrt` = vertical rate, `alts` = selected altitude).
- Light/dark mode for the Query, Results, and Live Flights pages: a small square icon button in the top-left corner (a plain moon in light mode, a plain sun in dark mode) toggles between them; light mode is the default, and the chosen mode is remembered across pages and reloads via the browser's `localStorage`. Table colors, headers, borders, and the military/government alert-row highlighting all adapt to the active theme, and native controls (date picker, scrollbars) follow along too. The shared styling and toggle logic live in two new files, `static/theme.css` and `static/theme.js`, served by Flask's default static file handling and referenced from all three templates.

## 2026-09-11

### Added
- Live data source switched from the discontinued colon-separated MODE-S URL to the Jetvision Radarcape JSON feed on `localhost:31009` (plain TCP: connect, read the snapshot until the connection closes, parse as JSON).
- `BaseStation.sqb`'s `Aircraft` table is now auto-populated with Registration/ICAOTypeCode directly from the JSON feed's `reg`/`typ` fields on every poll — only writes when a value is new or changed, and never overwrites a known value with a blank one.
- Flight callsign (`fli` field) is now recorded per sighting as `FirstCallsign`/`LastCallsign` in `adsb_data.db` and shown in the results table. A momentarily blank callsign on a later ping no longer erases a previously known one.
- Search page now has a "Search by" dropdown to search by ICAO24, Registration, or Callsign (previously ICAO24 only); wildcard `*` works in all three modes.
- Added `README.md` (GitHub-style project overview), `requirements.txt`, and `.gitignore`.
- Still-blank `First*` fields (`FirstCallsign`, `FirstSquawk`, `FirstLat`, `FirstLon`, `FirstAltitude`, `FirstTrack`, `FirstSpeed`) can now be backfilled from a later, richer message for the same flight, without ever moving `FirstDateTime`/`FirstEpoch` and without overwriting a field that's already known. Callsign/squawk use a 10-minute window (`IDENTITY_FILL_WINDOW_SECONDS`), since they rarely change mid-flight; position/altitude/track/speed use a 60-second window (`POSITION_FILL_WINDOW_SECONDS`), since they drift continuously. This improves data completeness for aircraft first picked up at long range or low altitude, where the receiver often can't decode everything on the very first message.
- `current_flights` gained a `first_epoch` column (the true first-contact time for the active flight, independent of debounced updates) to support the backfill windows above; existing rows are migrated automatically on first run.

### Changed
- Message timestamps now prefer the receiver's own per-aircraft capture time (`uti` in the JSON) over local poll time.
- `DB_NAME` and `SQB_DB_PATH` now resolve relative to the script's own location instead of a hardcoded `/home/bitnami/...` path, so the same code works unmodified wherever it's checked out.
- Search form fields (search, date, max altitude) are now stacked vertically, one per row, instead of inline on one row.
- Results page heading changed from "ADS-B Aircraft Query Results" to "MODE-S & ADS-B Query Results"; search page heading changed from "MODE-S database search" to "MODE-S & ADS-B Database Search".
- Results table font size and cell padding reduced to fit more rows on screen.
- Numeric position fields (`Lat`, `Lon`, `Altitude`, `Track`, `Speed`) now default to `NULL` instead of `0` when not yet known, so a genuine `0` (e.g. track due north, ground-level altitude) is never confused with "no data yet." The results page renders unknown values as a blank cell instead of a misleading `0` or the literal text "None".
- Results table now scrolls inside its own box (both vertically and, on narrow screens, horizontally), with the column header row locked to the top of that box at all times — so column labels stay visible on long result lists, and the table is usable on a phone without losing track of which column is which. Same behaviour on desktop and mobile browsers.
- Results table: Registration and ICAO24 columns swapped (Registration now comes right after `#`, since it's available for most sightings), and the Registration column is now frozen in place horizontally, staying visible while swiping/scrolling sideways through the rest of the columns — most useful on a phone.
- Search page date field label changed from "Date (DD-MM-YYYY):" to "Date (dd-mm-yyyy):".
- Results page now shows a small "Times in UTC" note under the title, to make the displayed timestamps' timezone explicit.

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
