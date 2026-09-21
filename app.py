"""Interactive Travel Bucket List & Map Planner.

A small Flask app that stores destination pins in SQLite, renders them as
summary cards (with country flags from the REST Countries API) alongside an
interactive Leaflet/OpenStreetMap map.
"""

import math
import os
import sqlite3
import time

import requests
from flask import Flask, flash, g, jsonify, redirect, render_template, request, url_for

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-travel-bucket-key")

DATABASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "travel.db")
SCHEMA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")

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
# External API helpers (server-side proxies)
# --------------------------------------------------------------------------- #
def _lookup_country(code):
    """Fetch a single country record from the REST Countries v5 API by alpha-2 code."""
    try:
        resp = requests.get(
            f"{REST_COUNTRIES}/codes.alpha_2/{code}",
            timeout=8,
            headers=COUNTRIES_HEADERS,
        )
        if resp.status_code != 200:
            return None
        payload = resp.json()
        objects = (payload.get("data") or {}).get("objects") or []
        return objects[0] if objects else None
    except (requests.RequestException, ValueError):
        return None


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
    notes = (request.form.get("notes") or "").strip()
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
    the place name when the user clicks the map.
    """
    try:
        lat = float(request.args.get("lat"))
        lon = float(request.args.get("lon"))
    except (TypeError, ValueError):
        return jsonify({"error": "lat and lon query params are required"}), 400

    try:
        resp = requests.get(
            NOMINATIM,
            params={"lat": lat, "lon": lon, "format": "json", "zoom": 10, "addressdetails": 1},
            timeout=8,
            headers=API_HEADERS,
        )
        data = resp.json() if resp.status_code == 200 else {}
    except requests.RequestException:
        data = {}

    address = data.get("address", {}) if isinstance(data, dict) else {}
    city = (
        address.get("city")
        or address.get("town")
        or address.get("village")
        or address.get("municipality")
        or address.get("county")
        or address.get("state")
        or "Unnamed place"
    )
    country = address.get("country") or "Unknown country"
    country_code = (address.get("country_code") or "").lower()
    return jsonify({"city": city, "country": country, "country_code": country_code,
                    "lat": lat, "lon": lon})


@app.route("/api/country/<code>")
def api_country(code):
    """Return flag, currency and language data for a 2-letter country code."""
    code = (code or "").strip().lower()
    if not code:
        return jsonify({"error": "country code required"}), 400
    summary = _summarize(_lookup_country(code))
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
        items.append({
            "name": tags.get("name") or tags.get("name:en") or "Unnamed spot",
            "type": kind.replace("_", " ").title(),
            "lat": el_lat,
            "lon": el_lon,
            "distance_km": round(_haversine_km(lat, lon, el_lat, el_lon), 2),
            "website": tags.get("website") or tags.get("wikipedia"),
        })
    items.sort(key=lambda x: x["distance_km"])
    return items


def _overpass_landmarks(lat, lon, radius_m, limit):
    """Query Overpass for landmark-ish OSM elements, trying mirrors on failure."""
    clauses = []
    for tag, values in LANDMARK_FILTERS:
        selector = f'["{tag}"~"{values}"]' if values else f'["{tag}"]'
        clauses.append(f"nwr{selector}(around:{radius_m},{lat},{lon});")
    query = f"[out:json][timeout:25];({''.join(clauses)});out center {limit};"

    last_err = None
    for url in OVERPASS_ENDPOINTS:
        try:
            resp = requests.post(url, data={"data": query}, timeout=30, headers=API_HEADERS)
            if resp.status_code == 200:
                return _parse_overpass(resp.json(), lat, lon)
            last_err = requests.HTTPError(f"overpass status {resp.status_code}")
        except (requests.RequestException, ValueError) as err:
            last_err = err
    raise last_err or requests.RequestException("all overpass mirrors failed")


# Small TTL cache so repeated clicks (and rate-limit bursts) reuse recent results.
_LANDMARK_CACHE = {}
_LANDMARK_TTL = 600  # seconds


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

    cache_key = (round(lat, 3), round(lon, 3), radius, limit)
    cached = _LANDMARK_CACHE.get(cache_key)
    if cached and cached[0] > time.monotonic():
        return jsonify({"items": cached[1], "radius_m": radius,
                        "origin": {"lat": lat, "lon": lon}, "cached": True})

    try:
        items = _overpass_landmarks(lat, lon, radius, limit)
    except (requests.RequestException, ValueError):
        # Serve a stale entry if we have one, otherwise report unavailability.
        if cached:
            return jsonify({"items": cached[1], "radius_m": radius,
                            "origin": {"lat": lat, "lon": lon}, "stale": True})
        return jsonify({"error": "Landmark service is busy — please try again in a moment.",
                        "items": []}), 502

    _LANDMARK_CACHE[cache_key] = (time.monotonic() + _LANDMARK_TTL, items)
    return jsonify({"items": items, "radius_m": radius, "origin": {"lat": lat, "lon": lon}})


if __name__ == "__main__":
    init_db()
    app.run(debug=True, port=5000)
