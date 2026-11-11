import json
import math
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .config import ACTIVITIES, ROOT, write_private
from .models import PET_NAMES, activity_decision, previous_pets


def recalculate(data: Path):
    """停止采集后运行，备份并重算活动；不调用模型或通知。"""
    database = data / "events.sqlite3"
    if not database.is_file():
        raise FileNotFoundError("未找到活动数据库")
    directory = data / "backups" / ("history-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    directory.mkdir(mode=0o700, parents=True)
    backup = directory / "events.sqlite3"
    backup.touch(mode=0o600, exist_ok=False)
    with closing(sqlite3.connect(database, timeout=30)) as db, closing(sqlite3.connect(backup)) as snapshot:
        snapshot.row_factory = sqlite3.Row
        db.backup(snapshot)
        rows = [dict(row) for row in snapshot.execute("SELECT * FROM activities ORDER BY id")]
        originals = {row["id"]: row["pets"] for row in rows}
        for row in rows:
            row["pets"] = json.loads(row["pets"])
        first = next((row["id"] for row in rows if row["status"] == "done" and all(
            row["pets"].get(pet, {}).get("visibility") in ("present", "absent", "uncertain") for pet in PET_NAMES
        )), None)
        if first is None:
            raise ValueError("没有可重算的入镜记录，未删除或修改活动")
        changes, previous = [], None
        activity_changes = 0
        for row in rows:
            if row["id"] < first:
                continue
            pets = {pet: dict(value) for pet, value in row["pets"].items()}
            if row["status"] == "done":
                prior = previous_pets(previous, datetime.fromisoformat(row["started_at"]))
                for pet, value in pets.items():
                    if not value.get("identity_verified") or value.get("visibility") != "present" or "proposed_activity" not in value:
                        continue
                    activity, confidence = value["proposed_activity"], value["confidence"]
                    if activity not in ACTIVITIES or isinstance(confidence, bool) or not isinstance(confidence, (int, float)) \
                            or not math.isfinite(confidence) or not 0 <= confidence <= 1:
                        raise ValueError(f"活动 {row['id']} 的原始判断无效，未写入重算结果")
                    value.update(activity_decision(activity, confidence, prior.get(pet, {})))
                    if activity == "not_visible":
                        value.update(activity="uncertain", identity_verified=False)
                if pets != row["pets"]:
                    activity_changes += any(pets[pet].get("activity") != value.get("activity") for pet, value in row["pets"].items())
                    changes.append({"id": row["id"], "before": row["pets"], "after": pets})
            previous = {**row, "pets": pets}
        summary = {"backup": str(backup.resolve()), "audit": str((directory / "changes.json").resolve()),
                   "first_retained_id": first, "deleted_records": sum(row["id"] < first for row in rows),
                   "recalculated_records": sum(row["id"] >= first and row["status"] == "done" for row in rows),
                   "updated_records": len(changes), "activity_changed_records": activity_changes}
        audit = {**summary, "applied": False, "changes": changes}
        write_private(directory / "changes.json", audit)
        with db:
            db.execute("BEGIN IMMEDIATE")
            for change in changes:
                updated = db.execute("UPDATE activities SET pets=? WHERE id=? AND status='done' AND pets=?",
                                     (json.dumps(change["after"]), change["id"], originals[change["id"]])).rowcount
                if updated != 1:
                    raise RuntimeError("活动记录发生变化，本次操作已回滚")
            deleted = db.execute("DELETE FROM activities WHERE id < ?", (first,)).rowcount
            if deleted != summary["deleted_records"]:
                raise RuntimeError("活动记录发生变化，本次操作已回滚")
        write_private(directory / "changes.json", {**audit, "applied": True})
    return summary


if __name__ == "__main__":
    print(json.dumps(recalculate(ROOT / "data"), ensure_ascii=False, indent=2))
