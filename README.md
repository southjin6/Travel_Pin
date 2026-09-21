# Philippine Travel Bucket List & Map Planner

An interactive travel map **scoped to the Philippines** where you drop markers
on places you want to visit, auto-populate country data, and track your
itinerary. Built as a CS50x final project with **Flask**, **SQLite**,
**Leaflet/OpenStreetMap**, and the **REST Countries API**.

> This planner only tracks Philippine destinations. On startup it removes any
> non-Philippine pins. The map is centred and bounded on the Philippines, and
> the back-end rejects a new pin whose country is not the Philippines **or**
> whose coordinates fall outside the country box — the form is client-supplied,
> so both halves are checked with no assumption about where they came from.

## Features

- **Accounts.** Everyone registers, signs in, and sees only their own pins —
  another user's destination is indistinguishable from one that never existed.
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
- An **admin account** manages people, not itineraries: enable or disable an
  account, hand out a replacement password for someone who forgot theirs, delete
  an account, and reassign a pin to another user. Admins cannot read anyone
  else's bucket list.
- **Self-service password change** (`/account`), so an admin never has to know a
  user's password to help them.

## Accounts and how they are secured

No third-party auth library is used — the whole thing is Flask, Werkzeug and the
standard library, because that is what `requirements.txt` can install.

- **Passwords** are hashed with Werkzeug's `generate_password_hash`, which
  defaults to `scrypt:32768:8:1` with a per-password salt. Plaintext passwords
  are never stored or logged. Length is bounded (8–200 characters) because
  scrypt's cost scales with the input, so an unbounded password would be a free
  denial-of-service.
- **Sessions** are Flask's signed cookies; the server stores nothing. The
  `users.session_version` column is what makes revocation possible anyway:
  logging in copies that integer into the cookie, and every request re-checks
  it. Bumping the column — on a password change, a password reset or a
  disable — kills that user's live cookies on their next request, with no
  server-side session store required.
- **Cookies** are `HttpOnly` and `SameSite=Lax`, and `Secure` when
  `SESSION_COOKIE_SECURE=1` is set for a deployment that terminates TLS.
  "Remember me" is off by default, so the cookie dies with the browser;
  enabling it extends the session to 30 days.
- **`SECRET_KEY` is required.** With no key configured the app refuses to start
  rather than signing cookies with a value an attacker could guess; see Setup.
- **CSRF** is a single random token held in the session and compared with
  `secrets.compare_digest` on every `POST`/`PUT`/`PATCH`/`DELETE`. Forms without
  the field get a `400`. `SameSite=Lax` already withholds the cookie on
  cross-site form posts; the token is the in-band check on top of that.
- **Login is throttled** per username *and* per IP (5 failures in 15 minutes →
  15-minute lockout), and registration per IP (10 in an hour). The counters live
  in a `login_attempts` table, so a restart — or the debug reloader, which
  wipes every in-memory structure in this file — does not unlock anything.
- **Failed sign-ins are deliberately indistinguishable.** Unknown username,
  wrong password and disabled account all take one code path and one message, so
  the form cannot be used to list which usernames exist.
- **Ownership is enforced in one place**: `get_owned_destination()` is the only
  function that reads a single pin by id, and it filters on `user_id`. That is
  also why admins get no pin-level access — the one exception in the filter is
  the one that eventually rots.

## Staying inside the free API limits

Every map API behind this app is free but rate-limited, so the back-end treats
its request budget as a real constraint (see `app.py`, "Request budget"):

- **The JSON APIs require login.** `/api/reverse`, `/api/country/<code>` and
  `/api/landmarks` answer `401` to anonymous callers, so a stranger cannot spend
  the shared per-IP request budget that every real user depends on.

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
  app.py              # Flask application + auth + routes + API proxies
  schema.sql          # SQLite tables (users, destinations, login_attempts, api_cache, meta)
  travel.db           # SQLite database (created on first run, not committed)
  .env                # SECRET_KEY + RESTCOUNTRIES_API_KEY etc. — never committed
  .env.example        # the shape of .env, safe to commit (values left blank)
  .gitignore          # keeps travel.db, .env, venv/ and logs out of git
  requirements.txt
  README.md
  templates/
    layout.html       # Base layout (Leaflet CDN, header nav, flashes)
    index.html        # Map, pin form, and destination cards
    login.html        # Sign in (+ "remember me")
    register.html     # Create an account
    account.html      # Change your own password
    admin.html        # Account management: disable, reset, delete, reassign
    error.html        # The 400/413 page a rejected or oversized request lands on
    _form.html        # The csrf_field() macro every form includes
  static/
    script.js         # Leaflet init, click-to-pin, landmark overlay
    styles.css        # Styling
