import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


class Store:
    def __init__(self, data: Path):
        data.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(data / "events.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, occurred_at TEXT NOT NULL,
                type TEXT NOT NULL, pet TEXT NOT NULL DEFAULT 'unknown',
                status TEXT NOT NULL, description TEXT NOT NULL,
                score REAL NOT NULL, images TEXT NOT NULL,
                frame_offsets TEXT NOT NULL, notification TEXT NOT NULL DEFAULT 'none'
            );
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS notification_lookup ON events(type, pet, notification, occurred_at);
            UPDATE events SET status='cancelled', description='服务重启，复核未完成'
                WHERE status='reviewing';
            UPDATE events SET notification='unknown' WHERE notification='sending';
        """)
        self.db.commit()

    def add(self, occurred_at, kind, score, images, offsets, status):
        with self.db:
            cursor = self.db.execute(
                "INSERT INTO events(occurred_at,type,status,description,score,images,frame_offsets) VALUES(?,?,?,?,?,?,?)",
                (occurred_at, kind, status, "初判候选，尚未确认", score, json.dumps(images), json.dumps(offsets)),
            )
        return cursor.lastrowid

    def update(self, event_id, *, status, description, pet="unknown", notification="none"):
        with self.db:
            self.db.execute("UPDATE events SET status=?,description=?,pet=?,notification=? WHERE id=?",
                            (status, description, pet, notification, event_id))

    @staticmethod
    def decode(row):
        if row is None:
            return None
        item = dict(row)
        for key in ("images", "frame_offsets"):
            item[key] = json.loads(item[key])
        return item

    def recent(self, before: int | None = None):
        rows = self.db.execute("SELECT * FROM events WHERE id < ? ORDER BY id DESC LIMIT 50", (before or 2**63 - 1,))
        return [self.decode(row) for row in rows]

    def get(self, event_id: int):
        return self.decode(self.db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone())

    def meta(self, key: str) -> float:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return float(row[0]) if row else 0

    def set_meta(self, key: str, value: float):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, str(value)))

    def recently_notified(self, kind, pet, now: datetime):
        cutoff = (now - timedelta(minutes=5)).isoformat()
        return self.db.execute(
            "SELECT 1 FROM events WHERE type=? AND pet=? AND notification IN ('sent','sent_text','unknown') AND occurred_at>? LIMIT 1",
            (kind, pet, cutoff),
        ).fetchone() is not None

    def cleanup(self, images: Path, days: int):
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        with self.db:
            self.db.execute("UPDATE events SET images='[]' WHERE occurred_at<?", (cutoff.isoformat(),))
        for image in images.glob("*.jpg"):
            if image.stat().st_mtime < cutoff.timestamp():
                image.unlink(missing_ok=True)
