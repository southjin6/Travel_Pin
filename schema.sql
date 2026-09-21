CREATE TABLE IF NOT EXISTS destinations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
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