```

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
python app.py
```

`.env` needs two things before the app will start:

1. **`SECRET_KEY`** — a long random string, used to sign session cookies. The
   app exits with an explanation if it is missing. Generate one with:

   ```bash
   python -c "import secrets; print(secrets.token_hex(32))"
   ```

2. **`ADMIN_PASSWORD`** — only on the **first** run, which bootstraps the admin
   account (`ADMIN_USERNAME`, default `admin`). Once that row exists the
   variable is never read again, so you can delete it from `.env`. It must
   satisfy the same length rule as any other password.

Then open http://127.0.0.1:5000, sign in as the admin, and register a personal
account. New accounts start with an empty bucket list — nothing is seeded.

To try the app without touching your real database, point it at a copy:

```bash
TRAVEL_DB=travel_test.db python app.py
```

### Upgrading a database from before accounts

`CREATE TABLE IF NOT EXISTS` creates a table in its current shape and then never
alters it again, so a database written by an earlier version of this code keeps
whichever columns it started with. `meta.schema_version` records which steps the
file has already been through, and `init_db()` runs the ones it has not:

| Version | Step | What it fixes |
| --- | --- | --- |
| 1 | `_migrate_to_v1` | adds `destinations.user_id`, creates the admin account, adopts every ownerless pin |
| 2 | `_migrate_to_v2` | adds `login_attempts.last_failure`, which the login and registration throttle reads |

Each step is idempotent, stamps its own version, and runs inside one transaction,
so an interrupted start is finished on the next run rather than duplicated — and a
failure rolls back instead of stranding pins. A database left at a version this
code does not recognise makes startup raise instead of running against
half-migrated tables.

**Copy `travel.db` somewhere safe first** — this is the one step here that changes
data in place.

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
| SQLite | `users` (`id, username, password_hash, is_admin, is_active, session_version, created_at`), `destinations` (`id, user_id, city, country, country_code, latitude, longitude, visited_status, notes, created_at`), `login_attempts` for the durable lockouts, `meta` for the schema version, and `api_cache` for the map APIs. Every destinations read and write is filtered by `user_id`. |
| External APIs | REST Countries (flags, currencies, languages); OpenStreetMap/Leaflet (map tiles); Nominatim (reverse geocoding). |
| JavaScript | Initializes the Leaflet map, handles click events to capture coordinates, and dynamically places markers. |
| Flask & Jinja | Authentication, CSRF-protected form handling, saving pins/notes to SQLite, and rendering destination summary cards with country flag icons. |

### Routes

- `GET /` — the map planner with the signed-in user's destinations.
- `POST /add` — save a new pin.
- `POST /notes/<id>` — replace a pin's notes (500-char cap).
- `POST /toggle/<id>` — flip `wishlist` ↔ `visited`.
- `POST /delete/<id>` — remove a pin.
- `GET|POST /login` — sign in (`?next=` is honoured for same-site paths only).
- `GET|POST /register` — create an account.
- `POST /logout` — clear the session.
- `GET|POST /account` — change your own password (signs out your other devices).
- `GET /admin` — list accounts with pin counts.
- `POST /admin/toggle/<user_id>` — enable or disable an account (disabling also
  ends its live sessions).
- `POST /admin/reset_password/<user_id>` — set a new password, which signs the
  account out everywhere.
- `POST /admin/delete/<user_id>` — delete an account and its pins.
- `POST /admin/reassign/<dest_id>` — hand a pin you own to another account.
  Every `/admin` route, including the `GET`, needs the admin flag.
- `GET /api/reverse?lat=&lon=` — reverse-geocode coordinates (JSON, login required).
- `GET /api/country/<code>` — REST Countries lookup by 2-letter code (JSON, login required).
- `GET /api/landmarks?lat=&lon=` — nearby landmarks + photos (JSON, login required).

Everything except `/login`, `/register` and the static assets requires a signed-in
session; page routes redirect to `/login` and the JSON routes answer `401`.

> Note: The map requires an internet connection to load OpenStreetMap tiles and
> to reach the REST Countries / Nominatim APIs.
