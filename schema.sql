-- One row per bookkeeping fact about this database file. Currently just
-- 'schema_version', which is what tells init_db() whether the legacy,
-- pre-accounts destinations table still needs migrating.
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    -- NOCASE uniqueness is what stops 'Admin' and 'admin' being two accounts.
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    is_admin        INTEGER NOT NULL DEFAULT 0 CHECK (is_admin IN (0, 1)),
    is_active       INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    -- Bumped whenever a session should stop working (admin reset, account
    -- disabled, self-service password change). Login records the value in the
    -- cookie; each request compares them, so one integer revokes every live
    -- session without a server-side session store.
    session_version INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Failed logins and sign-up attempts, keyed by account and by client address.
-- Deliberately its own table rather than a reuse of api_cache: this is security
-- state, and it must survive a restart while never being cleared as a cache.
CREATE TABLE IF NOT EXISTS login_attempts (
    key          TEXT PRIMARY KEY,   -- 'u:<username>' | 'i:<ip>' | 'r:<ip>'
    count        INTEGER NOT NULL DEFAULT 0,
    locked_until REAL NOT NULL DEFAULT 0,
    last_failure REAL NOT NULL DEFAULT 0
);

-- Answers from the free map APIs (Overpass, Nominatim, REST Countries,
-- Wikimedia). Kept on disk so restarting or re-editing the app never spends a
-- second request on a question we already know the answer to.
CREATE TABLE IF NOT EXISTS api_cache (
    kind       TEXT NOT NULL,
    key        TEXT NOT NULL,
    payload    TEXT NOT NULL,
    is_miss    INTEGER NOT NULL DEFAULT 0,
    fetched_at REAL NOT NULL,
    PRIMARY KEY (kind, key)
);

-- A pin and everything about it. user_id names its owner: every read and write
-- from the app filters on it (see get_owned_destination), so a destination from
-- someone else's list behaves exactly like one that does not exist.
CREATE TABLE IF NOT EXISTS destinations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER REFERENCES users(id) ON DELETE CASCADE,
    city          TEXT NOT NULL,
    country       TEXT NOT NULL,
    country_code  TEXT,
    latitude      REAL NOT NULL,
    longitude     REAL NOT NULL,
    visited_status TEXT NOT NULL DEFAULT 'wishlist'
        CHECK (visited_status IN ('wishlist', 'visited')),
    notes         TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- idx_destinations_user is created by init_db() rather than here: on an existing
-- database this file runs before user_id has been ALTERed in, and SQLite rejects
-- an index over a column that is not there yet.
