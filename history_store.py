import sqlite3
import time

DB = "market_history.db"


def connect():
    return sqlite3.connect(DB)


def init_db():
    with connect() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS candles (
                mint TEXT NOT NULL,
                ts INTEGER NOT NULL,
                o REAL NOT NULL,
                h REAL NOT NULL,
                l REAL NOT NULL,
                c REAL NOT NULL,
                v REAL NOT NULL,
                stored_at INTEGER NOT NULL,
                PRIMARY KEY (mint, ts)
            )
        """)
        con.execute("""
            CREATE INDEX IF NOT EXISTS idx_candles_mint_ts
            ON candles (mint, ts)
        """)


def save_candles(mint, candles):
    init_db()

    now = int(time.time())

    with connect() as con:
        con.executemany(
            """
            INSERT OR REPLACE INTO candles
            (mint, ts, o, h, l, c, v, stored_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    mint,
                    int(c.ts),
                    float(c.o),
                    float(c.h),
                    float(c.l),
                    float(c.c),
                    float(c.v),
                    now,
                )
                for c in candles
            ],
        )


def token_counts(min_candles=1):
    init_db()

    with connect() as con:
        rows = con.execute(
            """
            SELECT mint, COUNT(*) AS candle_count
            FROM candles
            GROUP BY mint
            HAVING COUNT(*) >= ?
            ORDER BY candle_count DESC
            """,
            (min_candles,),
        ).fetchall()

    return rows


def load_candles(mint):
    init_db()

    with connect() as con:
        rows = con.execute(
            """
            SELECT ts, o, h, l, c, v
            FROM candles
            WHERE mint = ?
            ORDER BY ts
            """,
            (mint,),
        ).fetchall()

    return rows


init_db()
