import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from million_pound.recalculate import recalculate
from million_pound.store import Store


def observation(activity="sleeping", confidence=.9, proposed="sleeping"):
    return {"dog": {"activity": activity, "confidence": confidence, "proposed_activity": proposed,
                    "visibility": "present", "visibility_confidence": .99, "identity_verified": True},
            "cat": {"activity": "not_visible", "confidence": .99, "visibility": "absent",
                    "visibility_confidence": .99, "identity_verified": False}}


class RecalculationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.store = Store(self.data)
        (self.data / "images").mkdir()

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def add(self, seconds, pets, status="done"):
        start = datetime(2026, 11, 11, tzinfo=timezone.utc) + timedelta(seconds=seconds)
        activity_id = self.store.add_activity(start.isoformat(), (start + timedelta(seconds=10)).isoformat())
        name = f"{activity_id:032x}-1.jpg"
        (self.data / "images" / name).write_bytes(b"proof")
        self.store.activity_images(activity_id, [name], [5])
        self.store.update_activity(activity_id, status, pets=pets)
        return activity_id

    def rows(self):
        return [tuple(row) for row in self.store.db.execute("SELECT * FROM activities ORDER BY id")]

    def test_backup_legacy_removal_recalculation_and_repeat_are_safe(self):
        old = self.add(0, {"dog": {"activity": "sleeping", "confidence": .95}})
        cancelled = self.add(10, {}, "cancelled")
        first = self.add(20, observation())
        low = self.add(30, observation("uncertain", .1))
        zero = self.add(40, observation("uncertain", 0))
        changed = self.add(50, observation("walking", .9, "walking"))
        pending = self.add(60, {}, "pending")
        event_id = self.store.add("2026-11-11T00:00:00+00:00", "eating", .9, [f"{old:032x}-1.jpg"], [5], "confirmed")
        event = self.store.get(event_id)
        original = self.rows()

        result = recalculate(self.data)
        self.assertEqual(result["deleted_records"], 2)
        self.assertEqual(result["recalculated_records"], 4)
        self.assertEqual(result["activity_changed_records"], 2)
        self.assertEqual(result["first_retained_id"], first)
        self.assertIsNone(self.store.get_activity(old))
        self.assertIsNone(self.store.get_activity(cancelled))
        for activity_id, score in ((low, .1), (zero, 0)):
            value = self.store.get_activity(activity_id)["pets"]["dog"]
            self.assertEqual(value["activity"], "sleeping")
            self.assertEqual(value["confidence"], score)
            self.assertTrue(value["continued"])
        self.assertEqual(self.store.get_activity(changed)["pets"]["dog"]["activity"], "walking")
        self.assertEqual(self.store.get_activity(pending)["status"], "pending")
        self.assertEqual(self.store.get(event_id), event)
        self.assertEqual(len(list((self.data / "images").glob("*.jpg"))), 7)

        backup = Path(result["backup"])
        with sqlite3.connect(backup) as db:
            self.assertEqual(db.execute("SELECT * FROM activities ORDER BY id").fetchall(), original)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        audit = json.loads(Path(result["audit"]).read_text())
        self.assertTrue(audit["applied"])
        self.assertEqual(audit["activity_changed_records"], 2)
        self.assertEqual(Path(result["audit"]).stat().st_mode & 0o777, 0o600)
        self.assertEqual(recalculate(self.data)["updated_records"], 0)

    def test_millisecond_timestamp_overlap_preserves_activity_continuity(self):
        self.add(0, observation())
        first = self.add(9.979, observation("uncertain", .79))
        second = self.add(19.978, observation("uncertain", .2))
        large_overlap = self.add(28.9, observation("uncertain", .2))
        recalculate(self.data)
        for activity_id in (first, second):
            value = self.store.get_activity(activity_id)["pets"]["dog"]
            self.assertEqual(value["activity"], "sleeping")
            self.assertTrue(value["continued"])
        self.assertEqual(self.store.get_activity(large_overlap)["pets"]["dog"]["activity"], "uncertain")

    def test_short_hidden_gaps_keep_state_but_long_gaps_reset_it(self):
        self.add(0, observation())
        after_gap = self.add(20, observation("uncertain", .2))
        self.add(30, observation())
        self.add(40, {}, "error")
        after_error = self.add(50, observation("uncertain", .2))
        self.add(60, observation())
        absent = observation("not_visible", .9)
        absent["dog"].update(visibility="absent", identity_verified=False)
        absent_id = self.add(70, absent)
        after_absence = self.add(80, observation("uncertain", .2))
        long_gap = self.add(160, observation("uncertain", .2))
        recalculate(self.data)
        for activity_id in (after_gap, after_error, after_absence):
            value = self.store.get_activity(activity_id)["pets"]["dog"]
            self.assertEqual(value["activity"], "sleeping")
            self.assertTrue(value["continued"])
        self.assertEqual(self.store.get_activity(long_gap)["pets"]["dog"]["activity"], "uncertain")
        self.assertEqual(self.store.get_activity(absent_id)["pets"]["dog"]["activity"], "not_visible")

    def test_invalid_saved_scores_do_not_partially_delete_or_update(self):
        self.add(0, {"dog": {"activity": "sleeping", "confidence": .9}})
        self.add(10, observation())
        self.add(20, observation("uncertain", "invalid"))
        original = self.rows()
        with self.assertRaises(ValueError):
            recalculate(self.data)
        self.assertEqual(self.rows(), original)

    def test_uncertain_candidate_scores_are_preserved_without_promoting_them(self):
        pets = observation("uncertain", .8, "uncertain")
        pets["dog"]["probabilities"] = {"uncertain": .8, "walking": .15, "sleeping": .05}
        activity_id = self.add(0, pets)
        recalculate(self.data)
        decision = self.store.get_activity(activity_id)["pets"]["dog"]
        self.assertEqual(decision["activity"], "uncertain")
        self.assertEqual(decision["proposed_activity"], "uncertain")
        self.assertEqual(decision["confidence"], .8)
        self.assertEqual(decision["probabilities"], pets["dog"]["probabilities"])

    def test_aliases_and_same_uncertain_candidate_keep_confirmed_state(self):
        self.add(0, observation())
        rest = self.add(10, observation("uncertain", .1, "resting"))
        uncertain = observation("uncertain", .8, "uncertain")
        uncertain["dog"]["probabilities"] = {"uncertain": .8, "sleeping": .2}
        fallback = self.add(20, uncertain)
        self.add(30, observation("eating", .9, "eating"))
        drink = self.add(40, observation("uncertain", .1, "drinking"))
        different = self.add(50, observation("uncertain", .1, "walking"))
        result = recalculate(self.data)
        self.assertTrue(Path(result["backup"]).is_file())
        for activity_id, activity, raw in ((rest, "sleeping", "resting"), (fallback, "sleeping", "uncertain"), (drink, "eating", "drinking")):
            decision = self.store.get_activity(activity_id)["pets"]["dog"]
            self.assertEqual(decision["activity"], activity)
            self.assertEqual(decision["proposed_activity"], raw)
            self.assertTrue(decision["continued"])
        self.assertEqual(self.store.get_activity(fallback)["pets"]["dog"]["probabilities"], uncertain["dog"]["probabilities"])
        self.assertEqual(self.store.get_activity(different)["pets"]["dog"]["activity"], "uncertain")

    def test_invalid_candidate_scores_do_not_change_history(self):
        self.add(0, observation())
        invalid = observation("uncertain", .8, "uncertain")
        invalid["dog"]["probabilities"] = {"uncertain": .8, "walking": float("nan")}
        self.add(10, invalid)
        original = self.rows()
        with self.assertRaises(ValueError):
            recalculate(self.data)
        self.assertEqual(self.rows(), original)

    def test_no_visibility_records_are_not_deleted_without_a_boundary(self):
        self.add(0, {"dog": {"activity": "sleeping", "confidence": .9}})
        original = self.rows()
        with self.assertRaises(ValueError):
            recalculate(self.data)
        self.assertEqual(self.rows(), original)
