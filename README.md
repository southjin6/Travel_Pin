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
  toggle status, delete, or focus the map on a saved pin.

## Project structure

```
Travel/
  app.py              # Flask application + routes + API proxies
  schema.sql          # SQLite table definition (destinations)
  travel.db           # SQLite database (created on first run)
  requirements.txt
  templates/
    layout.html       # Base layout (Leaflet CDN, header/footer, flashes)
    index.html        # Map, pin form, and destination cards
  static/
    script.js         # Leaflet init, click-to-pin, REST Countries lookups
    styles.css        # Styling
```

## Setup

```bash
pip install -r requirements.txt
python app.py
```

Then open http://127.0.0.1:5000 in your browser. The database is created
automatically on first run.

### REST Countries API key

The legacy free `restcountries.com/v3.1` endpoint has been deprecated. This app
uses the current **v5** API at `api.restcountries.com`, which requires a Bearer
token. Sign up for a free key at https://restcountries.com/sign-up and export it
before running:

```bash
# Linux / macOS
export RESTCOUNTRIES_API_KEY="your_key_here"
# Windows (PowerShell)
$env:RESTCOUNTRIES_API_KEY="your_key_here"
```

Without a key the app falls back to the public demo key (`rc_live_demo`), which
returns a sample country object — enough to exercise the integration, but not
live per-country data. Country **flags** always render regardless, because the
cards use the code-based `flagcdn.com` image CDN.

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
- `POST /toggle/<id>` — flip `wishlist` ↔ `visited`.
- `POST /delete/<id>` — remove a pin.
- `GET /api/reverse?lat=&lon=` — reverse-geocode coordinates (JSON).
- `GET /api/country/<code>` — REST Countries lookup by 2-letter code (JSON).

> Note: The map requires an internet connection to load OpenStreetMap tiles and
> to reach the REST Countries / Nominatim APIs.
