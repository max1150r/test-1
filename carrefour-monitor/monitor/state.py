"""Persistencia en SQLite (modo WAL). Todas las operaciones son seguras entre tareas asyncio."""
from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from monitor.models import Status

SCHEMA = """
CREATE TABLE IF NOT EXISTS products(
    key TEXT PRIMARY KEY,
    store TEXT, sku TEXT, name TEXT, url TEXT, image TEXT, seller TEXT,
    status TEXT, stock_status TEXT,
    current_price REAL, previous_price REAL,
    first_seen REAL, last_check REAL, last_change REAL,
    last_notified REAL, last_stock_notified REAL
);
CREATE TABLE IF NOT EXISTS search_hits(
    search_id TEXT, product_key TEXT, query TEXT, first_seen REAL, notified INTEGER DEFAULT 0,
    PRIMARY KEY(search_id, product_key)
);
CREATE TABLE IF NOT EXISTS searches(
    search_id TEXT PRIMARY KEY, query TEXT, initialized_at REAL, last_check REAL
);
"""


@dataclass
class ProductRecord:
    key: str
    store: str
    sku: str | None
    name: str | None
    url: str
    image: str | None
    seller: str | None
    status: Status
    stock_status: Status
    current_price: float | None
    previous_price: float | None
    first_seen: float
    last_check: float | None
    last_change: float | None
    last_notified: float | None
    last_stock_notified: float | None


_COLUMNS = ("key", "store", "sku", "name", "url", "image", "seller", "status", "stock_status",
            "current_price", "previous_price", "first_seen", "last_check", "last_change",
            "last_notified", "last_stock_notified")


class StateStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            if self.path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=30000")
            self._conn.executescript(SCHEMA)
            self._conn.commit()
        self._closed = False

    # ---- helpers síncronos (se ejecutan en un hilo) ----
    def _run(self, fn, *args):
        with self._lock:
            if self._closed:
                raise RuntimeError("StateStore cerrado")
            try:
                result = fn(*args)
                self._conn.commit()
                return result
            except Exception:
                self._conn.rollback()
                raise

    async def _call(self, fn, *args):
        return await asyncio.to_thread(self._run, fn, *args)

    # ---- productos ----
    def _get_product(self, key: str) -> ProductRecord | None:
        row = self._conn.execute("SELECT * FROM products WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["status"] = Status(d["status"] or "UNKNOWN")
        d["stock_status"] = Status(d["stock_status"] or "UNKNOWN")
        return ProductRecord(**d)

    async def get_product(self, key: str) -> ProductRecord | None:
        return await self._call(self._get_product, key)

    def _upsert(self, rec: dict) -> None:
        cols = ", ".join(_COLUMNS)
        qs = ", ".join("?" for _ in _COLUMNS)
        updates = ", ".join(f"{c}=excluded.{c}" for c in _COLUMNS if c not in ("key", "first_seen"))
        self._conn.execute(
            f"INSERT INTO products({cols}) VALUES({qs}) ON CONFLICT(key) DO UPDATE SET {updates}",
            tuple(rec.get(c) for c in _COLUMNS),
        )

    async def upsert_product(self, rec: dict) -> None:
        rec = dict(rec)
        for k in ("status", "stock_status"):
            if isinstance(rec.get(k), Status):
                rec[k] = rec[k].value
        await self._call(self._upsert, rec)

    def _touch(self, key: str, ts: float) -> None:
        self._conn.execute("UPDATE products SET last_check=? WHERE key=?", (ts, key))

    async def touch(self, key: str, ts: float | None = None) -> None:
        await self._call(self._touch, key, ts or time.time())

    # ---- búsquedas ----
    def _mark_hit(self, search_id: str, key: str, query: str | None, ts: float) -> bool:
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO search_hits(search_id, product_key, query, first_seen) VALUES(?,?,?,?)",
            (search_id, key, query, ts),
        )
        return cur.rowcount == 1

    async def mark_hit(self, search_id: str, key: str, query: str | None, ts: float | None = None) -> bool:
        """True si el resultado es nuevo para esta búsqueda."""
        return await self._call(self._mark_hit, search_id, key, query, ts or time.time())

    def _hit_notified(self, search_id: str, key: str) -> None:
        self._conn.execute("UPDATE search_hits SET notified=1 WHERE search_id=? AND product_key=?", (search_id, key))

    async def set_hit_notified(self, search_id: str, key: str) -> None:
        await self._call(self._hit_notified, search_id, key)

    def _search_initialized(self, search_id: str) -> bool:
        row = self._conn.execute("SELECT initialized_at FROM searches WHERE search_id=?", (search_id,)).fetchone()
        return bool(row and row["initialized_at"])

    async def search_initialized(self, search_id: str) -> bool:
        return await self._call(self._search_initialized, search_id)

    def _init_search(self, search_id: str, query: str, ts: float) -> None:
        self._conn.execute(
            "INSERT INTO searches(search_id, query, initialized_at, last_check) VALUES(?,?,?,?) "
            "ON CONFLICT(search_id) DO UPDATE SET initialized_at=COALESCE(initialized_at, excluded.initialized_at), "
            "last_check=excluded.last_check",
            (search_id, query, ts, ts),
        )

    async def mark_search_initialized(self, search_id: str, query: str) -> None:
        await self._call(self._init_search, search_id, query, time.time())

    def _counts(self) -> dict:
        r = self._conn.execute("SELECT COUNT(*) c FROM products").fetchone()["c"]
        h = self._conn.execute("SELECT COUNT(*) c FROM search_hits").fetchone()["c"]
        return {"products": r, "search_hits": h}

    async def counts(self) -> dict:
        return await self._call(self._counts)

    # ---- cierre ----
    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                self._conn.commit()
                if self.path != ":memory:":
                    self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                self._conn.close()
                self._closed = True
