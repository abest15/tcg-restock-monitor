from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    key TEXT PRIMARY KEY, retailer TEXT, sku TEXT, title TEXT, url TEXT,
    status TEXT, price REAL, watch TEXT, game TEXT,
    first_seen REAL, last_seen REAL, last_change REAL
);
CREATE TABLE IF NOT EXISTS store_stock (
    key TEXT, store_id TEXT, store_name TEXT, address TEXT, distance REAL,
    status TEXT, qty INTEGER, last_seen REAL,
    PRIMARY KEY (key, store_id)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT, retailer TEXT, key TEXT,
    title TEXT, store_id TEXT, store_name TEXT, qty_before INTEGER, qty_after INTEGER,
    price REAL, game TEXT, watch TEXT
);
CREATE INDEX IF NOT EXISTS events_store ON events(retailer, store_id, ts);
CREATE TABLE IF NOT EXISTS sent (key TEXT PRIMARY KEY, ts REAL);
CREATE TABLE IF NOT EXISTS prices (
    product TEXT, ts REAL, market REAL, low REAL, PRIMARY KEY (product, ts)
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def x(self, sql: str, args: tuple = ()) -> None:
        with self.lock:
            self.conn.execute(sql, args)
            self.conn.commit()

    # key/value
    def get(self, k: str, default: Any = None) -> Any:
        rows = self.q("SELECT v FROM kv WHERE k=?", (k,))
        return json.loads(rows[0]["v"]) if rows else default

    def set(self, k: str, v: Any) -> None:
        self.x("INSERT OR REPLACE INTO kv (k, v) VALUES (?, ?)", (k, json.dumps(v)))

    # alert de-duplication
    def recently_sent(self, key: str, within_s: float) -> bool:
        rows = self.q("SELECT ts FROM sent WHERE key=?", (key,))
        return bool(rows) and time.time() - rows[0]["ts"] < within_s

    def mark_sent(self, key: str) -> None:
        self.x("INSERT OR REPLACE INTO sent (key, ts) VALUES (?, ?)", (key, time.time()))

    def listing(self, key: str) -> Optional[sqlite3.Row]:
        rows = self.q("SELECT * FROM listings WHERE key=?", (key,))
        return rows[0] if rows else None

    def stores_for(self, key: str) -> dict[str, sqlite3.Row]:
        return {r["store_id"]: r for r in self.q("SELECT * FROM store_stock WHERE key=?", (key,))}

    def add_event(self, **kw: Any) -> None:
        kw.setdefault("ts", time.time())
        cols = ",".join(kw)
        self.x(f"INSERT INTO events ({cols}) VALUES ({','.join('?' * len(kw))})", tuple(kw.values()))
