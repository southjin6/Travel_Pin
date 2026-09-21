# Philippine Travel Bucket List & Map Planner

An interactive travel map **scoped to the Philippines** where you drop markers
on places you want to visit, auto-populate country data, and track your
itinerary. Built as a CS50x final project with **Flask**, **SQLite**,
**Leaflet/OpenStreetMap**, and the **REST Countries API**.

> This planner only tracks Philippine destinations. On startup it removes any
> non-Philippine pins and seeds a few sample spots (Manila, Cebu City, Puerto
> Princesa, Baguio, Boracay). The map is centred and bounded on the
> Philippines, and the back-end rejects any pin whose country is not the
> Philippines.

## Features

- Click anywhere on the (Philippines-bounded) interactive Leaflet map to drop a pin.
- Coordinates are captured automatically and reverse-geocoded (via
  OpenStreetMap Nominatim) into a city + country.
- Country flag, currency, and language data are fetched from the REST
  Countries API.
- Pins are saved to SQLite along with a status (`wishlist` / `visited`) and
  free-text notes.
- Destination summary cards render alongside country flags, with buttons to
  toggle status, delete, focus the map on a saved pin, or **edit the pin's
  notes** — each card opens an inline editor that saves without re-adding it.
- A **Landmarks** button on each card overlays nearby points of interest
  (attractions, historic sites, nature) from OpenStreetMap's Overpass API, each
  with a photo resolved from Wikipedia / Wikimedia Commons.

## Staying inside the free API limits

Every map API behind this app is free but rate-limited, so the back-end treats
its request budget as a real constraint (see `app.py`, "Request budget"):

- **On-disk cache.** Answers are stored in the `api_cache` SQLite table with a
  TTL per source (landmarks 7 days, geocoding/photos/country 30 days), so
  restarting the app or editing `app.py` under the debug reloader never re-asks
  a question. A lookup that *fails* is not remembered at all, so the next click
  retries it; only a definitive "nothing here" is parked for 15 minutes —
  including a landmark scan that came back empty, which therefore re-checks in
  minutes rather than staying blank for a week. Country data is the exception:
  a rejected key and a genuine "no such country" look identical to this app, so
  a failed lookup parks for 6 hours instead of re-firing an authenticated
  request every few minutes.
- **Pictures are resolved on every read.** The cached landmark list keeps its
  Wikipedia/Commons hints, so a pin whose photos were missing once grows them
  on a later click instead of staying photo-less for the whole cache lifetime.
- **Throttles.** Requests are spaced per host — 1.2s for Nominatim (its usage
  policy allows one per second), 2s for Overpass, 0.3s for Wikimedia.
- **Back-off instead of pushing.** A mirror that replies 429/503/504 is rested
  for 2 minutes rather than re-hit, and the remaining mirrors are tried once.
  A source that fails leaves nothing cached, so a rate-limited minute would
  otherwise re-fire every lookup on every click; instead the failing source is
  paused for 60s (`BUSY_PAUSE_S`) and reads serve whatever is already known.
- **Stale beats an error.** If every mirror is busy, the last cached landmark
  scan is served with a `stale` flag; only a pin that was never fetched returns
  `502` (with a `Retry-After` header, which the page reports as a wait time). A
  scan also gives up after 35s total rather than waiting on a queued mirror
  indefinitely.
- **The browser caches too.** Re-opening a pin's landmark panel is instant and
  costs no request, and country data is looked up once per session. While a
  first scan is still running, clicking another pin says so instead of quietly
  swallowing the click.

To reset the budget deliberately, delete `travel.db` (or just its `api_cache`
rows) and restart.

## Project structure

```
Travel/
  app.py              # Flask application + routes + API proxies
  schema.sql          # SQLite tables (destinations + the api_cache)
  travel.db           # SQLite database (created on first run, not committed)
  .env                # RESTCOUNTRIES_API_KEY etc. — never committed
  .env.example        # the shape of .env, safe to commit (values left blank)
  .gitignore          # keeps travel.db, .env, venv/ and logs out of git
  requirements.txt
  README.md
  templates/
    layout.html       # Base layout (Leaflet CDN, header/footer, flashes)
    index.html        # Map, pin form, and destination cards
  static/
    script.js         # Leaflet init, click-to-pin, landmark overlay
    styles.css        # Styling
```

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env    # then put your REST Countries key in it
python app.py
```

Then open http://127.0.0.1:5000 in your browser. The database is created
automatically on first run.

### REST Countries API key

The legacy free `restcountries.com/v3.1` endpoint has been deprecated. This app
uses the current **v5** API at `api.restcountries.com`, which requires a Bearer
token. Sign up for a free key at https://restcountries.com/sign-up.

The key must not live in the source (the code is committed; a key in it is a
leaked key). Put it in a plain-text `.env` file next to `app.py`, which
`.gitignore` already excludes:

```
RESTCOUNTRIES_API_KEY=your_key_here
```

`app.py` loads it at start-up (`_load_env_file`), and a variable already set in
the shell takes precedence over the file — so on a server or in CI you can
export it instead and skip the file entirely:

```bash
export RESTCOUNTRIES_API_KEY="your_key_here"      # Linux / macOS
$env:RESTCOUNTRIES_API_KEY="your_key_here"        # Windows PowerShell
```

With no key at all the app still runs on the public demo key (`rc_live_demo`),
but REST Countries then answers every country lookup with the same **sample**
object — which is why the status line says "Canada" on a fresh clone. If a key
is ever committed or shared, regenerate it in your dashboard.

## How it works

| Layer | Responsibility |
| --- | --- |
| SQLite | Stores destination pins: `id, city, country, country_code, latitude, longitude, visited_status, notes, created_at`. |
| External APIs | REST Countries (flags, currencies, languages); OpenStreetMap/Leaflet (map tiles); Nominatim (reverse geocoding). |
| JavaScript | Initializes the Leaflet map, handles click events to capture coordinates, and dynamically places markers. |
| Flask & Jinja | Saves pins/notes to SQLite and renders destination summary cards with country flag icons. |

### Routes

- `GET /` — the map planner with all saved destinations.
- `POST /add` — save a new pin.
- `POST /notes/<id>` — replace a pin's notes (500-char cap).
- `POST /toggle/<id>` — flip `wishlist` ↔ `visited`.
- `POST /delete/<id>` — remove a pin.
- `GET /api/reverse?lat=&lon=` — reverse-geocode coordinates (JSON).
- `GET /api/country/<code>` — REST Countries lookup by 2-letter code (JSON).
- `GET /api/landmarks?lat=&lon=` — nearby landmarks + photos (JSON).

> Note: The map requires an internet connection to load OpenStreetMap tiles and
> to reach the REST Countries / Nominatim APIs.
