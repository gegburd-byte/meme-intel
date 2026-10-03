from __future__ import annotations
import sqlite3, os, time
from pathlib import Path

class Store:
    def __init__(self, path=None):
        self.path = path or os.getenv("DATABASE_PATH","./data/meme_intel.db")
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._schema()
    def _schema(self):
        schema = Path(__file__).with_name("schema.sql").read_text()
        self.conn.executescript(schema)
        self.conn.commit()
    def add_trade(self, mint, side, entry, qty, note=""):
        cur=self.conn.execute(
            "INSERT INTO paper_trades(mint,side,entry,qty,opened_at,note) VALUES(?,?,?,?,datetime('now'),?)",
            (mint,side,entry,qty,note))
        self.conn.commit()
        return cur.lastrowid
    def close_trade(self, trade_id, exit_price):
        row=self.conn.execute("SELECT * FROM paper_trades WHERE id=?", (trade_id,)).fetchone()
        if not row: raise ValueError("trade not found")
        if row["side"].upper()=="LONG":
            pnl=(exit_price-row["entry"])*row["qty"]
        else:
            pnl=(row["entry"]-exit_price)*row["qty"]
        self.conn.execute("UPDATE paper_trades SET exit=?,pnl=?,closed_at=datetime('now') WHERE id=?",
                          (exit_price,pnl,trade_id))
        self.conn.commit()
        return {"id":trade_id,"pnl":pnl}
    def trades(self):
        return [dict(x) for x in self.conn.execute("SELECT * FROM paper_trades ORDER BY id DESC LIMIT 100").fetchall()]
