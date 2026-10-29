"""SQLite persistence layer. One DB file = resumable, inspectable pipeline state."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS influencers (
  channel_id TEXT PRIMARY KEY, platform TEXT, name TEXT, url TEXT, handle TEXT, country TEXT,
  followers INTEGER, video_count INTEGER, description TEXT, discovered_via TEXT,
  engagement_rate REAL, avg_views REAL, videos_sampled INTEGER, last_upload TEXT,
  recent_videos TEXT, themes TEXT, uploads_playlist TEXT, category TEXT,
  metrics_status TEXT, collected_at TEXT);
CREATE TABLE IF NOT EXISTS filter_results (
  channel_id TEXT PRIMARY KEY, passed INTEGER, reasons TEXT, evaluated_at TEXT);
CREATE TABLE IF NOT EXISTS enrichment (
  channel_id TEXT PRIMARY KEY, email TEXT, email_source TEXT, website TEXT, instagram TEXT,
  tiktok TEXT, audience_age TEXT, audience_gender TEXT, audience_geography TEXT, enriched_at TEXT);
CREATE TABLE IF NOT EXISTS messages (
  channel_id TEXT PRIMARY KEY, email_subject TEXT, email_body TEXT, instagram_dm TEXT,
  signals_used TEXT, model TEXT, status TEXT, generated_at TEXT);
CREATE TABLE IF NOT EXISTS outreach (
  channel_id TEXT, channel TEXT, recipient TEXT, status TEXT, detail TEXT, updated_at TEXT,
  PRIMARY KEY (channel_id, channel));
CREATE TABLE IF NOT EXISTS suppression (email TEXT PRIMARY KEY, reason TEXT, added_at TEXT);
"""

JSON_COLS = {"recent_videos", "themes", "reasons", "signals_used"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def upsert(self, table: str, row: dict) -> None:
        row = {k: (json.dumps(v) if k in JSON_COLS and v is not None else v) for k, v in row.items()}
        cols = ", ".join(row)
        qs = ", ".join("?" for _ in row)
        self.conn.execute(f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({qs})", list(row.values()))
        self.conn.commit()

    def fetch(self, sql: str, params: tuple = ()) -> list[dict]:
        out = []
        for r in self.conn.execute(sql, params).fetchall():
            d = dict(r)
            for k in JSON_COLS & d.keys():
                if d[k] is not None:
                    d[k] = json.loads(d[k])
            out.append(d)
        return out

    def one(self, sql: str, params: tuple = ()) -> dict | None:
        rows = self.fetch(sql, params)
        return rows[0] if rows else None

    def influencers(self) -> list[dict]:
        return self.fetch("SELECT * FROM influencers ORDER BY followers DESC")

    def shortlisted(self) -> list[dict]:
        return self.fetch(
            "SELECT i.* FROM influencers i JOIN filter_results f USING(channel_id) "
            "WHERE f.passed=1 ORDER BY i.followers DESC")

    def is_suppressed(self, email: str) -> bool:
        return self.one("SELECT 1 AS x FROM suppression WHERE email=?", (email.lower(),)) is not None

    def suppress(self, email: str, reason: str = "manual") -> None:
        self.upsert("suppression", {"email": email.lower(), "reason": reason, "added_at": now_iso()})

    def outreach_status(self, channel_id: str, channel: str) -> str | None:
        r = self.one("SELECT status FROM outreach WHERE channel_id=? AND channel=?", (channel_id, channel))
        return r["status"] if r else None

    def record_outreach(self, channel_id: str, channel: str, recipient: str, status: str, detail: str = "") -> None:
        self.upsert("outreach", {"channel_id": channel_id, "channel": channel, "recipient": recipient,
                                 "status": status, "detail": detail, "updated_at": now_iso()})
