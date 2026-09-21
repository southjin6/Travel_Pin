"""Interactive Travel Bucket List & Map Planner.

A small Flask app that stores destination pins in SQLite, renders them as
summary cards (with country flags from the REST Countries API) alongside an
interactive Leaflet/OpenStreetMap map.
"""

import json
import math
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, unquote, urlparse

import requests
from flask import Flask, flash, g, jsonify, redirect, render_template, request, url_for

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_env_file(path=os.path.join(BASE_DIR, ".env")):
    """Seed the environment from a gitignored .env so keys never live in the code.

    A variable that is already set (e.g. exported in the shell, or provided by a
    host on deployment) always wins over the file.
    """
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return
    for line in lines:
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("\"'")
        if sep and key and not key.startswith("#") and value:
            os.environ.setdefault(key, value)


_load_env_file()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-travel-bucket-key")

DATABASE = os.path.join(BASE_DIR, "travel.db")
SCHEMA = os.path.join(BASE_DIR, "schema.sql")

# REST Countries v5 (the legacy v3.1 endpoint has been deprecated). Live data
# requires a personal API key from https://restcountries.com/sign-up; set it via
# the RESTCOUNTRIES_API_KEY environment variable. The public demo key below only
# returns a sample object, but keeps the app runnable out of the box.
REST_COUNTRIES = "https://api.restcountries.com/countries/v5"
RESTCOUNTRIES_API_KEY = os.environ.get("RESTCOUNTRIES_API_KEY", "rc_live_demo")
NOMINATIM = "https://nominatim.openstreetmap.org/reverse"
# Public Overpass mirrors tried in order (the main instance rate-limits bursts).
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
API_HEADERS = {"User-Agent": "TravelBucketList/1.0 (CS50x final project)"}
COUNTRIES_HEADERS = {**API_HEADERS, "Authorization": f"Bearer {RESTCOUNTRIES_API_KEY}"}

# The planner is scoped to the Philippines: only Philippine destinations are
# allowed, and the map opens centred on the country.
PH_COUNTRY_CODE = "ph"
PH_COUNTRY_NAME = "Philippines"
PH_CENTER = [12.8797, 121.7740]
PH_ZOOM = 6

# Sample Philippine destinations seeded on first run (city, lat, lng, notes).
SEED_DESTINATIONS = [
    ("Manila", 14.5995, 120.9842, "Intramuros, Rizal Park, baywalk food crawl"),
    ("Cebu City", 10.3157, 123.8854, "Base for Moal Boal whale sharks & Oslob falls"),
    ("Puerto Princesa", 9.7392, 118.7360, "Palawan: Underground River & island hopping"),
    ("Baguio", 16.4023, 120.5960, "Summer capital — cool climate, night market"),
    ("Boracay", 11.9674, 121.9256, "White Beach, sunset sails, paragliding"),
]


def _is_philippines(country, code):
    """True when a place is in the Philippines (by ISO code or country name)."""
    if (code or "").strip().lower() == PH_COUNTRY_CODE:
        return True
    return PH_COUNTRY_NAME.lower() in (country or "").lower()


# --------------------------------------------------------------------------- #
# Database helpers
# --------------------------------------------------------------------------- #
def get_db():
    """Return a per-request SQLite connection stored on Flask's `g` object."""
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """Create tables, drop non-Philippine pins, and seed sample PH destinations."""
    db = sqlite3.connect(DATABASE)
    with open(SCHEMA, "r", encoding="utf-8") as f:
        db.executescript(f.read())
    # Focus: remove any destination that is not in the Philippines.
    db.execute("DELETE FROM destinations WHERE LOWER(country_code) != ?", (PH_COUNTRY_CODE,))
    # Seed sample Philippine destinations when the list is empty.
    if db.execute("SELECT COUNT(*) FROM destinations").fetchone()[0] == 0:
        db.executemany(
            """INSERT INTO destinations
               (city, country, country_code, latitude, longitude, visited_status, notes)
               VALUES (?, ?, ?, ?, ?, 'wishlist', ?)""",
            [(city, PH_COUNTRY_NAME, PH_COUNTRY_CODE, lat, lon, notes)
             for city, lat, lon, notes in SEED_DESTINATIONS],
        )
    db.commit()
    db.close()


