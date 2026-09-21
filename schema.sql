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
