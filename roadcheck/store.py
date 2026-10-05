"""SQLite 儲存：訂閱、事件快照、已送出的通知。"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from .models import Event, Subscription

DEFAULT_DB = os.environ.get("ROADCHECK_DB", "roadcheck.sqlite3")

SCHEMA = """
CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    points TEXT NOT NULL,
    radius_m REAL NOT NULL,
    roads TEXT NOT NULL DEFAULT '[]',
    channel TEXT NOT NULL DEFAULT 'console',
    channel_target TEXT NOT NULL DEFAULT '',
    only_blocking INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    key TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    lat REAL, lon REAL,
    address TEXT, start TEXT, end TEXT, time_window TEXT,
    blocks_traffic INTEGER, agency TEXT, purpose TEXT, url TEXT, extra TEXT,
    fingerprint TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications (
    subscription_id INTEGER NOT NULL,
    event_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    PRIMARY KEY (subscription_id, event_key)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path = DEFAULT_DB):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # ---- subscriptions ----
    def add_subscription(self, sub: Subscription) -> Subscription:
        cur = self.conn.execute(
            "INSERT INTO subscriptions (name, kind, points, radius_m, roads, channel, channel_target, only_blocking, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (sub.name, sub.kind, json.dumps(sub.points), sub.radius_m, json.dumps(sub.roads, ensure_ascii=False),
             sub.channel, sub.channel_target, int(sub.only_blocking), _now()),
        )
        self.conn.commit()
        sub.id = cur.lastrowid
        return sub

    def update_subscription(self, sub: Subscription) -> None:
        self.conn.execute(
            "UPDATE subscriptions SET name=?, kind=?, points=?, radius_m=?, roads=?, channel=?, channel_target=?, only_blocking=? WHERE id=?",
            (sub.name, sub.kind, json.dumps(sub.points), sub.radius_m, json.dumps(sub.roads, ensure_ascii=False),
             sub.channel, sub.channel_target, int(sub.only_blocking), sub.id),
        )
        self.conn.commit()

    def delete_subscription(self, sub_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM subscriptions WHERE id=?", (sub_id,))
        self.conn.execute("DELETE FROM notifications WHERE subscription_id=?", (sub_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def list_subscriptions(self, channel_target: Optional[str] = None) -> list[Subscription]:
        if channel_target is None:
            rows = self.conn.execute("SELECT * FROM subscriptions ORDER BY id").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM subscriptions WHERE channel_target=? ORDER BY id", (channel_target,)
            ).fetchall()
        return [self._sub_from_row(r) for r in rows]

    def get_subscription(self, sub_id: int) -> Optional[Subscription]:
        row = self.conn.execute("SELECT * FROM subscriptions WHERE id=?", (sub_id,)).fetchone()
        return self._sub_from_row(row) if row else None

    @staticmethod
    def _sub_from_row(r: sqlite3.Row) -> Subscription:
        return Subscription(
            id=r["id"], name=r["name"], kind=r["kind"],
            points=[tuple(p) for p in json.loads(r["points"])],
            radius_m=r["radius_m"], roads=json.loads(r["roads"]),
            channel=r["channel"], channel_target=r["channel_target"],
            only_blocking=bool(r["only_blocking"]),
        )

    # ---- events ----
    def upsert_events(self, events: Iterable[Event]) -> tuple[int, int]:
        """回傳 (新增數, 內容變更數)。"""
        added = changed = 0
        now = _now()
        for ev in events:
            row = ev.to_row()
            existing = self.conn.execute("SELECT fingerprint FROM events WHERE key=?", (ev.key,)).fetchone()
            if existing is None:
                added += 1
                self.conn.execute(
                    "INSERT INTO events (key, source, source_id, kind, title, lat, lon, address, start, end, time_window,"
                    " blocks_traffic, agency, purpose, url, extra, fingerprint, first_seen, last_seen)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ev.key, row["source"], row["source_id"], row["kind"], row["title"], row["lat"], row["lon"],
                     row["address"], row["start"], row["end"], row["time_window"],
                     None if row["blocks_traffic"] is None else int(row["blocks_traffic"]),
                     row["agency"], row["purpose"], row["url"], row["extra"], row["fingerprint"], now, now),
                )
            else:
                if existing["fingerprint"] != row["fingerprint"]:
                    changed += 1
                self.conn.execute(
                    "UPDATE events SET title=?, lat=?, lon=?, address=?, start=?, end=?, time_window=?, blocks_traffic=?,"
                    " agency=?, purpose=?, url=?, extra=?, fingerprint=?, last_seen=? WHERE key=?",
                    (row["title"], row["lat"], row["lon"], row["address"], row["start"], row["end"], row["time_window"],
                     None if row["blocks_traffic"] is None else int(row["blocks_traffic"]),
                     row["agency"], row["purpose"], row["url"], row["extra"], row["fingerprint"], now, ev.key),
                )
        self.conn.commit()
        return added, changed

    def list_events(self, source: Optional[str] = None) -> list[Event]:
        if source:
            rows = self.conn.execute("SELECT * FROM events WHERE source=? ORDER BY start", (source,)).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM events ORDER BY start").fetchall()
        return [Event.from_row(dict(r)) for r in rows]

    # ---- notifications ----
    def already_notified(self, sub_id: int, event: Event) -> bool:
        row = self.conn.execute(
            "SELECT fingerprint FROM notifications WHERE subscription_id=? AND event_key=?", (sub_id, event.key)
        ).fetchone()
        return row is not None and row["fingerprint"] == event.fingerprint()

    def mark_notified(self, sub_id: int, event: Event) -> None:
        self.conn.execute(
            "INSERT INTO notifications (subscription_id, event_key, fingerprint, sent_at) VALUES (?,?,?,?)"
            " ON CONFLICT(subscription_id, event_key) DO UPDATE SET fingerprint=excluded.fingerprint, sent_at=excluded.sent_at",
            (sub_id, event.key, event.fingerprint(), _now()),
        )
        self.conn.commit()
