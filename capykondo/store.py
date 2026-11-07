"""Tiny SQLite store + in-process event bus shared by the web server and the Fetch agents."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
import uuid
from typing import Any

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
  id TEXT PRIMARY KEY, created_at REAL, mode TEXT, status TEXT, seller TEXT,
  source TEXT, width INTEGER, height INTEGER, duration REAL, extra TEXT
);
CREATE TABLE IF NOT EXISTS items (
  id TEXT PRIMARY KEY, scan_id TEXT, created_at REAL, status TEXT,
  name TEXT, sam_phrase TEXT, category TEXT, title TEXT, brand TEXT, condition TEXT,
  description TEXT, price REAL, floor_price REAL, currency TEXT,
  data TEXT
);
CREATE TABLE IF NOT EXISTS offers (
  id TEXT PRIMARY KEY, item_id TEXT, created_at REAL, buyer TEXT, amount REAL,
  status TEXT, counter REAL, round INTEGER, note TEXT
);
CREATE TABLE IF NOT EXISTS orders (
  id TEXT PRIMARY KEY, item_id TEXT, created_at REAL, buyer TEXT, amount REAL,
  status TEXT, payment_method TEXT, payment_ref TEXT, pickup_code TEXT, data TEXT
);
CREATE TABLE IF NOT EXISTS corrections (
  id TEXT PRIMARY KEY, item_id TEXT, created_at REAL, field TEXT, old TEXT, new TEXT,
  lesson_id TEXT, data TEXT
);
CREATE TABLE IF NOT EXISTS lessons (
  id TEXT PRIMARY KEY, created_at REAL, status TEXT, version INTEGER, text TEXT,
  field TEXT, correction_id TEXT, score_before REAL, score_after REAL, n_eval INTEGER,
  data TEXT
);
CREATE TABLE IF NOT EXISTS activity (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT, data TEXT
);
"""

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False, isolation_level=None)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(SCHEMA)
    return _conn


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _row(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    for k in ("data", "extra"):
        if k in d and isinstance(d[k], str):
            try:
                d[k] = json.loads(d[k])
            except json.JSONDecodeError:
                pass
    return d


def execute(sql: str, args: tuple = ()) -> None:
    with _lock:
        conn().execute(sql, args)


def query(sql: str, args: tuple = ()) -> list[dict]:
    with _lock:
        return [_row(r) for r in conn().execute(sql, args).fetchall()]


def one(sql: str, args: tuple = ()) -> dict | None:
    with _lock:
        return _row(conn().execute(sql, args).fetchone())


# ---------------------------------------------------------------- scans

def create_scan(mode: str, source: str, seller: str = "you", **extra: Any) -> str:
    sid = new_id("scan")
    execute("INSERT INTO scans VALUES (?,?,?,?,?,?,?,?,?,?)",
            (sid, time.time(), mode, "processing", seller, source, None, None, None,
             json.dumps(extra)))
    return sid


def update_scan(sid: str, **fields: Any) -> None:
    if "extra" in fields:
        fields["extra"] = json.dumps(fields["extra"])
    sets = ", ".join(f"{k}=?" for k in fields)
    execute(f"UPDATE scans SET {sets} WHERE id=?", (*fields.values(), sid))


def get_scan(sid: str) -> dict | None:
    return one("SELECT * FROM scans WHERE id=?", (sid,))


def list_scans(limit: int = 20) -> list[dict]:
    return query("SELECT * FROM scans ORDER BY created_at DESC LIMIT ?", (limit,))


# ---------------------------------------------------------------- items

ITEM_COLS = ("status", "name", "sam_phrase", "category", "title", "brand", "condition",
             "description", "price", "floor_price", "currency")


def create_item(scan_id: str, **fields: Any) -> str:
    iid = new_id("item")
    data = fields.pop("data", {})
    cols = {k: fields.get(k) for k in ITEM_COLS}
    cols["status"] = cols["status"] or "processing"
    cols["currency"] = cols["currency"] or config.CURRENCY
    execute("INSERT INTO items (id, scan_id, created_at, " + ", ".join(ITEM_COLS) + ", data) "
            "VALUES (?,?,?," + ",".join("?" * len(ITEM_COLS)) + ",?)",
            (iid, scan_id, time.time(), *cols.values(), json.dumps(data)))
    return iid


def update_item(iid: str, data: dict | None = None, **fields: Any) -> dict:
    with _lock:
        if fields:
            sets = ", ".join(f"{k}=?" for k in fields)
            conn().execute(f"UPDATE items SET {sets} WHERE id=?", (*fields.values(), iid))
        if data:
            cur = get_item(iid) or {}
            merged = dict(cur.get("data") or {})
            merged.update(data)
            conn().execute("UPDATE items SET data=? WHERE id=?", (json.dumps(merged), iid))
        return get_item(iid)


def get_item(iid: str) -> dict | None:
    return one("SELECT * FROM items WHERE id=?", (iid,))


def items_for_scan(scan_id: str) -> list[dict]:
    return query("SELECT * FROM items WHERE scan_id=? ORDER BY created_at", (scan_id,))


def list_items(statuses: tuple[str, ...] | None = None, limit: int = 200) -> list[dict]:
    if statuses:
        ph = ",".join("?" * len(statuses))
        return query(f"SELECT * FROM items WHERE status IN ({ph}) ORDER BY created_at DESC LIMIT ?",
                     (*statuses, limit))
    return query("SELECT * FROM items ORDER BY created_at DESC LIMIT ?", (limit,))


# ---------------------------------------------------------------- activity + event bus

class Bus:
    """Fan-out of JSON events to any number of SSE subscribers (same process)."""

    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def publish(self, kind: str, **data: Any) -> None:
        evt = {"kind": kind, "ts": time.time(), **data}
        loop = self._loop
        for q in list(self._subs):
            try:
                if loop and loop.is_running():
                    loop.call_soon_threadsafe(_put_nowait, q, evt)
                else:
                    q.put_nowait(evt)
            except Exception:  # noqa: BLE001
                pass


def _put_nowait(q: asyncio.Queue, evt: dict) -> None:
    try:
        q.put_nowait(evt)
    except asyncio.QueueFull:
        pass


bus = Bus()


def log_activity(kind: str, **data: Any) -> None:
    execute("INSERT INTO activity (ts, kind, data) VALUES (?,?,?)",
            (time.time(), kind, json.dumps(data)))
    bus.publish("activity", activity_kind=kind, **data)


def recent_activity(limit: int = 50) -> list[dict]:
    rows = query("SELECT * FROM activity ORDER BY id DESC LIMIT ?", (limit,))
    return rows
