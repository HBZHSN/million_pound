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
            CREATE TABLE IF NOT EXISTS activities (
                id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', pets TEXT NOT NULL DEFAULT '{}',
                message TEXT NOT NULL DEFAULT '', images TEXT NOT NULL DEFAULT '[]',
                frame_offsets TEXT NOT NULL DEFAULT '[]'
            );
            CREATE INDEX IF NOT EXISTS activity_time ON activities(started_at);
            CREATE INDEX IF NOT EXISTS notification_lookup ON events(type, pet, notification, occurred_at);
            UPDATE events SET status='cancelled', description='服务重启，复核未完成'
                WHERE status='reviewing';
            UPDATE events SET notification='unknown' WHERE notification='sending';
            UPDATE activities SET status='cancelled', message='服务重启，本段未完成判断' WHERE status='pending';
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
        if "pets" in item:
            item["pets"] = json.loads(item["pets"])
        return item

    def recent(self, before: int | None = None):
        rows = self.db.execute("SELECT * FROM events WHERE id < ? ORDER BY id DESC LIMIT 50", (before or 2**63 - 1,))
        return [self.decode(row) for row in rows]

    def get(self, event_id: int):
        return self.decode(self.db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone())

    def add_activity(self, started_at, ended_at):
        with self.db:
            row = self.db.execute("INSERT INTO activities(started_at,ended_at) VALUES(?,?)", (started_at, ended_at))
        return row.lastrowid

    def activity_images(self, activity_id, images, offsets):
        with self.db:
            self.db.execute("UPDATE activities SET images=?,frame_offsets=? WHERE id=?",
                            (json.dumps(images), json.dumps(offsets), activity_id))

    def update_activity(self, activity_id, status, *, pets=None, message=""):
        with self.db:
            self.db.execute("UPDATE activities SET status=?,pets=?,message=? WHERE id=?",
                            (status, json.dumps(pets or {}), message, activity_id))

    def get_activity(self, activity_id):
        return self.decode(self.db.execute("SELECT * FROM activities WHERE id=?", (activity_id,)).fetchone())

    def previous_activity(self, activity_id):
        return self.decode(self.db.execute("SELECT * FROM activities WHERE id < ? ORDER BY id DESC LIMIT 1",
                                           (activity_id,)).fetchone())

    def activities(self, before=None, start=None, end=None, limit=50):
        query, values = "SELECT * FROM activities WHERE id < ?", [before or 2**63 - 1]
        if start is not None:
            query += " AND ended_at>? AND started_at<?"
            values += [start, end]
        rows = self.db.execute(query + " ORDER BY id DESC LIMIT ?", values + [limit]).fetchall()
        return [self.decode(row) for row in rows]

    def activity_events(self, items):
        if not items:
            return {}
        times = [item["started_at"] for item in items]
        rows = self.db.execute("SELECT occurred_at,type,pet FROM events WHERE status='confirmed' AND occurred_at>=? AND occurred_at<=?",
                               (min(times), max(times)))
        events = {}
        for row in rows:
            events.setdefault(row["occurred_at"], []).append({"type": row["type"], "pet": row["pet"]})
        return events

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
            self.db.execute("UPDATE activities SET images='[]' WHERE ended_at<?", (cutoff.isoformat(),))
        for image in images.iterdir():
            if image.suffix in {".jpg", ".mp4"} and image.stat().st_mtime < cutoff.timestamp():
                image.unlink(missing_ok=True)
