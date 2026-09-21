"""Interactive Travel Bucket List & Map Planner.

A small Flask app that keeps a per-account list of destination pins in SQLite
and renders them as summary cards (with country flags from the REST Countries
API) alongside an interactive Leaflet/OpenStreetMap map. Sign-in, open
registration and an administrator who can reset a forgotten password are built
on Flask, Werkzeug and the standard library alone.
"""

import functools
import json
import math
import os
import re
import secrets
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from urllib.parse import quote, unquote, urlparse

import requests
from flask import (Flask, abort, flash, g, jsonify, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

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

# The signing key turns session cookies into credentials, so a shared default
# would let anyone forge an admin's cookie. Refuse to start rather than fall
# back to one: generate yours with
#   python -c "import secrets; print(secrets.token_hex(32))"
# and put it in .env (see .env.example).
SECRET_KEY = os.environ.get("SECRET_KEY")
if not SECRET_KEY:
    raise SystemExit(
        "SECRET_KEY is not set. Add it to .env (or the environment) as the "
        "output of:  python -c \"import secrets; print(secrets.token_hex(32))\""
    )
app.config["SECRET_KEY"] = SECRET_KEY
# Cookies carry the login, so they are unreadable to scripts and withheld from
# other sites' top-level posts; HTTPS-only is opt-in because local dev is plain http.
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = bool(os.environ.get("SESSION_COOKIE_SECURE"))
# Only applies to a session marked `permanent` (the "remember me" checkbox).
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
# Every form in this app is a few hundred characters of text, so Werkzeug's own
# 500 KB field ceiling is still two orders of magnitude more than a real
# submission. Bound the request body so an oversized one is refused outright.
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024

# TRAVEL_DB lets the migration be rehearsed against a copy instead of the real
# file, which is the only safe way to test a schema change in place.
DATABASE = os.environ.get("TRAVEL_DB") or os.path.join(BASE_DIR, "travel.db")
SCHEMA = os.path.join(BASE_DIR, "schema.sql")
# The schema version this code expects. Each step in _upgrade_schema() brings a
# database one version closer to it; never edit an old step, add a new one.
SCHEMA_VERSION = 2

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
# allowed, and the map is bounded by the same box (PH_BOUNDS in script.js,
# which maxBounds enforces on a click).
PH_COUNTRY_CODE = "ph"
PH_COUNTRY_NAME = "Philippines"
# The coordinate limits are checked in /add as well, because there the form is
# client-supplied: the country name proves nothing about the numbers beside it.
PH_LAT_MIN, PH_LAT_MAX = 4.0, 21.3
PH_LNG_MIN, PH_LNG_MAX = 117.0, 126.7


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
    """Create the schema, bring an older database up to date, keep pins Philippine.

    Runs once, from the `__main__` block. `CREATE TABLE IF NOT EXISTS` cannot add
    a column to a table that already exists, so anything written by an earlier
    version of this code needs the explicit steps in `_upgrade_schema`.
    """
    db = sqlite3.connect(DATABASE, timeout=10)
    try:
        # Set before the schema runs: this connection is one of several that do
        # not go through get_db(), and CASCADE only fires where the pragma is on.
        db.execute("PRAGMA foreign_keys = ON")
        with open(SCHEMA, "r", encoding="utf-8") as f:
            db.executescript(f.read())
        _upgrade_schema(db)
        # Focus: remove any destination that is not in the Philippines.
        db.execute("DELETE FROM destinations WHERE LOWER(country_code) != ?", (PH_COUNTRY_CODE,))
        db.execute("CREATE INDEX IF NOT EXISTS idx_destinations_user ON destinations(user_id)")
        db.commit()
    finally:
        db.close()


def _upgrade_schema(db):
    """Run every migration step this database has not been through yet.

    Version-gated rather than "migrate once if unversioned", because a table can
    be older than the file it lives in: `CREATE TABLE IF NOT EXISTS` creates
    `login_attempts` in its *current* shape on a fresh database and then leaves
    that table alone forever, so a file first created against an earlier draft of
    schema.sql keeps whichever columns it started with. Each step stamps its own
    version, so an interrupted run is finished on the next start.
    """
    if int(_meta_get(db, "schema_version") or 0) < 1:
        _migrate_to_v1(db)
    if int(_meta_get(db, "schema_version") or 0) < 2:
        _migrate_to_v2(db)
    # Read back rather than trusting the local count: a step that forgot to stamp
    # would otherwise look like a success and leave the next version's code
    # running against half-migrated tables.
    version = int(_meta_get(db, "schema_version") or 0)
    if version != SCHEMA_VERSION:
        raise RuntimeError(
            f"database is at schema_version {version} but this code needs "
            f"{SCHEMA_VERSION}; add a migration step rather than editing an old one."
        )


def _meta_get(db, key):
    row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _meta_set(db, key, value):
    db.execute(
        """INSERT INTO meta (key, value) VALUES (?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
        (key, str(value)),
    )


def _ensure_admin(db):
    """Return the id of the recovery admin, creating it from ADMIN_PASSWORD once.

    Every database has exactly one of these, whether it is brand new or predates
    accounts, because it is the account that hands out password resets. After the
    row exists the environment variable is never read again, so the app stays
    runnable with ADMIN_PASSWORD removed from .env.
    """
    username = (os.environ.get("ADMIN_USERNAME") or "admin").strip() or "admin"
    # Validated rather than trusted: this name comes from .env, not the sign-up
    # form, so it is the one username that could otherwise reach a template
    # without passing the pattern every other account is held to.
    if not USERNAME_RE.match(username):
        raise SystemExit(f"ADMIN_USERNAME {username!r} is not a valid username: "
                         "3-20 characters, letters, digits and ._- only.")
    row = db.execute("SELECT id, is_admin FROM users WHERE username = ?",
                     (username,)).fetchone()
    if row:
        # Indexed, not by name: init_db() opens this connection without a
        # row_factory, which is why every other query in a migration step reads
        # row[0] as well.
        if not row[1]:
            # Registration is open, so an ordinary account may already own this
            # name by the time the app first boots. Returning its id here would
            # quietly make that account the recovery admin the migration below
            # hands every pre-accounts pin to, so this refuses instead.
            raise SystemExit(
                f"The username {username!r} belongs to a non-admin account, so the "
                "recovery admin cannot be created. Set ADMIN_USERNAME to a name "
                "nobody has registered and run this again."
            )
        return row[0]
    password = os.environ.get("ADMIN_PASSWORD") or ""
    if not password:
        raise SystemExit(
            "No admin account exists yet and ADMIN_PASSWORD is not set.\n"
            "    1. Set ADMIN_PASSWORD in .env (see .env.example) to the password "
            "you want for the '" + username + "' recovery account.\n"
            "    2. Run this again, then empty the value so it is only read once."
        )
    error = _password_error(password)
    if error:
        raise SystemExit(f"ADMIN_PASSWORD is not usable: {error}")
    db.execute(
        "INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, 1)",
        (username, generate_password_hash(password)),
    )
    return db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()[0]


def _migrate_to_v1(db):
    """Give an existing database owners: add user_id and adopt the pins it holds.

    Also covers a fresh file, where each step simply does nothing. Every step is
    idempotent and the whole thing is one transaction, so a database left half
    migrated (an earlier crash, an interrupted run) is finished rather than
    duplicated — and a failure rolls back instead of stranding pins.
    """
    with db:  # commit on success, roll back on any raise below
        columns = [row[1] for row in db.execute("PRAGMA table_info(destinations)")]
        if "user_id" not in columns:
            # SQLite forbids NOT NULL here without a constant default, and a
            # default of 0 would point at a user that cannot exist, so the
            # "every pin has an owner" rule is enforced in Python instead: see
            # get_owned_destination() and the NULL check at the end of this function.
            db.execute("ALTER TABLE destinations ADD COLUMN user_id INTEGER "
                       "REFERENCES users(id) ON DELETE CASCADE")
        admin_id = _ensure_admin(db)
        # Pins written before accounts existed belong to nobody; hand them to the
        # admin, who can pass each one on from the admin page.
        db.execute("UPDATE destinations SET user_id = ? WHERE user_id IS NULL", (admin_id,))
        ownerless = db.execute(
            "SELECT COUNT(*) FROM destinations WHERE user_id IS NULL").fetchone()[0]
        if ownerless:
            raise RuntimeError(
                f"{ownerless} destination(s) still have no owner after migration; "
                "nothing was changed."
            )
        _meta_set(db, "schema_version", 1)


def _migrate_to_v2(db):
    """Give a pre-existing login_attempts table the sliding-window column.

    `count` alone cannot tell five failures in a minute from five spread over a
    week, which is what `last_failure` is for — so a table created before that
    column existed makes every login and every registration raise
    "no such column: last_failure". A fresh database never comes here: schema.sql
    already creates the column, and the check below then does nothing.
    """
    with db:
        columns = [row[1] for row in db.execute("PRAGMA table_info(login_attempts)")]
        if "last_failure" not in columns:
            # Unlike user_id, a constant default is both legal and correct here:
            # 0 means "the window opened with the next attempt", which is how an
            # already-cooling-down row should behave. locked_until is untouched,
            # so no lockout is shortened or extended by this step.
            db.execute("ALTER TABLE login_attempts "
                       "ADD COLUMN last_failure REAL NOT NULL DEFAULT 0")
        _meta_set(db, "schema_version", 2)


# --------------------------------------------------------------------------- #
# Accounts
#
# Written by hand against Flask, Werkzeug and the stdlib: this machine has no
# pip, so Flask-Login, Flask-WTF and bcrypt are not options. Werkzeug's
# generate_password_hash already defaults to scrypt, which is the part worth
# getting right.
# --------------------------------------------------------------------------- #
# The upper bound is not pedantry: scrypt's cost scales with the input, so
# hashing an unbounded password is a cheap way to keep a server busy.
PASSWORD_MIN, PASSWORD_MAX = 8, 200
USERNAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{2,19}\Z")
LOGIN_WINDOW_S, LOGIN_MAX_FAILS, LOGIN_LOCKOUT_S = 15 * 60, 5, 15 * 60
REGISTER_WINDOW_S, REGISTER_MAX, REGISTER_LOCKOUT_S = 60 * 60, 10, 60 * 60
FLASH_GENERIC_LOGIN_ERROR = "Invalid username or password."


def _password_error(password):
    """Return a message for a usable-but-too-short/long password, else None."""
    if len(password) < PASSWORD_MIN:
        return f"Password must be at least {PASSWORD_MIN} characters."
    if len(password) > PASSWORD_MAX:
        return f"Password must be at most {PASSWORD_MAX} characters."
    return None


def _lockout_remaining(db, key):
    """Seconds left on a cool-down for `key`, or 0 when it is not cooling down."""
    row = db.execute("SELECT locked_until FROM login_attempts WHERE key = ?",
                     (key,)).fetchone()
    if row:
        left = row["locked_until"] - time.time()
        if left > 0:
            return int(math.ceil(left))
    return 0


def _note_failure(db, key, max_fails, window_s, lockout_s):
    """Record one refused attempt, locking `key` once the window is exhausted.

    Wall-clock time and a table, not a counter in memory: every in-process
    structure in this file is wiped by the debug reloader, and a lockout that
    resets whenever the app restarts is not a lockout.
    """
    now = time.time()
    row = db.execute("SELECT count, last_failure FROM login_attempts WHERE key = ?",
                     (key,)).fetchone()
    count = 0 if row is None or now - row["last_failure"] > window_s else row["count"]
    count += 1
    locked_until = 0.0
    if count >= max_fails:
        locked_until, count = now + lockout_s, 0
    db.execute(
        """INSERT OR REPLACE INTO login_attempts (key, count, locked_until, last_failure)
           VALUES (?, ?, ?, ?)""",
        (key, count, locked_until, now),
    )


def _clear_failures(db, *keys):
    for key in keys:
        db.execute("DELETE FROM login_attempts WHERE key = ?", (key,))


@app.before_request
def load_logged_in_user():
    """Put the signed-in user (or None) on `g` for every request.

    The cookie only ever holds an id and the session_version that was current
    when it was minted. Comparing them on each read is what lets a password
    reset, an account being disabled, or a change of one's own password end a
    live session immediately — a signed cookie cannot be recalled, so this is
    the standing revocation check that replaces a server-side session store.
    """
    g.user = None
    user_id = session.get("user_id")
    if user_id is None:
        return
    row = get_db().execute(
        "SELECT id, username, is_admin, is_active, session_version "
        "FROM users WHERE id = ?", (user_id,)).fetchone()
    if (row is None or not row["is_active"]
            or row["session_version"] != session.get("sv")):
        session.clear()
        return
    g.user = dict(row)


def login_required(view):
    """Gate a route on a live session; send API and page callers different hints."""
    @functools.wraps(view)
    def wrapped(**kwargs):
        if getattr(g, "user", None):
            return view(**kwargs)
        # The JSON APIs are fetch()ed by script.js, which reads `error`; a
        # redirect would hand it an HTML login page and a confusing 200.
        if request.path.startswith("/api/"):
            return jsonify({"error": "login required"}), 401
        flash("Please sign in to continue.", "error")
        return redirect(url_for("login", next=request.path))
    return wrapped


def admin_required(view):
    """Gate a route on the admin flag. A refused non-admin sees the home page."""
    @functools.wraps(view)
    def wrapped(**kwargs):
        if getattr(g, "user", None) and g.user["is_admin"]:
            return view(**kwargs)
        flash("That page is only available to an administrator.", "error")
        return redirect(url_for("index"))
    return wrapped


def _safe_next(target):
    """Accept a post-login redirect only if it is a path on this site.

    The second character has to be checked as well, not just the leading slash:
    a browser reads both "//host/" and "/\\host/" as an absolute URL at another
    host, so either one would turn a login form into an off-site redirect.
    Control characters are refused because the value becomes a response header,
    where Werkzeug rejects a newline by raising rather than sanitising it.
    """
    if (target and target[0] == "/" and target[1:2] not in ("/", "\\")
            and not any(ch < " " or ch == "\x7f" for ch in target)):
        return target
    return url_for("index")


@app.route("/login", methods=["GET", "POST"])
def login():
    """Sign in, spending the expensive hash only for an address that is not cooling down."""
    if getattr(g, "user", None):
        return redirect(url_for("index"))
    if request.method == "GET":
        return render_template("login.html", next=request.args.get("next") or "")

    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    db = get_db()
    user_key, ip_key = "u:" + username.lower(), "i:" + (request.remote_addr or "?")
    remaining = max(_lockout_remaining(db, user_key), _lockout_remaining(db, ip_key))
    if remaining:
        flash(f"Too many failed attempts. Try again in {remaining} seconds.", "error")
        return render_template("login.html", next=request.form.get("next") or "")

    row = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    # Unknown name, wrong password and disabled account all take this one path
    # with this one message, so the form cannot be used to list usernames. An
    # over-long password is refused by the first test, which short-circuits
    # before the hasher: scrypt's cost grows with its input, and this is the one
    # place a password of any length at all reaches it (register and /account
    # both bound theirs with _password_error first).
    if (len(password) > PASSWORD_MAX or row is None
            or not check_password_hash(row["password_hash"], password)
            or not row["is_active"]):
        _note_failure(db, user_key, LOGIN_MAX_FAILS, LOGIN_WINDOW_S, LOGIN_LOCKOUT_S)
        _note_failure(db, ip_key, LOGIN_MAX_FAILS, LOGIN_WINDOW_S, LOGIN_LOCKOUT_S)
        db.commit()
        flash(FLASH_GENERIC_LOGIN_ERROR, "error")
        return render_template("login.html", next=request.form.get("next") or "")

    # Clearing first drops the anonymous session's flash queue and CSRF token;
    # nothing from before the sign-in survives into it.
    session.clear()
    session["user_id"] = row["id"]
    session["sv"] = row["session_version"]
    session.permanent = bool(request.form.get("remember"))
    # Only the account's own counter is cleared. The per-address one is not:
    # sign-ups are free, so wiping it on every success would let anyone spend a
    # few guesses at somebody else's username and then erase the evidence by
    # signing in to an account of their own. It expires on its own instead.
    _clear_failures(db, user_key)
    db.commit()
    flash(f"Welcome back, {row['username']}.", "success")
    return redirect(_safe_next(request.form.get("next")))


@app.route("/register", methods=["GET", "POST"])
def register():
    """Open self-registration: a new account starts with an empty bucket list."""
    if getattr(g, "user", None):
        return redirect(url_for("index"))
    if request.method == "GET":
        return render_template("register.html")

    db = get_db()
    ip_key = "r:" + (request.remote_addr or "?")
    remaining = _lockout_remaining(db, ip_key)
    if remaining:
        flash("Too many accounts created from here. Try again later.", "error")
        return render_template("register.html")

    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    error = None
    if not USERNAME_RE.match(username):
        error = ("Username must be 3-20 characters: letters, digits and "
                 "._- only, starting with a letter or digit.")
    if error is None:
        error = _password_error(password)
    if request.form.get("confirm") != password:
        error = error or "The two passwords do not match."
    if error:
        flash(error, "error")
        return render_template("register.html")

    # Every attempt counts, including successful ones: open registration is
    # unlimited by design, so an address has to run out at some point.
    _note_failure(db, ip_key, REGISTER_MAX, REGISTER_WINDOW_S, REGISTER_LOCKOUT_S)
    try:
        # Inserted blind instead of checked first: a pre-check would be a
        # TOCTOU race and, worse, a timing oracle for username existence.
        db.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                   (username, generate_password_hash(password)))
    except sqlite3.IntegrityError:
        db.commit()
        # Availability is already probeable by anyone who can reach this form, so
        # the useful message here is the true one; the throttle limits the probing.
        flash("That username is already taken.", "error")
        return render_template("register.html")
    db.commit()

    row = db.execute("SELECT id, session_version FROM users WHERE username = ?",
                     (username,)).fetchone()
    session.clear()
    session["user_id"] = row["id"]
    session["sv"] = row["session_version"]
    flash("Account created. Your bucket list starts empty.", "success")
    return redirect(url_for("index"))


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    """Forget this browser's session. A POST, so the CSRF check applies to it too."""
    session.clear()
    flash("Signed out.", "success")
    return redirect(url_for("index"))


@app.route("/account", methods=["GET", "POST"])
@login_required
def account():
    """Change your own password, which also signs out your other devices."""
    if request.method == "GET":
        return render_template("account.html")

    db = get_db()
    row = db.execute("SELECT * FROM users WHERE id = ?", (g.user["id"],)).fetchone()
    current = request.form.get("current_password") or ""
    new = request.form.get("new_password") or ""
    error = None
    if not check_password_hash(row["password_hash"], current):
        error = "That is not your current password."
    if error is None:
        error = _password_error(new)
    if error is None and new != (request.form.get("confirm") or ""):
        error = "The two new passwords do not match."
    if error:
        flash(error, "error")
        return render_template("account.html")

    db.execute("UPDATE users SET password_hash = ?, session_version = session_version + 1 "
               "WHERE id = ?", (generate_password_hash(new), row["id"]))
    # Read the bumped value back rather than assuming it: the new number has to go
    # into this tab's cookie, or the row update would log this tab out as well.
    db.commit()
    session["sv"] = db.execute("SELECT session_version FROM users WHERE id = ?",
                               (row["id"],)).fetchone()["session_version"]
    flash("Password changed. Other devices signed in to this account are now out.",
          "success")
    return redirect(url_for("index"))


# --------------------------------------------------------------------------- #
# Admin: recovery for accounts, and nothing else
#
# Admins have no access to anyone's pins — / stays strictly user_id = me for
# them too. The role exists to hand out a new password to someone who forgot
# theirs and to stop an account that is misbehaving; an admin can never take an
# action on their own account, so they cannot lock themselves out of it either.
# --------------------------------------------------------------------------- #
def _admin_target(db, user_id):
    """Return (row, error) for an admin acting on another account.

    Refusing to act on one's own account is also the lockout guard: the signed-in
    admin is active and is never the target, so no action here can leave the app
    without somebody who can reset a password.
    """
    if user_id == g.user["id"]:
        return None, "That action is not available on your own account."
    row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        return None, "That user no longer exists."
    return row, None


@app.route("/admin")
@admin_required
def admin():
    """List accounts, with each one's pin count and the actions an admin can take."""
    db = get_db()
    users = db.execute(
        """SELECT u.*, COUNT(d.id) AS pin_count
           FROM users u LEFT JOIN destinations d ON d.user_id = u.id
           GROUP BY u.id ORDER BY u.username""").fetchall()
    my_pins = db.execute("SELECT id, city FROM destinations WHERE user_id = ? "
                         "ORDER BY created_at DESC", (g.user["id"],)).fetchall()
    return render_template("admin.html", users=[dict(u) for u in users], my_pins=my_pins)


@app.route("/admin/toggle/<int:user_id>", methods=["POST"])
@admin_required
def admin_toggle(user_id):
    """Enable or disable an account, which also ends its live sessions."""
    db = get_db()
    row, error = _admin_target(db, user_id)
    if row is None:
        flash(error, "error")
        return redirect(url_for("admin"))
    # Disabling has to log the account out, and bumping session_version is how a
    # cookie is revoked here; enabling bumps it too, so nothing stale survives.
    db.execute("UPDATE users SET is_active = 1 - is_active, "
               "session_version = session_version + 1 WHERE id = ?", (user_id,))
    db.commit()
    flash(f"{row['username']} is now "
          f"{'disabled' if row['is_active'] else 'enabled'}.", "success")
    return redirect(url_for("admin"))


@app.route("/admin/reset_password/<int:user_id>", methods=["POST"])
@admin_required
def admin_reset_password(user_id):
    """Set a new password for someone who forgot theirs, ending their sessions."""
    db = get_db()
    row, error = _admin_target(db, user_id)
    if row is None:
        flash(error, "error")
        return redirect(url_for("admin"))
    password = request.form.get("new_password") or ""
    error = _password_error(password)
    if error:
        flash(error, "error")
        return redirect(url_for("admin"))
    # The bump is the important half: without it the person's phone would keep
    # working with the old cookie after the password they forgot was replaced.
    db.execute("UPDATE users SET password_hash = ?, session_version = session_version + 1 "
               "WHERE id = ?", (generate_password_hash(password), user_id))
    db.commit()
    flash(f"Password reset for {row['username']}. Tell them in person.", "success")
    return redirect(url_for("admin"))


@app.route("/admin/delete/<int:user_id>", methods=["POST"])
@admin_required
def admin_delete(user_id):
    """Delete an account and every pin it owns."""
    db = get_db()
    row, error = _admin_target(db, user_id)
    if row is None:
        flash(error, "error")
        return redirect(url_for("admin"))
    # destinations has ON DELETE CASCADE, but the pragma is per-connection and
    # not every connection in this file sets it, so the pins go explicitly.
    db.execute("DELETE FROM destinations WHERE user_id = ?", (user_id,))
    db.execute("DELETE FROM login_attempts WHERE key = ?", ("u:" + row["username"].lower(),))
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()
    flash(f"Account {row['username']} and its pins were deleted.", "success")
    return redirect(url_for("admin"))


@app.route("/admin/reassign/<int:dest_id>", methods=["POST"])
@admin_required
def admin_reassign(dest_id):
    """Hand a pin the admin owns to another account.

    This is how the pins inherited from before accounts existed get distributed;
    it is deliberately limited to the admin's own list, so no admin route ever
    needs to read another user's destinations.
    """
    db = get_db()
    username = (request.form.get("to_username") or "").strip()
    target = db.execute("SELECT id, is_active FROM users WHERE username = ?",
                        (username,)).fetchone()
    if target is None or not target["is_active"]:
        flash("No active account has that username.", "error")
        return redirect(url_for("admin"))
    pin = get_owned_destination(db, dest_id)
    if pin is None:
        flash("Only pins in your own list can be reassigned.", "error")
        return redirect(url_for("admin"))
    db.execute("UPDATE destinations SET user_id = ? WHERE id = ? AND user_id = ?",
               (target["id"], dest_id, g.user["id"]))
    db.commit()
    flash(f"{pin['city']} now belongs to {username}.", "success")
    return redirect(url_for("admin"))


# --------------------------------------------------------------------------- #
# CSRF
#
# One random token per session, echoed by every form and compared on every
# post. A cross-site request can carry the cookie but not the secret inside the
# body. Origin/Referer are deliberately not checked as well: SameSite=Lax
# already withholds the cookie from cross-site form posts, and a header
# comparison adds edge cases (proxies, referrer policy) without teaching
# anything this project needs.
# --------------------------------------------------------------------------- #
def get_csrf_token():
    return session.setdefault("_csrf", secrets.token_hex(32))


@app.before_request
def verify_csrf():
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return
    expected = session.get("_csrf", "")
    # compare_digest, not `!=`: both sides are secrets and the comparison should
    # not leak how much of a wrong token matched.
    if not expected or not secrets.compare_digest(request.form.get("csrf_token", ""),
                                                  expected):
        abort(400)


@app.errorhandler(400)
def bad_request(error):
    """A form whose token did not match, or a hand-written request."""
    if request.path.startswith("/api/"):
        return jsonify({"error": "bad request"}), 400
    return render_template("error.html",
                           message="That form could not be submitted, probably because "
                                   "this tab was open before you signed in again."), 400


@app.errorhandler(413)
def too_large(error):
    """A request body over MAX_CONTENT_LENGTH, refused before it was read."""
    if request.path.startswith("/api/"):
        return jsonify({"error": "request too large"}), 413
    return render_template("error.html",
                           message="That submission was far too large to be a form."), 413


@app.context_processor
def inject_account_helpers():
    """Make the signed-in user available to every template."""
    return {"current_user": getattr(g, "user", None)}


# The token has to be a Jinja *global* rather than a context-processor value:
# templates loaded with {% import %} get the environment's globals but not the
# rendering context, so _form.html's csrf_field() macro could not see it there.
app.jinja_env.globals["csrf_token"] = get_csrf_token


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


def get_owned_destination(db, dest_id):
    """Fetch a pin only if it belongs to the signed-in user.

    Every read or write of `destinations` that names a specific id goes through
    this, which is what makes another user's pin indistinguishable from one that
    was never there — same None, same flash, no row touched. It also carries the
    "a pin always has an owner" invariant that SQLite cannot state here: an
    ALTER TABLE cannot add a NOT NULL column without a constant default, and a
    default of 0 would reference a user that cannot exist.
    """
    return db.execute("SELECT * FROM destinations WHERE id = ? AND user_id = ?",
                      (dest_id, g.user["id"])).fetchone()


@app.route("/")
@login_required
def index():
    """Render the map planner with the signed-in user's saved destinations."""
    db = get_db()
    rows = db.execute("SELECT * FROM destinations WHERE user_id = ? "
                      "ORDER BY created_at DESC", (g.user["id"],)).fetchall()
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
@login_required
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

    # This planner only tracks Philippine destinations. The name is checked
    # first because that is what the map looked up, and the coordinates next
    # because a hand-made request can claim any name it likes. A comparison is
    # also what rejects NaN and infinity, which float() above accepts happily
    # and a map could never draw.
    if not _is_philippines(country, country_code):
        flash("Only destinations in the Philippines can be added.", "error")
        return redirect(url_for("index"))
    if not (PH_LAT_MIN <= latitude <= PH_LAT_MAX and PH_LNG_MIN <= longitude <= PH_LNG_MAX):
        flash("Only destinations in the Philippines can be added.", "error")
        return redirect(url_for("index"))

    country_code = PH_COUNTRY_CODE
    country = PH_COUNTRY_NAME

    if not city:
        city = "Unnamed place"

    db = get_db()
    db.execute(
        """INSERT INTO destinations
           (user_id, city, country, country_code, latitude, longitude, visited_status, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (g.user["id"], city, country, country_code, latitude, longitude, status, notes),
    )
    db.commit()
    flash(f"Added {city}, {country} to your bucket list.", "success")
    return redirect(url_for("index"))


@app.route("/notes/<int:dest_id>", methods=["POST"])
@login_required
def save_notes(dest_id):
    """Replace the free-text notes on an existing pin."""
    notes = (request.form.get("notes") or "").strip()[:NOTES_MAX]
    db = get_db()
    row = get_owned_destination(db, dest_id)
    if row is None:
        flash("That destination no longer exists.", "error")
        return redirect(url_for("index"))
    # Re-filtered by owner in the statement itself, not only by the check above:
    # an admin could hand this pin to someone else in between the two queries.
    db.execute("UPDATE destinations SET notes = ? WHERE id = ? AND user_id = ?",
               (notes, dest_id, g.user["id"]))
    db.commit()
    flash(f"Notes saved for {row['city']}.", "success")
    return redirect(url_for("index"))


@app.route("/toggle/<int:dest_id>", methods=["POST"])
@login_required
def toggle(dest_id):
    """Flip a destination between 'wishlist' and 'visited'."""
    db = get_db()
    row = get_owned_destination(db, dest_id)
    if row is None:
        flash("That destination no longer exists.", "error")
        return redirect(url_for("index"))
    new_status = "visited" if row["visited_status"] == "wishlist" else "wishlist"
    db.execute(
        "UPDATE destinations SET visited_status = ? WHERE id = ? AND user_id = ?",
        (new_status, dest_id, g.user["id"]),
    )
    db.commit()
    return redirect(url_for("index"))


@app.route("/delete/<int:dest_id>", methods=["POST"])
@login_required
def delete(dest_id):
    """Remove a destination pin, if it is one of ours."""
    db = get_db()
    if get_owned_destination(db, dest_id) is None:
        # Checked first instead of deleting blindly: an id from someone else's
        # list must not report the success it never had.
        flash("That destination no longer exists.", "error")
        return redirect(url_for("index"))
    db.execute("DELETE FROM destinations WHERE id = ? AND user_id = ?",
               (dest_id, g.user["id"]))
    db.commit()
    flash("Destination removed.", "success")
    return redirect(url_for("index"))


# --------------------------------------------------------------------------- #
# JSON API used by the front-end JavaScript
# --------------------------------------------------------------------------- #
@app.route("/api/reverse")
@login_required
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
@login_required
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


def _place_hint(lat, lon, user_id):
    """City a pin belongs to, so photo searches aren't just a bare landmark name.

    Scoped to the caller's own pins: /api/landmarks takes any lat/lng, so an
    unscoped match would let one user's photo results reveal where someone else
    dropped a pin.
    """
    try:
        db = sqlite3.connect(DATABASE, timeout=10)
        try:
            row = db.execute(
                """SELECT city FROM destinations
                   WHERE user_id = ? AND ROUND(latitude, 3) = ? AND ROUND(longitude, 3) = ?""",
                (user_id, round(lat, 3), round(lon, 3)),
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
@login_required
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
    body = {"items": _landmark_photos(items, _place_hint(lat, lon, g.user["id"])),
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
