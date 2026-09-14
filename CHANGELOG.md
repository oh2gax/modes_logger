# Changelog

All notable changes to this project are documented here, grouped by date
(newest first). Multiple changes made on the same day are listed together
under that day's heading.

## 2026-09-14

### Added
- New combined watchlist source: `alertdb/plane-alert-db.csv`, the full [plane-alert-db](https://github.com/sdr-enthusiasts/plane-alert-db) database (17,229 entries), loaded at startup alongside the existing `plane-alert-gov.csv`/`plane-alert-mil.csv` files. The master file is a strict superset of both, so it's loaded first and any ICAO24 already present from it is skipped when the older files are read afterward — avoiding duplicate/conflicting entries between the master file and its own derivatives, with no data lost either way.
- Row highlight color is now driven by each watchlist entry's `#CMPG` field (the plane-alert-db project's own standard Military/Government/Civil classification) instead of which file it came from: Military stays light blue, Government stays light green (now also covering `#CMPG` values of `Pol`, i.e. police), and a new Civil category — light amber, the same color previously used only for your own watchlist — covers everything else, including a blank or unrecognized `#CMPG` value.
- Admin page: the CMPG field moved out of the "More fields" section and now sits as a Military/Government/Civil dropdown right next to Type in the main add/edit form. Saving an entry writes the selected value straight into its `#CMPG` field using the same standard the official CSV uses, so your own watchlist entries get colored exactly like the curated lists.
- Admin page: an Enabled tickbox next to each entry's Remove button lets you pull an entry out of flagging without deleting it — unticking it keeps the row (and all its data) in `plane-alert-user.csv`, just excluded the next time the watchlists are loaded/Applied. Editing an entry's other fields never silently changes its Enabled state.
- Admin page: a new "Show flagged eastern planes as red" tickbox (off by default, persisted to a new `admin_settings.json` file). When enabled, any aircraft that's already flagged on a watchlist and whose ICAO24 falls in Russia's allocated Mode-S address block (`100000`–`1FFFFF` hex) is shown with a red background instead of its usual Mil/Gov/Civ color — it never flags a plane that isn't already on a watchlist, only recolors ones that are.
- Admin page: a small BaseStation.sqb search tool — search the app's own aircraft database by ICAO24 or Registration (wildcards supported) to find Type/Registration for a plane you've already logged, then click **Use** to drop its details straight into the add/edit form. Handy for adding a plane to your own watchlist without having to type its ICAO24 from memory.
- `plane-alert-user.csv` gained a 12th column, `Enabled`; an existing 11-column file is migrated to the new layout automatically the first time it's read, with all existing rows defaulted to Enabled so nothing already on the list gets silently dropped.
- Live Flights page: Latitude and Longitude are now one combined Lat/Lon column (e.g. `60.349 25.102`, rounded to 3 decimal places) instead of two separate columns, freeing up space for a new Distance column showing each aircraft's great-circle distance from EFHK (Helsinki-Vantaa) in nautical miles, computed in the browser from its current position. Distance is sortable like the other numeric columns (closest or farthest first) and is blank, sorting to the bottom, until a position is known — a quick way to see what's nearby versus still far out.

### Changed
- The CSS variable and row class previously specific to "your own watchlist" (`--user-bg`, `.user-alert`) were renamed to `--civ-bg`/`.civ-alert`, reflecting that Civil is now a shared classification used by both the official and personal watchlists rather than something unique to the admin page's entries.
- Results and Live Flights pages: removed the gray row-hover highlight, since it visually clashed with the Mil/Gov/Civ/eastern-red row coloring on flagged aircraft. Rows on both pages are no longer highlighted on mouseover; the admin page's watchlist table is unaffected.

### Fixed
- Admin page: searching BaseStation.sqb no longer leaves the search results scrolled out of view below a long watchlist table — the search form now submits to a `#db-search` anchor, so the page lands back on the search box and its results instead of at the top of the page.

## 2026-09-13

### Added
- Search page: a "Show only flagged" checkbox below Max last altitude restricts results to ICAO24s on the military/government watchlist, combinable with the other search fields. When checked and nothing matches, the column headers still show with a "No flagged planes found" note underneath instead of an empty table.
- Live Flights page: a matching "Show only flagged" checkbox, placed above the top-left corner of the table (above the `#` column). Filtering happens instantly in the browser against the already-loaded data — no extra request, no waiting for the next auto-refresh — and the status line switches to "No flagged aircraft currently in range" if nothing matches.
- Live Flights page: checking "Show only flagged" now also switches the data rows into a bigger "flight strip" style (~1.7x font size, roughly doubled row padding), since a flagged-only view typically shows just a few aircraft and benefits from being easier to scan at a glance. Only the data cells resize — the column header row, sticky Registration column, and military/government row coloring are unchanged. Unchecking the box reverts both the filter and the row size together instantly.
- New password-protected admin page (`/admin`) for building your own personal watchlist, separate from the military/government CSVs — useful for keeping an eye on planes that aren't on any official list, e.g. a friend's aircraft. Backed by a new `alertdb/plane-alert-user.csv` file (same 11-column layout as the upstream CSVs; only ICAO24 is required, everything else is optional). Logged in, you can add, edit, and remove entries from a table on the page; logged out, the table is still visible read-only. Matches are highlighted in light amber on the Results and Live Flights pages and included in "Show only flagged" on both, alongside the existing military/government matches.
- Admin page entries never touch `BaseStation.sqb` the way the military/government CSVs do — this list can include perfectly ordinary aircraft you just want to keep watching, so it's never treated as an authoritative registration/type source.
- Admin page login is a simple username/password check against a new `dbauth.txt` file (one line, `username:passwd`) in the modes_logger folder — read fresh on every login attempt, so editing it takes effect immediately, no restart needed. Missing the file just disables editing (the page still shows a read-only "not configured" notice) rather than breaking anything.
- Admin page has its own "Apply" button that reloads all three watchlists (government/military/user) into memory and re-syncs the government/military ones into `BaseStation.sqb`, the same as what happens at startup — so edits to `plane-alert-user.csv`, whether made through the page or by hand in a text editor, take effect without restarting `modes-logger.py`.
- Admin page table now fills in Registration/Type for display from `BaseStation.sqb` when an entry only specifies an ICAO24 and that aircraft has already been logged before (shown in italics with a tooltip noting where it came from) — handy for the "just add a friend's plane by its ICAO24" case. This is display-only and never written into `plane-alert-user.csv` itself, so removing the entry later leaves nothing behind.
- Search page: a small "Live" button next to the light/dark toggle (top-left corner) jumps straight to the Live Flights page.
- Live Flights page: a matching "Search" button next to its light/dark toggle jumps back to the Search page. Both new buttons share the same flat box style as the theme toggle, just sized to fit a text label instead of an icon, via a new shared `.nav-button` style in `static/theme.css`.

### Fixed
- Results page: the "No flagged planes found" message was inheriting the page title's negative top margin and overlapping the column header row; it now has its own spacing and sits clearly below the table.

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
- README expanded with more explanatory detail throughout (a "how it works"/design-rationale note on why only first/last is logged, and a new "Web UI" section documenting each page's fields and behavior in depth), and the military/government watchlist feature is now mentioned up front in the project's opening description.

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
