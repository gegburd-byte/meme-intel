-- SQLite-compatible schema used by the easy Replit build.
CREATE TABLE IF NOT EXISTS tokens (
    mint TEXT PRIMARY KEY,
    symbol TEXT,
    name TEXT,
    created_at TEXT,
    last_seen_at TEXT
);

CREATE TABLE IF NOT EXISTS candles (
    mint TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    ts INTEGER NOT NULL,
    o REAL NOT NULL,
    h REAL NOT NULL,
    l REAL NOT NULL,
    c REAL NOT NULL,
    v REAL DEFAULT 0,
    PRIMARY KEY (mint, timeframe, ts)
);

CREATE TABLE IF NOT EXISTS social_posts (
    id TEXT PRIMARY KEY,
    mint TEXT,
    username TEXT,
    text TEXT,
    created_at TEXT,
    likes INTEGER DEFAULT 0,
    reposts INTEGER DEFAULT 0,
    replies INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT NOT NULL,
    side TEXT NOT NULL,
    entry REAL,
    exit REAL,
    qty REAL,
    pnl REAL,
    r_multiple REAL,
    opened_at TEXT,
    closed_at TEXT,
    note TEXT
);

CREATE TABLE IF NOT EXISTS setups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT NOT NULL,
    state TEXT NOT NULL,
    prev_high REAL,
    higher_low REAL,
    stop REAL,
    updated_at TEXT,
    reason TEXT
);
