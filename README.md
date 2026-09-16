# modes_logger

A small, simple MODE-S/ADS-B logger and query tool. It watches a live feed of
aircraft seen by a local receiver and records, per aircraft, the first and
last time each one was seen — not every individual position update — into a
SQLite database. A minimal Flask web UI lets you query that history by
ICAO24 address, registration, callsign, date, or altitude, watch what's
currently in range in near-real-time, and get visually flagged whenever a
tracked aircraft shows up, color-coded Military/Government/Civil.

The project intentionally stays small: one main script, one SQLite database
for flight history, and the industry-standard `BaseStation.sqb` file for
aircraft registration/type lookups so other ADS-B tools can share that data.

At a glance, modes_logger currently gives you:

- **History logging** — first/last-seen tracking per flight, not a firehose
  of every position update (see "How it works" below for why).
- **A search page** (`/`) to look up past flights by ICAO24, registration, or
  callsign (wildcards supported), by exact or partial date, and/or by a
  maximum last-seen altitude.
- **A live view** (`/liveflights`) of everything currently being received,
  auto-refreshing, with sortable columns, aviation-style altitude formatting
  (flight levels, QNH altitudes), and QNH-corrected altitudes below the
  transition altitude using a periodically-polled EFHK METAR.
- **Military/Government/Civil watchlist alerts** — aircraft matched against
  the [plane-alert-db](https://github.com/sdr-enthusiasts/plane-alert-db)
  combined CSV are highlighted on both the Results and Live Flights pages by
  their `#CMPG` classification (military/government/civil), so a tracked
  aircraft stands out immediately instead of scrolling past it unnoticed.
- **Your own watchlist** — a password-protected admin page (`/admin`) for
  adding any aircraft you personally want to keep an eye on (e.g. a friend's
  plane), with its own Military/Government/Civil dropdown so it's colored
  exactly like the official lists, an Enabled tickbox to temporarily pull an
  entry out of flagging without deleting it, and a small BaseStation.sqb
  search tool to help you find an ICAO24/registration to add.
- **A BaseStation.sqb database editor** — also on the admin page, a separate
  section for directly adding, modifying, or deleting an entry in
  `BaseStation.sqb` itself (ICAO24, Registration, ICAO Type), for when you
  already know a plane's details before it's ever come through the live
  feed. Every change is confirmed before it's made, since it edits a shared
  database other ADS-B tools may also read.
- **An optional read-only legacy archive** — drop in an older
  BaseStation-format database covering years before `adsb_data.db` started,
  and a "Include legacy archive" checkbox appears on the Search page to pull
  matching historical flights straight into your results.
- **"Show flagged eastern planes as red"** — an admin-page toggle that
  recolors any *already-flagged* aircraft red when its ICAO24 falls in
  Russia's Mode-S allocation block, regardless of its Mil/Gov/Civ color.
- **A squawk alarm** on the Live Flights page — 7500/7600/7700 gets an
  aircraft's Squawk cell a bold blinking red highlight, independent of
  Mil/Gov/Civ/watchlist status entirely.
- **Light/dark mode** on every page, remembered across visits.

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
- **modes-logger.py** polls that port every `FETCH_INTERVAL` seconds (5s by
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
  - flags any aircraft found in the watchlists (see below) so both the
    Results and Live Flights pages can highlight them.
- The Flask app then serves a simple search form and results table over that
  history, joining in Registration/Aircraft Type from `BaseStation.sqb` at
  query time, plus a Live Flights page showing what's currently being
  received.

Why first/last instead of logging every update: a receiver can emit a
message for the same aircraft every second or two while it's in range, and
almost none of that is interesting after the fact — what you actually want
to know later is "when did this plane first show up, and when did it
leave/go quiet." Keeping only two snapshots per flight (plus a capped
`SeenCount`) keeps `adsb_data.db` small and queries fast even after months
of continuous logging, at the cost of not having a full track log for any
individual flight. If you ever need full per-position tracks, that's a
different kind of tool (e.g. `dump1090`/`tar1090`'s own history, or a
time-series DB) — this project deliberately doesn't try to be that.

## Military/Government/Civil watchlist alerts

Drop `plane-alert-db.csv` — the combined watchlist CSV from the
[plane-alert-db](https://github.com/sdr-enthusiasts/plane-alert-db) project
— into an `alertdb/` folder next to `modes-logger.py`. At startup:

- the master CSV is loaded into memory and matched against incoming
  aircraft by ICAO24. Each entry's `#CMPG` field — the project's own
  standard Military/Government/Civil classification, which also covers
  `Pol` for police, grouped here under Government — decides its highlight
  color: light blue for Military, light green for Government, light amber
  for Civil, on both the Results page and the Live Flights page. A blank or
  unrecognized `#CMPG` value defaults to Civil.
- the older `plane-alert-gov.csv`/`plane-alert-mil.csv` files (from the same
  upstream project) are still supported as an optional fallback and loaded
  right after the master file, for anyone still using them instead of, or
  alongside, `plane-alert-db.csv`. Since the master file is a superset of
  both, any ICAO24 already loaded from it is left alone — this also quietly
  handles the duplicate entries you'd otherwise get between the master file
  and its own older derivatives.
- a fourth, optional file, `tar1090-military.csv`, is loaded last of the
  official sources. It's a military-only extract from the
  [tar1090-db / Mictronics](https://github.com/wiedehopf/tar1090-db)
  aircraft database — the one most dump1090/readsb/tar1090 setups use for
  hex → registration/type lookups — in the same 11-column layout as the
  files above, so it's picked up the same way. It mostly doesn't overlap
  `plane-alert-db.csv` at all, so it's there to add coverage rather than to
  agree or disagree with it; on the rare ICAO24 that does appear in an
  earlier file too, that earlier, more-curated entry wins.
- their Registration/Aircraft Type values are written into `BaseStation.sqb`
  for those ICAO24s, since these manually-curated lists are treated as more
  trustworthy than whatever the live feed itself reports. A later live
  sighting of the same aircraft can still update that entry again afterward
  — the same as any other aircraft — so this is "the CSV wins at startup,"
  not a permanent lock.

All four files are optional: a missing one is logged and skipped rather
than crashing the app. Since the project only reads them at startup (or
when the admin page's Apply button is used), update the CSVs and
restart/Apply to pick up changes.

### Flagging Russian-registered aircraft red

The admin page has a "Show flagged eastern planes as red" tickbox, off by
default. When enabled, any aircraft that's *already flagged* on one of the
watchlists above and whose ICAO24 falls in Russia's allocated Mode-S
address block (`100000`–`1FFFFF` hex — the entire leading hex digit `1`) is
shown with a red background instead of its usual Mil/Gov/Civ color. It
never flags an aircraft that isn't already on a watchlist — it only
overrides the color of ones that already are. The setting is saved to
`admin_settings.json` and applies immediately, without a restart.

## Your own watchlist (admin page)

Besides the curated Military/Government/Civil watchlist above, you can keep
your own personal watchlist through the admin page at `/admin` — for
aircraft that aren't on any official list but that you still want flagged,
like a friend's plane. It's backed by a separate
`alertdb/plane-alert-user.csv` file, using the same 12-column layout as the
upstream CSV plus one extra `Enabled` column (added automatically the first
time the file is read; an older 11-column file already on disk is migrated
in place, with existing rows defaulted to Enabled). In practice you'll
usually only fill in the ICAO24 (and maybe registration/type/CMPG) —
everything else is optional reference detail.

Access is gated behind a simple login: create a `dbauth.txt` file in the
modes_logger folder (next to `modes-logger.py`) with one line,
`username:passwd`. It's read fresh on every login attempt, so changing the
credentials takes effect immediately without a restart, and it's excluded
from the repo via `.gitignore` since it's a plaintext credential file.
Logged out, `/admin` shows only a login form (or a note that login isn't
set up yet) — your watchlist entries, the BaseStation.sqb search tool, and
the "Show flagged eastern planes as red" setting are never loaded or sent
to the page for a logged-out request, not just hidden by the template, so
there's nothing to see even by viewing the page source or requesting it
directly.

Logged in, you get a table of current entries with an Edit button, a Remove
button, and an Enabled tickbox, plus a small add/edit form (ICAO24,
Registration, Type, and a Military/Government/Civil dropdown up front; the
rest of the CSV's fields tucked under an optional "More fields" section).
The dropdown writes straight into the entry's `#CMPG` field using the same
standard values the official CSV uses, so your own entries are colored
exactly the same way as the curated lists (light blue/light green/light
amber) — no separate color scheme to keep track of.

The Enabled tickbox lets you temporarily pull an entry out of flagging
without deleting it — unticking it keeps the row (and everything you filled
in) in `plane-alert-user.csv`, it's just skipped when the watchlists are
loaded. Editing an entry's other fields never silently re-enables or
disables it — its Enabled state only ever changes via the tickbox itself.

Two things are deliberately different from the official watchlist:

- **Entries here never get written into `BaseStation.sqb`.** That sync is
  reserved for the curated upstream lists, since this personal list can
  include entirely ordinary aircraft you just want to watch — it would be
  wrong to treat "planes I'm personally curious about" as an authoritative
  registration/type source the way the vetted watchlists are.
- **Changes don't take effect immediately** — saving, editing, removing, or
  enabling/disabling an entry updates `plane-alert-user.csv` on disk right
  away, but flagging only picks it up once you click the page's **Apply**
  button, which reloads all the watchlists (master/gov/mil/user) and
  re-syncs the official ones into `BaseStation.sqb`, exactly like what
  happens at startup. This is also how a manual edit to the CSV in a text
  editor gets picked up — no restart needed either way.

If you only type in an ICAO24 for a plane that's already been logged before
(e.g. you know its tail number in real life but the app hasn't stored it
yet), the admin table will still show its Registration/Type — looked up
from `BaseStation.sqb` on the fly and shown in italics with a tooltip
explaining where it came from. That lookup is display-only: it's never
written into `plane-alert-user.csv`, so if you later remove the entry to
stop watching that plane, nothing about it is left behind. The admin page
also has its own small search tool for `BaseStation.sqb` itself — search by
ICAO24 or Registration (wildcards supported) to find a plane the app has
already logged, then click **Use** to drop its ICAO24/Registration/Type
straight into the add/edit form above.

That same search tool's results also have **Edit** and **Delete** buttons,
for a separate **Database editor** section at the very bottom of the admin
page (below the Apply button). This is a direct editor for
`BaseStation.sqb`'s `Aircraft` table itself — not the watchlist — with just
three fields: ICAO24, Registration, and ICAO Type, for the case where you
already know an aircraft's registration/type before it's ever come through
the live feed (e.g. you know a plane's tail number but it hasn't flown past
the receiver yet). Typing in a new ICAO24 and saving adds it; typing in one
that's already in the database and saving overwrites it, including clearing
a field back to blank if you empty it out — this is a deliberate human
correction, so unlike the live feed's own writes (which never clear a value
that's already known), it always writes exactly what's in the form.
Clicking **Edit** on a search result scrolls down and pre-fills this form;
clicking **Delete** removes that entry immediately. Every add, modify, or
delete asks for confirmation first, summarizing exactly what's about to be
written or removed — since, unlike the watchlist above, this directly edits
the same shared `BaseStation.sqb` file other ADS-B tools may also be
reading — and takes effect immediately, with no Apply step needed.

Matches from this list are highlighted the same way as the official
lists — light blue/light green/light amber by CMPG — and are included in
"Show only flagged" on both the Results and Live Flights pages alongside
the official watchlist matches.

## Legacy archive (optional)

If you've been logging for a long time and have an older BaseStation-format
database from a previous system, drop it in as `BaseStation-legacy-2023.sqb`
next to `modes-logger.py` and the Search page gains an **"Include legacy
archive (2007–2023)"** checkbox. It's entirely optional — without the file
present, the checkbox simply doesn't appear, and everything else behaves
exactly as it always has.

This is deliberately separate from `adsb_data.db`/`BaseStation.sqb`: it's
treated as a big, read-only reference file rather than something to migrate
data into or keep updated. The app never writes to it — the connection is
opened in SQLite's read-only URI mode as a hard guarantee on top of the fact
that no write statement is ever issued against it anywhere in the code — and
it plays no part in the live poller, registration auto-updates, or anything
else that touches the current data.

Because an archive like this can easily hold a few million historical
flights, it's only ever queried narrowly, never scanned in full:

- The checkbox has to be ticked.
- An ICAO24, Registration, or Callsign value has to be given in Search
  value — the same requirement the main "please narrow your search" guard
  already enforces, just extended to gate the archive too.
- Date has to be one specific year, month, or day (`2013`, `06-2013`, or
  `15-06-2013`) — not left blank, and not combined with an End Date range.
  This matches how the archive tends to actually get used: "let's check that
  plane's movements for year 2013," not an open-ended trawl.
- The requested year has to fall inside the archive's own covered range
  (`LEGACY_ARCHIVE_MIN_YEAR`–`LEGACY_ARCHIVE_MAX_YEAR` below) — a search for,
  say, 2026 skips the archive entirely rather than running a query that
  could only come back empty.

If any of these aren't met, the archive is quietly left out of the search
rather than showing an error — the main database is still searched
normally. When the archive is included, matching flights are merged
straight into the same results table as the current data and re-sorted
together by last-seen time, with no separate section or visual marker,
since having searched a specific year already tells you it's a legacy
result. The underlying file is a standard Kinetic/BaseStation-format
database (the same family `BaseStation.sqb` belongs to, just its own
independent `Aircraft`/`Flights` schema rather than this app's), so if
you've used one before with another ADS-B tool, the data should already
look familiar.

Since this file is expected to be large and is never meant to be shared,
it's excluded from the repo via `.gitignore`'s existing `*.sqb` wildcard —
no extra setup needed there.

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

Open the site in a browser to use the three pages described below.

## Web UI

### Search page (`/`)

A small "Live" button next to the light/dark toggle (top-left corner) jumps
straight to the Live Flights page.

The form itself is a simple, one-filter-per-row layout with a lightly
modernized look — rounded input/select boxes, a label above each field, and
each box sized to what it actually holds (e.g. a narrow Max last altitude
box, the ICAO24/Registration/Callsign dropdown sitting beside its value
box) — kept deliberately compact so it still fits comfortably on a phone
screen. The search form has five fields (Date required, the rest optional
and combinable), plus a sixth that only appears when a legacy archive file
is present (see "Legacy archive" above):

- **Search by** — a dropdown choosing what "Search value" matches against:
  `Registration` (the default), `ICAO24`, or `Callsign`. All three accept
  the `*` wildcard (translated to SQL's `%` under the hood), e.g. `OH-*` for
  registration or `15*` for ICAO24. Registration matching looks the value
  up in `BaseStation.sqb` first, then filters the flight history by the
  ICAO24 hexes found there — since registration itself isn't stored in
  `adsb_data.db`. Registration is the default because the app now resolves
  it for almost all traffic automatically; if a registration search comes
  back with no matches, the Results page suggests trying ICAO24 instead, in
  case that aircraft has been logged but its registration hasn't been
  resolved into `BaseStation.sqb` yet. Callsign matching checks both
  `FirstCallsign` and `LastCallsign`, since a callsign occasionally isn't
  decoded until partway through a flight.
- **Date** — required for every search, matched as a substring against both
  `FirstDateTime` and `LastDateTime` (which are stored as `dd-mm-yyyy
  HH:MM`), so a full date like `12-09-2026` matches that exact day, while a
  partial value like `02-2025` matches any flight with `02-2025` appearing
  anywhere in either timestamp (i.e. any day in February 2025). The small
  calendar icon next to the field is just a convenience — clicking a date in
  the picker fills the text field in `dd-mm-yyyy` format, but the field
  stays a plain, freely-editable text input, so partial searches still work
  afterward. Leaving Date empty is refused with an on-page message instead
  of running, regardless of what else is filled in — Search value, Max last
  altitude, and Show only flagged can only narrow an already-dated search,
  they can't run on their own. A bare year (`2026`) or month+year
  (`09-2026`) is a further step narrower still: on its own — without an
  ICAO24, Registration, or Callsign value to narrow it too — it's also
  rejected with its own on-page message, since it would otherwise scan and
  return a large fraction of the whole flight history at once.
- **End Date** — filling this in switches the search into a real date range
  instead of Date's substring match, with its own calendar-icon picker,
  filled in `dd-mm-yyyy` the same way as Date. Date now acts as the range's
  start day, so it has to be one specific day rather than a bare month or
  year once End Date is set (an on-page message explains if Date is missing,
  isn't a specific day, or falls after End Date). A flight matches if it was
  active at any point during the range — its First DateTime on/before End
  Date, and its Last DateTime on/after Date — so a flight that started
  before the range or ran past it still shows up if it was in the air at
  some point inside it. A range of up to 7 days can be searched on its own —
  e.g. with just "Show only flagged", to check the last few days for
  anything flagged — but a longer range needs an ICAO24, Registration, or
  Callsign value too, same reasoning as the bare year/month protection
  above.
- **Max last altitude** — filters to flights whose `LastAltitude` is
  strictly below the given value (in feet); leave it blank to not filter by
  altitude at all.
- **Show only flagged** — a checkbox that restricts results to ICAO24s
  currently on the official watchlist or your own watchlist (see below),
  combinable with any of the fields above — but still needs Date filled in
  too, same as every other search.
- **Include legacy archive (2007–2023)** — only shown when a legacy archive
  file is present (see "Legacy archive" above). Pulls matching historical
  flights from that archive into the same results, but only when Search
  value and a single-year/month/day Date are also given — see "Legacy
  archive" above for exactly when it does and doesn't kick in.

Date has to be filled in before a search runs — Search value, Max last
altitude, and Show only flagged only narrow an already-dated search, none of
them (alone or combined) can substitute for it. Leaving Date empty is
refused with an on-page message instead of running, the same way the bare
year/month case above is: an unbounded search would otherwise scan the
entire flight history and, for every single matching row, run a separate
lookup query against `BaseStation.sqb`, which on a real history is enough
individual queries to bring the app down rather than just being slow. A
**Clear** button next to Search resets every field back to this default
(blocked) state without running a search — handy for starting over between
different lookups.

### Results page (`/query`)

A "Search" and "Live" button next to the light/dark toggle (top-left corner)
jump back to the Search page and straight to the Live Flights page.

Shows every matching flight as one row, oldest first (by `LastEpoch`),
numbered, with Registration/Aircraft Type joined in from `BaseStation.sqb`
at query time (shown as "Not Found" when that ICAO24 isn't in there yet).
Each row shows the full First/Last pair for callsign, squawk, position,
altitude, track, speed, and timestamp (all times UTC — noted under the
page title) — so you can see both when a flight was first picked up and
its most recent known state in one place. First/Last Track and First/Last
Speed are always rounded to the nearest whole number for display (the
underlying value is unaffected) — most noticeable on flights sourced from
the legacy archive, whose Track/Speed values were originally stored with
more decimal precision than the live feed's. A row is highlighted light blue,
light green, or light amber depending on that ICAO24's Military/Government/
Civil classification (see below), whether it comes from the official
watchlist or your own; a red background instead means it's flagged and
matched the "Show flagged eastern planes as red" setting. The table works reasonably well on a phone too: it scrolls within
its own box (vertically, and horizontally on narrow screens) with the
column header row locked in place and the Registration column frozen on
the left, so you can keep track of which row is which while scrolling
sideways through the rest of the columns. When "Show only flagged" was
checked and no watchlist matches were found, the column headers still show
(so you can see the search ran) with a "No flagged planes found" note
underneath instead of an empty table. A Registration search that finds no
matches shows its own note instead, suggesting a search by ICAO24, since the
aircraft may already be logged under its ICAO24 even if its registration
hasn't been resolved into `BaseStation.sqb` yet.

Click the First DateTime, Last DateTime, First Altitude, or Last Altitude
column headers to sort by that column (click again to reverse direction) —
entirely in the browser, instantly re-ordering the already-loaded table with
no page reload, the same way the Live Flights page's columns work. The
DateTime columns sort by true chronological order rather than the displayed
text; the Altitude columns sort by their real numeric value, with a blank
altitude always sorting to the bottom regardless of direction. The page
still opens in its original default order (oldest Last DateTime first).

Clicking the `#` column header toggles "flagged first": every flagged
aircraft (military/government/civil watchlist, or your own) moves to the
top of the table, itself ordered oldest Last DateTime first, with every
other row following below in that same default order. Click `#` again to
turn it back off; clicking any other sortable column header also cancels it
and switches to a normal single-column sort instead.

### Live Flights page (`/liveflights`)

A matching "Search" button next to its own light/dark toggle jumps back to
the Search page.

A near-real-time view of everything currently being received (Registration,
ICAO24, Callsign, Type, Squawk, Altitude, Selected Alt, Vert Rate, Track,
Speed, Lat/Lon, Distance), independent of the history in `adsb_data.db` —
it's simply whatever was in the most recent poll of the JSON feed, so an
aircraft disappears from this page as soon as one poll cycle no longer
reports it. It refreshes automatically every `LIVE_PAGE_REFRESH_SECONDS`
(5s by default) via a small JavaScript poller (no full page reload), and
uses the same sticky header, frozen Registration column, and alert-row
highlighting as the Results page.

Click the Registration, ICAO24, Altitude, Selected Alt, Vert Rate, or
Distance column headers to sort by that column (click again to reverse
direction); it opens sorted by altitude ascending (lowest first) by
default, and keeps whatever sort you pick across each refresh. Blank
values (e.g. no registration yet, altitude not decoded, position not yet
known) always sort to the bottom regardless of direction.

A "Show only flagged" checkbox sits above the top-left corner of the table
(above the `#` column). Checking it filters the currently-displayed
aircraft down to matches from either the official watchlist or your own
instantly, entirely in the browser — no extra request, and no need to wait
for the next auto-refresh — and the status line switches to "No flagged
aircraft currently in range" if nothing matches.

Since a flagged-only view typically has just a handful of aircraft on
screen, checking it also switches the data rows into a bigger "flight
strip" style: roughly 1.7x the normal font size and doubled row padding,
so the few tracked aircraft are easier to scan at a glance. Only the data
cells change size — the column header row, the sticky Registration column,
and the Mil/Gov/Civ/eastern-red row coloring all stay exactly as they are
in the normal view. A small gap also opens up between the header and the
first flight strip in this view, so a flagged row's color doesn't sit
flush against the header and get mistaken for it at a glance. Unchecking
the box instantly reverts the filter, the row size, and the gap together.

### Squawk alarm

Any aircraft squawking one of the universal ICAO emergency codes — 7500
(hijack), 7600 (radio/communication failure), or 7700 (general emergency) —
gets its Squawk cell highlighted with a strong, saturated red background,
bold white text, and a slow blink (about once a second). This is completely
independent of the Mil/Gov/Civ/watchlist coloring described above — any
aircraft can trigger it, watchlisted or not — and it colors only the Squawk
cell itself, never the whole row, so it stays clearly visible no matter
what row color (or none) it happens to be sitting on. It's deliberately a
much more intense red than the "Show flagged eastern planes as red" row
highlight, specifically so it doesn't get lost if an eastern-flagged
aircraft also has an active squawk alarm. If your browser or OS is set to
reduce motion, the blink is skipped automatically and the highlight just
stays solid instead.

"Show only flagged" also takes a squawk alarm into account: an aircraft
squawking one of these codes is kept visible while that filter is on even
if it isn't on any watchlist at all, so an emergency squawk is never
accidentally hidden by it.

Altitude and Selected Alt are shown in standard aviation shorthand instead
of raw feet: at or above a 5000ft transition altitude (`TRANSITION_ALTITUDE_FT`,
also hardcoded to match in `liveflights.html`) as a flight level, e.g. `F370`
for 37000ft; below it, Altitude shows the exact altitude in feet (e.g.
`2800`) and Selected Alt shows a QNH-style altitude, e.g. `A030` for 3000ft.
Selected Alt is the autopilot/FCU's selected altitude from MODE-S Enhanced
Surveillance (EHS) data — only available for some aircraft, blank
otherwise — and its raw value (often slightly off a round number, e.g.
`36992`) is rounded to the nearest 100ft before display; sorting still uses
the exact underlying value, unaffected by the rounding/formatting. Vert
Rate is the vertical rate in feet per minute, straight from the feed.

Below the transition altitude, Altitude is also corrected for local
barometric pressure rather than shown as raw 1013.25hPa pressure altitude:
`modes-logger.py` polls a QNH value for EFHK (Helsinki-Vantaa) every
`QNH_FETCH_INTERVAL_SECONDS` (10 minutes by default) from NOAA's plain-text
METAR feed for the station, and `/api/liveflights` reports the latest known
value alongside its own staleness. The browser applies the correction —
`corrected = raw + (QNH − 1013.25) × 27 ft/hPa`, the standard-atmosphere
figure rather than the rounded 30ft/hPa pilot shortcut — only below the
transition altitude; Selected Alt is deliberately left uncorrected, since
it's the pilot's own MCP/FCU target rather than a sensor reading, so
correcting it would misrepresent what's actually selected. A small status
line under the page's "last updated" line shows the current state: green
"Altitude correction in use (QNH ### hPa)" once a usable value has been
fetched, or amber "Altitude correction timeout" if a QNH hasn't been
fetched yet, the feed request failed, or the last known value is older than
`QNH_MAX_AGE_SECONDS` (2 hours by default).

Latitude and Longitude are combined into one Lat/Lon column, e.g.
`60.349 25.102`, rounded to 3 decimal places (roughly 100m of precision) —
plenty for a glance at where a plane is, without the extra column width two
separate fields need. Distance shows each aircraft's great-circle distance
from EFHK (Helsinki-Vantaa), in nautical miles, computed in the browser from
its current position and rounded to one decimal; it's blank until a
position is known, same as Lat/Lon. Sorting by Distance (ascending for
closest first, descending for farthest) is handy for quickly spotting
what's nearby versus what's still far out.

### Admin page (`/admin`)

Manage your own watchlist (see "Your own watchlist" above). Not linked from
any other page — open it directly by URL. Logged out, it shows just a login
form and a "Log in to view your watchlist entries" note in place of the
table — your entries, the eastern-red toggle's current on/off state, and
the BaseStation.sqb search tool are withheld entirely until you log in with
the credentials from `dbauth.txt`. Once logged in you can add, edit,
remove, or enable/disable entries, change that toggle, use the
BaseStation.sqb search tool, and use the **Apply** button that reloads all
watchlists for flagging purposes. An entry that's missing Registration/Type
but matches an aircraft already known in `BaseStation.sqb` shows those values
in italics for reference — looked up on the fly, never written into the
CSV itself. A separate **Database editor** section at the bottom of the
page lets you add, modify, or delete an entry in `BaseStation.sqb` directly
(ICAO24, Registration, ICAO Type) — each change confirmed before it's made
and applied immediately, no Apply step needed.

### Light/dark mode

All four pages have a light/dark mode toggle: a small square icon button
in the top-left corner, showing a plain moon in light mode or a plain sun
in dark mode (click to switch). Light mode is the default; your choice is
remembered in the browser (via `localStorage`) and applied instantly on
every page, with no page reload needed and no flash of the wrong theme on
load. The shared styling and toggle logic live in `static/theme.css` and
`static/theme.js`, served by Flask's default static file handling, and
cover the table colors, headers, borders, and alert-row highlighting on
all of them.

## Configuration

All tunable settings live as constants near the top of `modes-logger.py`:

| Constant | Default | Meaning |
|---|---|---|
| `ADSB_JSON_HOST` / `ADSB_JSON_PORT` | `127.0.0.1` / `31009` | Where to read the live aircraft JSON snapshot from |
| `DB_NAME` | `<script dir>/adsb_data.db` | Flight history database — always resolved next to `modes-logger.py`, not the working directory, so it doesn't matter where you launch it from |
| `SQB_DB_PATH` | `<script dir>/BaseStation.sqb` | Shared aircraft registration/type database — same resolution as above |
| `LEGACY_DB_PATH` | `<script dir>/BaseStation-legacy-2023.sqb` | Optional read-only legacy archive (see "Legacy archive" above) — same resolution as above; the Search page's checkbox only appears when this file exists |
| `LEGACY_ARCHIVE_MIN_YEAR` / `LEGACY_ARCHIVE_MAX_YEAR` | `2007` / `2023` | Year range the legacy archive is assumed to cover — a search outside this range skips querying it entirely |
| `FETCH_INTERVAL` | `5` (seconds) | How often to poll the JSON source |
| `MIN_UPDATE_MINUTES` | `2` | Debounce window per aircraft |
| `FLIGHT_GAP_SECONDS` | `3600` | Gap after which a new sighting starts a new flight row |
| `IDENTITY_FILL_WINDOW_SECONDS` | `600` | How long after true first contact a still-blank `FirstCallsign`/`FirstSquawk` can be backfilled |
| `POSITION_FILL_WINDOW_SECONDS` | `60` | How long after true first contact a still-blank `FirstLat`/`FirstLon`/`FirstAltitude`/`FirstTrack`/`FirstSpeed` can be backfilled |
| `ALERTDB_DIR` | `<script dir>/alertdb` | Folder holding the optional watchlist CSVs |
| `ALERT_MASTER_CSV` | `plane-alert-db.csv` in `ALERTDB_DIR` | The combined Mil/Gov/Civil watchlist, from [plane-alert-db](https://github.com/sdr-enthusiasts/plane-alert-db) |
| `ALERT_GOV_CSV` / `ALERT_MIL_CSV` | `plane-alert-gov.csv` / `plane-alert-mil.csv` in `ALERTDB_DIR` | Optional fallback watchlist files, loaded after the master CSV (any ICAO24 already loaded from it is skipped) |
| `ALERT_USER_CSV` | `plane-alert-user.csv` in `ALERTDB_DIR` | Your own watchlist, managed from `/admin` |
| `RUSSIA_ICAO24_MIN` / `RUSSIA_ICAO24_MAX` | `0x100000` / `0x1FFFFF` | Russia's allocated Mode-S address block, used by the "Show flagged eastern planes as red" toggle |
| `SQUAWK_ALARM_CODES` | `{"7500", "7600", "7700"}` | Squawk values that trigger the Live Flights page's squawk alarm highlight |
| `DBAUTH_PATH` | `<script dir>/dbauth.txt` | Admin page login credentials (one line, `username:passwd`) |
| `ADMIN_SETTINGS_PATH` | `<script dir>/admin_settings.json` | Stores the "Show flagged eastern planes as red" toggle state; created automatically the first time it's changed |
| `LIVE_PAGE_REFRESH_SECONDS` | `5` | How often `/liveflights` polls `/api/liveflights` for fresh data |
| `METAR_STATION_ICAO` | `EFHK` | Station whose METAR is polled for QNH altitude correction |
| `QNH_FETCH_INTERVAL_SECONDS` | `600` (10 minutes) | How often the background poller refreshes QNH from NOAA's METAR feed |
| `QNH_FETCH_TIMEOUT_SECONDS` | `10` | Timeout for each QNH fetch attempt |
| `QNH_MAX_AGE_SECONDS` | `7200` (2 hours) | How old the last successfully-fetched QNH can be before altitude correction reports "timeout" |
| `QNH_MIN_HPA` / `QNH_MAX_HPA` | `850` / `1085` | Sanity bounds a parsed QNH must fall within to be accepted |
| `TRANSITION_ALTITUDE_FT` | `5000` | Altitude at/above which no QNH correction is applied (matches the flight-level cutoff used for display, both server- and client-side) |

## Database schema

**`adsb_data.db` → `aircraft` table** — one row per flight/sighting:

- `ICAO24` — aircraft's 24-bit Mode S address (hex)
- `FirstCallsign` / `LastCallsign` — flight callsign as reported by the receiver
- `First*` / `Last*` — Squawk, Lat, Lon, Altitude, Track, Speed, DateTime, Epoch at first and most recent sighting of this flight. The numeric position fields are `NULL` (not `0`) until an actual reading is received, so a genuine `0` (e.g. track due north) is never mistaken for "unknown"; still-`NULL` `First*` fields can be backfilled from a later message — see Configuration above.
- `SeenCount` — capped at 5, just a rough "how many updates" indicator
- `current_flights` — internal pointer table (ICAO24 → active `aircraft` row, plus `first_epoch`, the true first-contact time used for the backfill windows) used to route incoming updates to the right row

**`BaseStation.sqb` → `Aircraft` table** — shared registration/type lookup, keyed by `ModeS` (= ICAO24), auto-populated by modes-logger.py (from the live feed, and at startup/Apply from the official alert-db CSVs — see Military/Government/Civil watchlist alerts above; entries from your own watchlist are deliberately excluded from this — see Your own watchlist above) and readable by any other ADS-B tool that expects this standard file.

## Repo layout

This repo intentionally contains only what modes_logger itself needs to run:

- [`modes-logger.py`](modes-logger.py) — the whole application (poller + Flask web UI)
- [`templates/`](templates/) — the Jinja templates for the web UI (search form, results table, live flights, admin)
- [`static/`](static/) — shared front-end assets (currently just the light/dark theme CSS/JS used by all four pages)
- [`alertdb/`](alertdb/) — the `plane-alert-db.csv` combined watchlist (see Military/Government/Civil watchlist alerts above), the optional `plane-alert-gov.csv`/`plane-alert-mil.csv` fallback files, plus your own `plane-alert-user.csv` (see Your own watchlist above)
- `dbauth.txt` — admin page login credentials (not committed — see `.gitignore`; you create this yourself, see Your own watchlist above)
- `requirements.txt` — the one dependency (Flask)
- `adsb_data.db`, `BaseStation.sqb`, `admin_settings.json` — local data files, created/updated at runtime (not meant to be committed — see `.gitignore`)
- `BaseStation-legacy-2023.sqb` — optional, read-only legacy archive (see "Legacy archive" above); not committed, same `*.sqb` `.gitignore` rule as `BaseStation.sqb`

The JSON relay that feeds port 31009 and any registration-lookup helper
tools are separate, optional projects and intentionally don't live in this
repo.

## Status / roadmap

This project intentionally started as simple as possible: log first/last
seen times, query them back. Planned next steps include GUI improvements to
the web UI. See [CHANGELOG.md](CHANGELOG.md) for the dated history of changes.

## Contributing

This project is developed and maintained by Otso Laakso / OH2GAX. Feedback, observations and bug reports — especially from UI testing and real-world operational use — are welcome.

## Acknowledgments

The Military/Government/Civil watchlist CSV used in `alertdb/` (see
"Military/Government/Civil watchlist alerts" above) comes from the
[plane-alert-db](https://github.com/sdr-enthusiasts/plane-alert-db) project.
Thanks to its maintainers and contributors for compiling and maintaining
that data.

The optional `tar1090-military.csv` source is derived from the
[tar1090-db / Mictronics](https://github.com/wiedehopf/tar1090-db) aircraft
database (maintained at [mictronics.de](https://www.mictronics.de/aircraft-database/)),
the standard hex → registration/type database used by most dump1090/readsb/
tar1090 setups. Thanks to its maintainers and contributors as well.

## License

This project is licensed under the GNU General Public License v3.0. You are free to use, study, modify and distribute this software under the terms of the GPLv3. Any derivative work must also be distributed under the same license.

See the [LICENSE](LICENSE) file for the full license text, or visit https://www.gnu.org/licenses/gpl-3.0.html.