# --------------------------------------------------------------------------- #
# Request budget: on-disk cache + per-host throttles
#
# The map APIs behind this app are free but rate-limited, and exceeding their
# quotas is the one thing that can stop a session half-way through. So every
# upstream answer is cached in SQLite (it survives restarts and debug reloads),
# failures are remembered briefly instead of retried on every click, and
# requests to each host are spaced out to stay inside its stated limits.
# --------------------------------------------------------------------------- #
CACHE_TTL = {
    "landmarks": 7 * 24 * 3600,     # POIs barely move
    "reverse": 30 * 24 * 3600,      # addresses are effectively static
    "country": 30 * 24 * 3600,      # only "ph" is ever looked up, and it never changes
    "photo": 30 * 24 * 3600,
}
MISS_TTL = 15 * 60                  # wait before re-asking a source that failed
BUSY_MESSAGE = "Landmark service is busy — please try again in a moment."


def _cache_row(kind, key):
    """Return (payload, age_seconds, was_miss) for a stored answer, or None."""
    try:
        db = sqlite3.connect(DATABASE, timeout=10)
        try:
            row = db.execute(
                "SELECT payload, is_miss, fetched_at FROM api_cache WHERE kind = ? AND key = ?",
                (kind, key),
            ).fetchone()
        finally:
            db.close()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    try:
        payload = json.loads(row[0])
    except ValueError:
        return None
    return payload, time.time() - row[2], bool(row[1])


def _cache_write(kind, key, payload):
    """Remember an upstream answer; None stores a miss so we back off briefly."""
    try:
        db = sqlite3.connect(DATABASE, timeout=10)
        try:
            db.execute(
                """INSERT OR REPLACE INTO api_cache (kind, key, payload, is_miss, fetched_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (kind, key, json.dumps(payload), int(payload is None), time.time()),
            )
            db.commit()
        finally:
            db.close()
    except sqlite3.Error:
        pass


# A source that is rate-limiting us shouldn't be re-asked on every click, since
# a failed lookup leaves nothing cached. One busy minute therefore pauses that
# source instead of turning into a request storm that keeps us blocked.
BUSY_PAUSE_S = 60
_busy_until = {}
_busy_lock = threading.Lock()


def _in_busy_pause(kind):
    with _busy_lock:
        return time.monotonic() < _busy_until.get(kind, 0.0)


def _note_busy(kind):
    with _busy_lock:
        _busy_until[kind] = time.monotonic() + BUSY_PAUSE_S


def _clear_busy_pause(kind):
    with _busy_lock:
        _busy_until.pop(kind, None)


def cached_lookup(kind, key, fetcher, miss_ttl=MISS_TTL):
    """Return fetcher()'s answer, reusing the cached one while it is fresh.

    A fetcher that raises UpstreamBusy had its request *fail*, which is not the
    same as a definitive "no result", so nothing is remembered and a later read
    tries again instead of being stuck with a cached blank for miss_ttl.
    """
    entry = _cache_row(kind, key)
    if entry:
        payload, age, was_miss = entry
        if age <= (miss_ttl if was_miss else CACHE_TTL[kind]):
            return None if was_miss else payload
    if _in_busy_pause(kind):
        return None
    try:
        answer = fetcher()
    except UpstreamBusy:
        _note_busy(kind)
        return None
    _clear_busy_pause(kind)
    _cache_write(kind, key, answer)
    return answer


class _Throttle:
    """Give each caller a reserved slot so a host never sees two hits at once."""

    def __init__(self, interval_s):
        self._interval = interval_s
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self):
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next)
            self._next = slot + self._interval
        time.sleep(max(0.0, slot - now))


# Nominatim's usage policy asks for at most one request per second; public
# Overpass mirrors tolerate far less than that per IP under load.
NOMINATIM_THROTTLE = _Throttle(1.2)
OVERPASS_THROTTLE = _Throttle(2.0)
WIKIMEDIA_THROTTLE = _Throttle(0.3)


class UpstreamBusy(Exception):
    """Every healthy endpoint is currently rate-limiting us."""


# --------------------------------------------------------------------------- #
# External API helpers (server-side proxies)
# --------------------------------------------------------------------------- #
def _lookup_country(code):
    """Fetch a single country record from the REST Countries v5 API by alpha-2 code.

    A failed lookup (bad key, offline) backs off for hours rather than the usual
    15 minutes: this endpoint carries no up-stream busy signal, so every retry is
    another authenticated request that cannot succeed.
    """
    def fetch():
        try:
            resp = requests.get(
                f"{REST_COUNTRIES}/codes.alpha_2/{code}",
                timeout=8,
                headers=COUNTRIES_HEADERS,
            )
            if resp.status_code != 200:
                return None
            objects = (resp.json().get("data") or {}).get("objects") or []
            return _summarize(objects[0] if objects else None)
        except (requests.RequestException, ValueError):
            return None

    return cached_lookup("country", code, fetch, miss_ttl=6 * 3600)


def _summarize(country):
    """Reduce a raw REST Countries v5 record to the fields the UI needs."""
    if not country:
        return None
    names = country.get("names") or {}
    codes = country.get("codes") or {}
    flag = country.get("flag") or {}
    currencies = [c.get("name") for c in (country.get("currencies") or []) if c.get("name")]
    languages = [l.get("name") for l in (country.get("languages") or []) if l.get("name")]
    capitals = [c.get("name") for c in (country.get("capitals") or []) if c.get("name")]
    return {
        "name": names.get("common"),
        "official_name": names.get("official"),
        "code": (codes.get("alpha_2") or "").lower(),
        "flag": flag.get("url_svg") or flag.get("url_png"),
        "flag_emoji": flag.get("emoji"),
        "currencies": ", ".join(currencies),
        "languages": ", ".join(languages),
        "capital": ", ".join(capitals),
        "region": country.get("region"),
        "population": country.get("population"),
    }


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
NOTES_MAX = 500         # keeps a note to one card-sized block of text


@app.route("/")
def index():
    """Render the map planner with every saved destination."""
    db = get_db()
    rows = db.execute("SELECT * FROM destinations ORDER BY created_at DESC").fetchall()
    destinations = [dict(row) for row in rows]
    total = len(destinations)
    visited = sum(1 for d in destinations if d["visited_status"] == "visited")
    return render_template(
        "index.html",
        destinations=destinations,
        notes_max=NOTES_MAX,
        total=total,
        visited=visited,
        wishlist=total - visited,
    )


@app.route("/add", methods=["POST"])
def add():
    """Save a new pin (coordinates, place name and notes) to SQLite."""
    city = (request.form.get("city") or "").strip()
    country = (request.form.get("country") or "").strip()
    country_code = (request.form.get("country_code") or "").strip().lower()
    notes = (request.form.get("notes") or "").strip()[:NOTES_MAX]
    status = request.form.get("visited_status", "wishlist")
    if status not in ("wishlist", "visited"):
        status = "wishlist"

    try:
        latitude = float(request.form.get("latitude"))
        longitude = float(request.form.get("longitude"))
    except (TypeError, ValueError):
        flash("Could not read the map coordinates. Click the map to drop a pin.", "error")
        return redirect(url_for("index"))

    # This planner only tracks Philippine destinations.
    if not _is_philippines(country, country_code):
        flash("Only destinations in the Philippines can be added.", "error")
        return redirect(url_for("index"))

    country_code = PH_COUNTRY_CODE
    country = PH_COUNTRY_NAME

    if not city:
        city = "Unnamed place"

    db = get_db()
    db.execute(
        """INSERT INTO destinations
           (city, country, country_code, latitude, longitude, visited_status, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (city, country, country_code, latitude, longitude, status, notes),
    )
    db.commit()
    flash(f"Added {city}, {country} to your bucket list.", "success")
    return redirect(url_for("index"))


@app.route("/notes/<int:dest_id>", methods=["POST"])
def save_notes(dest_id):
    """Replace the free-text notes on an existing pin."""
    notes = (request.form.get("notes") or "").strip()[:NOTES_MAX]
    db = get_db()
    row = db.execute("SELECT city FROM destinations WHERE id = ?", (dest_id,)).fetchone()
    if row is None:
        flash("That destination no longer exists.", "error")
        return redirect(url_for("index"))
    db.execute("UPDATE destinations SET notes = ? WHERE id = ?", (notes, dest_id))
    db.commit()
    flash(f"Notes saved for {row['city']}.", "success")
    return redirect(url_for("index"))


@app.route("/toggle/<int:dest_id>", methods=["POST"])
def toggle(dest_id):
    """Flip a destination between 'wishlist' and 'visited'."""
    db = get_db()
    row = db.execute("SELECT visited_status FROM destinations WHERE id = ?", (dest_id,)).fetchone()
    if row is None:
        flash("That destination no longer exists.", "error")
        return redirect(url_for("index"))
    new_status = "visited" if row["visited_status"] == "wishlist" else "wishlist"
    db.execute(
        "UPDATE destinations SET visited_status = ? WHERE id = ?",
        (new_status, dest_id),
    )
    db.commit()
    return redirect(url_for("index"))


@app.route("/delete/<int:dest_id>", methods=["POST"])
def delete(dest_id):
    """Remove a destination pin."""
    db = get_db()
    db.execute("DELETE FROM destinations WHERE id = ?", (dest_id,))
    db.commit()
    flash("Destination removed.", "success")
    return redirect(url_for("index"))


# --------------------------------------------------------------------------- #
# JSON API used by the front-end JavaScript
# --------------------------------------------------------------------------- #
@app.route("/api/reverse")
def api_reverse():
    """Reverse-geocode a lat/lng pair into a city, country and country code.

    Uses OpenStreetMap's Nominatim service so the front-end can auto-populate
    the place name when the user clicks the map. Coordinates are rounded for the
    cache key so clicking around one neighbourhood costs one request.
    """
    try:
        lat = float(request.args.get("lat"))
        lon = float(request.args.get("lon"))
    except (TypeError, ValueError):
        return jsonify({"error": "lat and lon query params are required"}), 400

    def fetch():
        NOMINATIM_THROTTLE.wait()
        try:
            resp = requests.get(
                NOMINATIM,
                params={"lat": lat, "lon": lon, "format": "json", "zoom": 10,
                        "addressdetails": 1},
                timeout=8,
                headers=API_HEADERS,
            )
            data = resp.json() if resp.status_code == 200 else {}
        except (requests.RequestException, ValueError):
            return None
        address = data.get("address", {}) if isinstance(data, dict) else {}
        if not address:
            return None
        city = (
            address.get("city")
            or address.get("town")
            or address.get("village")
            or address.get("municipality")
            or address.get("county")
            or address.get("state")
            or "Unnamed place"
        )
        return {"city": city,
                "country": address.get("country") or "Unknown country",
                "country_code": (address.get("country_code") or "").lower()}

    place = cached_lookup("reverse", f"{round(lat, 3)},{round(lon, 3)}", fetch)
    if place is None:
        return jsonify({"city": "", "country": "", "country_code": "",
                        "lat": lat, "lon": lon, "lookup_failed": True})
    return jsonify({**place, "lat": lat, "lon": lon})


@app.route("/api/country/<code>")
def api_country(code):
    """Return flag, currency and language data for a 2-letter country code."""
    code = (code or "").strip().lower()
    if not code:
        return jsonify({"error": "country code required"}), 400
    summary = _lookup_country(code)
    if not summary:
        return jsonify({"error": "country not found"}), 404
    return jsonify(summary)


# --------------------------------------------------------------------------- #
# Nearby landmarks (Overpass / OpenStreetMap)
# --------------------------------------------------------------------------- #
# Tag filters used to find points of interest around a pin.
LANDMARK_FILTERS = [
    ("tourism", "attraction|viewpoint|museum|artwork|gallery|zoo|aquarium|theme_park"),
    ("historic", None),
    ("leisure", "park|nature_reserve|garden|stadium|marina"),
    ("natural", "peak|beach|waterfall|cave_entrance|rock"),
    ("amenity", "place_of_worship|castle"),
]


def _haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in kilometres between two coordinate pairs."""
    r_lat1, r_lon1, r_lat2, r_lon2 = map(math.radians, (lat1, lon1, lat2, lon2))
    dlat = r_lat2 - r_lat1
    dlon = r_lon2 - r_lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(r_lat1) * math.cos(r_lat2) * math.sin(dlon / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(a))


# --------------------------------------------------------------------------- #
# Landmark photos (Wikimedia Commons / Wikipedia)
# --------------------------------------------------------------------------- #
COMMONS_THUMB = "https://commons.wikimedia.org/wiki/Special:FilePath"
WIKIMEDIA_API = "https://{project}/w/api.php"
THUMB_WIDTH = 400
# Commons search is one request per landmark, so cap it to keep a click cheap.
# The answers cache for a month, so this is paid once per pin, not per view.
COMMONS_SEARCH_LIMIT = 12


def _commons_ref(value):
    """Classify an OSM image/wikimedia_commons tag as ('file'|'category', title)."""
    value = (value or "").strip()
    if not value:
        return None, None
    if "/wiki/" in value:
        value = value.split("/wiki/", 1)[1]
    value = unquote(value).replace("_", " ").strip()
    if value.lower().startswith("category:"):
        return "category", value
    return "file", value if value.lower().startswith("file:") else "File:" + value


def _thumb_url(file_title, width=THUMB_WIDTH):
    """Build a scaled Commons thumbnail URL from a 'File:…' title."""
    name = file_title.split(":", 1)[1] if ":" in file_title else file_title
    return f"{COMMONS_THUMB}/{quote(name.replace(' ', '_'))}?width={width}"


def _wiki_ref(value):
    """Parse an OSM wikipedia tag ('en:Fort Santiago' or a URL) into (lang, title)."""
    value = (value or "").strip()
    if not value:
        return None
    if "://" in value:
        parts = urlparse(value)
        lang = parts.netloc.split(".")[0]
        title = unquote(parts.path.rsplit("/wiki/", 1)[-1]) if "/wiki/" in parts.path else ""
    elif ":" in value:
        lang, title = value.split(":", 1)
    else:
        return None
    title = unquote(title).replace("_", " ").strip()
    return [lang or "en", title] if title else None


def _wiki_thumb_key(lang, title):
    return f"wiki:{lang}:{title}"


def _wikipedia_thumbs(refs):
    """Map article titles to photo URLs, in one batched request per wiki.

    Each title is cached on its own, so re-reading a landmark list costs nothing
    and a title that failed is retried later instead of staying photo-less.
    """
    thumbs = {}
    todo = {}
    for lang, title in refs:
        entry = _cache_row("photo", _wiki_thumb_key(lang, title))
        if entry and entry[1] <= (MISS_TTL if entry[2] else CACHE_TTL["photo"]):
            if entry[0]:
                thumbs[title] = entry[0]
        else:
            todo.setdefault(lang, set()).add(title)

    for lang, titles in todo.items():
        if _in_busy_pause("photo"):
            return thumbs
        WIKIMEDIA_THROTTLE.wait()
        try:
            resp = requests.get(
                WIKIMEDIA_API.format(project=f"{lang}.wikipedia.org"),
                params={
                    "action": "query", "format": "json", "redirects": 1,
                    "prop": "pageimages", "pithumbsize": THUMB_WIDTH,
                    "titles": "|".join(sorted(titles)),
                },
                timeout=10, headers=API_HEADERS,
            )
            if resp.status_code != 200:
                _note_busy("photo")
                return thumbs
            query = resp.json().get("query", {})
        except (requests.RequestException, ValueError):
            continue        # leave the titles uncached so the next read retries
        source = {p["title"]: p["thumbnail"]["source"]
                  for p in query.get("pages", {}).values()
                  if p.get("thumbnail", {}).get("source")}
        # A requested title may come back normalised or redirected to another.
        for move in query.get("normalized", []) + query.get("redirects", []):
            if move.get("to") in source and move.get("from"):
                source[move["from"]] = source[move["to"]]
        for title in titles:
            url = source.get(title)
            _cache_write("photo", _wiki_thumb_key(lang, title), url)
            if url:
                thumbs[title] = url
    return thumbs


def _commons_search(query):
    """Best-matching Commons file for a landmark name, or None."""
    def fetch():
        WIKIMEDIA_THROTTLE.wait()
        try:
            resp = requests.get(
                WIKIMEDIA_API.format(project="commons.wikimedia.org"),
                params={
                    "action": "query", "format": "json", "list": "search",
                    "srnamespace": 6, "srlimit": 1, "srsearch": f"{query} filetype:bitmap",
                },
                timeout=10, headers=API_HEADERS,
            )
            if resp.status_code != 200:
                raise UpstreamBusy(f"commons search status {resp.status_code}")
            hits = resp.json().get("query", {}).get("search", [])
        except ValueError:
            return None
        except requests.RequestException as err:
            raise UpstreamBusy("commons search unreachable") from err
        return _thumb_url(hits[0]["title"]) if hits else None

    return cached_lookup("photo", f"search:{query}", fetch)


def _commons_category_file(category):
    """First photo filed under a Commons category, or None."""
    def fetch():
        WIKIMEDIA_THROTTLE.wait()
        try:
            resp = requests.get(
                WIKIMEDIA_API.format(project="commons.wikimedia.org"),
                params={
                    "action": "query", "format": "json", "list": "categorymembers",
                    "cmtitle": category, "cmnamespace": 6, "cmlimit": 8,
                },
                timeout=10, headers=API_HEADERS,
            )
            if resp.status_code != 200:
                raise UpstreamBusy(f"commons category status {resp.status_code}")
            titles = [m.get("title", "") for m in resp.json().get("query", {}).get("categorymembers", [])]
        except ValueError:
            return None
        except requests.RequestException as err:
            raise UpstreamBusy("commons category unreachable") from err
        for title in titles:
            if title.lower().endswith((".jpg", ".jpeg", ".png")):
                return _thumb_url(title)
        return None

    return cached_lookup("photo", f"category:{category}", fetch)


def _place_hint(lat, lon):
    """City a pin belongs to, so photo searches aren't just a bare landmark name."""
    try:
        db = sqlite3.connect(DATABASE, timeout=10)
        try:
            row = db.execute(
                """SELECT city FROM destinations
                   WHERE ROUND(latitude, 3) = ? AND ROUND(longitude, 3) = ?""",
                (round(lat, 3), round(lon, 3)),
            ).fetchone()
        finally:
            db.close()
    except sqlite3.Error:
        row = None
    return f"{row[0]}, {PH_COUNTRY_NAME}" if row and row[0] else PH_COUNTRY_NAME


def _landmark_photos(items, hint=""):
    """Return the landmarks with a photo URL filled in for each.

    The stored list keeps its lookup hints (wiki title, Commons category), so a
    pin whose pictures failed to resolve once can still grow them on a later
    read instead of staying photo-less for the whole cache lifetime.
    """
    images = [it.get("image") for it in items]

    wiki_at = [i for i, it in enumerate(items) if not images[i] and it.get("wiki")]
    if wiki_at:
        thumbs = _wikipedia_thumbs([items[i]["wiki"] for i in wiki_at])
        for i in wiki_at:
            images[i] = thumbs.get(items[i]["wiki"][1])

    # Category listings are curated for the place, so they outrank a name search.
    todo = [i for i, img in enumerate(images)
            if not img and items[i]["name"] != "Unnamed spot"]
    todo.sort(key=lambda i: not items[i].get("category"))
    todo = todo[:COMMONS_SEARCH_LIMIT]

    def lookup(i):
        it = items[i]
        query = f"{it['name']} {hint}".strip()
        if not it.get("category"):
            return _commons_search(query)
        # An OSM-tagged category can be empty or renamed on Commons, so a name
        # search backs it up rather than leaving the row photo-less.
        return _commons_category_file(it["category"]) or _commons_search(query)

    # The throttle spaces the outgoing requests, so running them concurrently
    # keeps a fresh scan from stacking up into a half-minute wait.
    if todo:
        with ThreadPoolExecutor(max_workers=len(todo)) as pool:
            for i, url in zip(todo, pool.map(lookup, todo)):
                images[i] = url

    return [{k: v for k, v in it.items() if k not in ("wiki", "category")} | {"image": img}
            for it, img in zip(items, images)]


def _parse_overpass(payload, lat, lon):
    """Turn a raw Overpass JSON payload into a sorted list of landmark dicts."""
    items = []
    for el in (payload or {}).get("elements", []):
        tags = el.get("tags", {}) or {}
        center = el.get("center") or {}
        el_lat = el.get("lat", center.get("lat"))
        el_lon = el.get("lon", center.get("lon"))
        if el_lat is None or el_lon is None:
            continue
        kind = (
            tags.get("tourism") or tags.get("historic") or tags.get("leisure")
            or tags.get("natural") or tags.get("amenity") or "landmark"
        )
        wiki = _wiki_ref(tags.get("wikipedia"))
        kind_of_commons, commons = _commons_ref(tags.get("wikimedia_commons") or tags.get("image"))
        items.append({
            "name": tags.get("name") or tags.get("name:en") or "Unnamed spot",
            "type": kind.replace("_", " ").title(),
            "lat": el_lat,
            "lon": el_lon,
            "distance_km": round(_haversine_km(lat, lon, el_lat, el_lon), 2),
            "website": tags.get("website") or tags.get("wikipedia"),
            "image": _thumb_url(commons) if kind_of_commons == "file" else None,
            "category": commons if kind_of_commons == "category" else None,
            "wiki": wiki,
        })
    items.sort(key=lambda x: x["distance_km"])
    return items


def _overpass_landmarks(lat, lon, radius_m, limit):
    """Query Overpass for landmark-ish OSM elements near a coordinate."""
    clauses = []
    for tag, values in LANDMARK_FILTERS:
        selector = f'["{tag}"~"{values}"]' if values else f'["{tag}"]'
        clauses.append(f"nwr{selector}(around:{radius_m},{lat},{lon});")
    query = f"[out:json][timeout:25];({''.join(clauses)});out center {limit};"
    return _parse_overpass(_overpass_payload(query), lat, lon)


# A mirror that answers 429/503 is rested instead of being re-hit on the next
# click — pushing harder is how a free endpoint ends up blocking the IP.
MIRROR_REST = 120
# Public mirrors can queue a heavy query for a long time; past this budget we
# give up and serve whatever we cached earlier rather than keep the user waiting.
OVERPASS_BUDGET = 35
_mirrors_rest_until = {}


def _overpass_payload(query, budget=OVERPASS_BUDGET):
    """Post an Overpass query, falling back to mirrors that aren't resting."""
    deadline = time.monotonic() + budget
    for url in OVERPASS_ENDPOINTS:
        remaining = deadline - time.monotonic()
        if _mirrors_rest_until.get(url, 0) > time.monotonic() or remaining <= 1:
            continue
        OVERPASS_THROTTLE.wait()
        try:
            resp = requests.post(url, data={"data": query},
                                 timeout=min(remaining, 25), headers=API_HEADERS)
        except requests.RequestException:
            continue
        if resp.status_code == 200:
            try:
                return resp.json()
            except ValueError:
                continue
        if resp.status_code in (429, 503, 504):
            _mirrors_rest_until[url] = time.monotonic() + MIRROR_REST
    raise UpstreamBusy("all overpass mirrors are busy")


@app.route("/api/landmarks")
def api_landmarks():
    """Return nearby landmarks (attractions, historic sites, nature) for a pin."""
    try:
        lat = float(request.args.get("lat"))
        lon = float(request.args.get("lon"))
    except (TypeError, ValueError):
        return jsonify({"error": "lat and lon query params are required", "items": []}), 400

    try:
        radius = min(max(int(request.args.get("radius", 15000)), 1000), 50000)
        limit = min(max(int(request.args.get("limit", 20)), 1), 40)
    except (TypeError, ValueError):
        radius, limit = 15000, 20

    # Pin positions are stored to 4-6 decimals; rounding the key to ~100 m lets
    # small drags of the same pin reuse one expensive Overpass query.
    key = f"{round(lat, 3)},{round(lon, 3)},{radius},{limit}"
    entry = _cache_row("landmarks", key)
    if entry and entry[1] <= (MISS_TTL if entry[2] else CACHE_TTL["landmarks"]):
        # A scan that came back empty is stored as a miss, so a pin that looked
        # blank by accident re-checks within minutes instead of staying blank for
        # a week, while a genuine answer (open sea) is still reused.
        items = entry[0]["items"] if entry[0] else []
        return _landmark_response(items, radius, lat, lon, cached=True, age_s=entry[1])

    try:
        items = _overpass_landmarks(lat, lon, radius, limit)
    except UpstreamBusy:
        # A stale answer is far better than nothing for a read-only lookup.
        if entry and entry[0]:
            return _landmark_response(entry[0]["items"], radius, lat, lon, stale=True)
        response = jsonify({"error": BUSY_MESSAGE, "items": [], "retry_after_s": MIRROR_REST})
        response.status_code = 502
        response.headers["Retry-After"] = str(MIRROR_REST)
        return response

    # Only the landmark list is cached; pictures are resolved on each read so a
    # pin that was photo-less once can recover later.
    _cache_write("landmarks", key, {"items": items} if items else None)
    return _landmark_response(items, radius, lat, lon)


def _landmark_response(items, radius, lat, lon, cached=False, stale=False, age_s=None):
    """Attach photos to the cached landmarks and shape the JSON response."""
    body = {"items": _landmark_photos(items, _place_hint(lat, lon)),
            "radius_m": radius, "origin": {"lat": lat, "lon": lon}}
    if cached:
        body["cached"] = True
        body["age_s"] = int(age_s or 0)
    if stale:
        body["stale"] = True
    return jsonify(body)


if __name__ == "__main__":
    init_db()
    app.run(debug=True, port=5000)
