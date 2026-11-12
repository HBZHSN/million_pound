import asyncio
import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient
from pydantic import ValidationError

from million_pound.app import create_app
from million_pound.config import Config, Settings, TimeWindow, in_schedule
from million_pound.media import MediaError
from million_pound.models import ModelError, Models, ReviewEvent, send_bark
from million_pound.monitor import Monitor
from million_pound.security import Security
from million_pound.store import Store


PASSWORD = "test-password-with-12-characters"


def visibility_response(dog=("present", .95), cat=("present", .95)):
    return {"answers": {pet + "_presence": {"choice": choice, "probabilities": {choice: confidence}}
                        for pet, (choice, confidence) in (("dog", dog), ("cat", cat))}}


class ScheduleTests(unittest.TestCase):
    def test_beijing_windows_boundaries_and_overnight(self):
        def at(hour, minute=0):
            return datetime(2026, 1, 1, hour, minute, tzinfo=timezone.utc)
        day = [TimeWindow(start="09:00", end="18:00")]
        self.assertFalse(in_schedule(day, at(0, 59)))
        self.assertTrue(in_schedule(day, at(1)))
        self.assertFalse(in_schedule(day, at(10)))
        night = [TimeWindow(start="22:00", end="06:00")]
        self.assertTrue(in_schedule(night, at(15)))
        self.assertTrue(in_schedule(night, at(21)))
        self.assertFalse(in_schedule(night, at(22)))
        self.assertTrue(in_schedule([], at(12)))
        self.assertTrue(in_schedule([TimeWindow(start="12:00", end="12:00")], at(12)))

    def test_config_boundaries(self):
        for value in ({"clip_duration": 0}, {"sample_fps": 0}, {"speed": 0}, {"enabled": "false"},
                      {"stream_url": "file:///etc/passwd"}, {"event_modes": {"eating": "notify"}},
                      {"model_url": "http://example.com/v1/chat/completions"},
                      {"public_url": "https://example.com/path"}, {"bark_url": "https://api.day.app"}):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                Settings(**value)

    def test_minute_clip_becomes_ten_second_video_by_default(self):
        settings = Settings()
        self.assertEqual(settings.clip_duration, 60)
        self.assertEqual(settings.speed, 6)
        self.assertEqual(settings.clip_duration / settings.speed, 10)
        self.assertEqual(settings.review_interval, 0)


class LocalSetup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        Security.set_password(self.root / "data", PASSWORD)
        self.security = Security(self.root / "data")

    def tearDown(self):
        self.tmp.cleanup()


class LocalTests(LocalSetup):
    def test_old_interval_config_migrates_without_losing_secrets(self):
        (self.root / "config.json").write_text(json.dumps({"sample_interval": 60, "model_key": "private-key"}))
        config = Config(self.root)
        self.assertEqual(config.current.sample_fps, 2)
        self.assertEqual(config.current.model_key, "private-key")
        self.assertNotIn("sample_interval", config.current.model_dump())

    def test_password_sessions_and_signed_images(self):
        sec = self.security
        self.assertTrue(sec.verify_password(PASSWORD))
        self.assertFalse(sec.verify_password("wrong"))
        token = sec.session()
        self.assertTrue(sec.valid_session(token))
        self.assertFalse(sec.valid_session(token + "tampered"))
        payload = f"{int(time.time()) - 1}.nonce"
        self.assertFalse(sec.valid_session(payload + "." + sec.sign("session:" + payload)))
        expiry = int(time.time()) + 600
        sig = sec.image_signature("one.jpg", expiry)
        self.assertTrue(sec.valid_image("one.jpg", expiry, sig))
        self.assertFalse(sec.valid_image("two.jpg", expiry, sig))
        self.assertFalse(sec.valid_image("one.jpg", int(time.time()) - 1, sig))

    def test_event_retention_and_dedup_persist(self):
        images = self.root / "data" / "images"
        images.mkdir()
        image = images / "old.jpg"
        image.write_bytes(b"example")
        now = datetime.now(timezone.utc)
        store = Store(self.root / "data")
        event_id = store.add(now.isoformat(), "eating", .9, [image.name], [1], "confirmed")
        store.update(event_id, status="confirmed", description="进食", pet="dog", notification="sent_text")
        self.assertTrue(store.recently_notified("eating", "dog", now))
        self.assertFalse(store.recently_notified("eating", "cat", now))
        self.assertFalse(store.recently_notified("eating", "dog", now + timedelta(minutes=6)))
        store.set_meta("last_review", 123)
        store.db.close()
        store = Store(self.root / "data")
        self.assertEqual(store.meta("last_review"), 123)
        import os
        old = (now - timedelta(days=8)).timestamp()
        os.utime(image, (old, old))
        store.db.execute("UPDATE events SET occurred_at=? WHERE id=?", ((now - timedelta(days=8)).isoformat(), event_id))
        store.db.commit()
        store.cleanup(images, 7)
        self.assertFalse(image.exists())
        self.assertEqual(store.get(event_id)["images"], [])
        self.assertEqual(store.get(event_id)["description"], "进食")
        store.db.close()

    def test_password_change_validates_persists_and_revokes_old_signatures(self):
        sec = self.security
        original = sec.path.read_bytes()
        token = sec.session()
        expiry = int(time.time()) + 600
        signature = sec.image_signature("one.jpg", expiry)
        for password in ("x" * 10, "x" * 257):
            with self.subTest(length=len(password)), self.assertRaises(ValueError):
                sec.change_password(password)
            self.assertEqual(sec.path.read_bytes(), original)
            self.assertTrue(sec.verify_password(PASSWORD))
        sec.change_password("new-pass-11")
        self.assertTrue(sec.verify_password("new-pass-11"))
        self.assertFalse(sec.verify_password(PASSWORD))
        self.assertFalse(sec.valid_session(token))
        self.assertFalse(sec.valid_image("one.jpg", expiry, signature))
        self.assertTrue(sec.valid_session(sec.session()))
        self.assertTrue(Security(sec.path.parent).verify_password("new-pass-11"))
        self.assertNotIn(b"new-pass-11", sec.path.read_bytes())
        self.assertEqual(sec.path.stat().st_mode & 0o777, 0o600)

    def test_activity_pagination_and_evidence_retention(self):
        store = Store(self.root / "data")
        images = self.root / "data" / "images"
        images.mkdir()
        old = datetime.now(timezone.utc) - timedelta(days=8)
        name = "c" * 32 + "-1.jpg"
        path = images / name
        path.write_bytes(b"proof")
        video = path.with_suffix(".mp4")
        video.write_bytes(b"model-video")
        recent_video = images / ("d" * 32 + "-1.mp4")
        recent_video.write_bytes(b"recent-video")
        import os
        os.utime(path, (old.timestamp(), old.timestamp()))
        os.utime(video, (old.timestamp(), old.timestamp()))
        for i in range(55):
            when = old + timedelta(seconds=i * 10)
            activity_id = store.add_activity(when.isoformat(), (when + timedelta(seconds=10)).isoformat())
            store.activity_images(activity_id, [name], [5])
            store.update_activity(activity_id, "done", pets={"dog": {"activity": "resting", "confidence": .9}})
        first = store.activities()
        second = store.activities(before=first[-1]["id"])
        self.assertEqual((len(first), len(second)), (50, 5))
        self.assertFalse({row["id"] for row in first} & {row["id"] for row in second})
        self.assertEqual(store.previous_activity(activity_id)["id"], activity_id - 1)
        self.assertIsNone(store.previous_activity(second[-1]["id"]))
        store.cleanup(images, 7)
        self.assertFalse(path.exists())
        self.assertFalse(video.exists())
        self.assertTrue(recent_video.exists())
        activity = store.get_activity(activity_id)
        self.assertEqual(activity["images"], [])
        self.assertEqual(activity["pets"]["dog"]["activity"], "resting")
        store.db.close()


class WebTests(LocalSetup):
    def setUp(self):
        super().setUp()
        config = Config(self.root)
        config.save(Settings(model_key="private-model-key", bark_url="https://example.com/private-bark-key",
                             stream_url="rtsp://user:private-camera-password@127.0.0.1/feed"))
        self.client = TestClient(create_app(self.root))
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        super().tearDown()

    def login(self):
        self.assertEqual(self.client.post("/api/login", json={"password": PASSWORD}).status_code, 200)
        return {"X-CSRF-Token": self.client.get("/api/session").json()["csrf"]}

    def test_authentication_secret_redaction_csrf_and_restore(self):
        for path in ("/api/config", "/api/events", "/api/activities", "/api/status", "/api/preview"):
            self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.client.get("/", follow_redirects=False).status_code, 303)
        self.assertEqual(self.client.get("/docs").status_code, 404)
        self.assertEqual(self.client.post("/api/login", json={"password": "wrong"}).status_code, 401)
        headers = self.login()
        response = self.client.get("/api/config")
        for secret in ("private-model-key", "private-bark-key", "private-camera-password"):
            self.assertNotIn(secret, response.text)
        self.assertTrue(response.json()["stream_url_configured"])
        self.assertEqual(self.client.put("/api/config", json={"clip_duration": 20}).status_code, 403)
        self.assertEqual(self.client.put("/api/config", json={"clip_duration": 20}, headers=headers).status_code, 200)
        restored = Config(self.root).current
        self.assertEqual(restored.clip_duration, 20)
        self.assertEqual(restored.model_key, "private-model-key")
        self.assertEqual(self.client.put("/api/config", json={"enabled": "false"}, headers=headers).status_code, 422)
        self.assertEqual(self.client.put("/api/config", json={"enabled": True}, headers={**headers, "Origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(self.client.post("/api/logout", headers=headers).status_code, 200)
        self.assertEqual(self.client.get("/api/config").status_code, 401)

    def test_image_links_are_bound_and_expire(self):
        images = self.root / "data" / "images"
        name = "a" * 32 + "-0.jpg"
        (images / name).write_bytes(b"jpeg-fixture")
        expiry = int(time.time()) + 600
        sig = self.security.image_signature(name, expiry)
        url = f"/shared/{name}?expires={expiry}&sig={sig}"
        self.assertEqual(self.client.get(url).content, b"jpeg-fixture")
        self.assertEqual(self.client.get(url + "bad").status_code, 403)
        expired = int(time.time()) - 1
        self.assertEqual(self.client.get(f"/shared/{name}?expires={expired}&sig={self.security.image_signature(name, expired)}").status_code, 403)
        self.assertEqual(self.client.get("/data/images/" + name).status_code, 404)
        store = Store(self.root / "data")
        event_id = store.add(datetime.now(timezone.utc).isoformat(), "eating", .9, [name], [1], "confirmed")
        store.db.close()
        private = f"/api/events/{event_id}/images/0"
        self.assertEqual(self.client.get(private).status_code, 401)
        self.login()
        self.assertEqual(self.client.get(private).status_code, 200)

    def test_home_defaults_to_timeline_and_settings_remain_accessible(self):
        for path in ("/", "/activities", "/settings"):
            response = self.client.get(path, follow_redirects=False)
            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers["location"], "/login")
        self.login()
        for path in ("/", "/activities"):
            page = self.client.get(path)
            self.assertEqual(page.status_code, 200)
            self.assertIn('id="activity-list"', page.text)
            self.assertIn('href="/settings#settings"', page.text)
            self.assertIn('href="/settings#events"', page.text)
        settings = self.client.get("/settings")
        self.assertEqual(settings.status_code, 200)
        self.assertIn('id="settings-form"', settings.text)
        self.assertIn('id="password-form"', settings.text)

    def test_login_rejects_foreign_origin_and_rate_limits(self):
        self.assertEqual(self.client.post("/api/login", json={"password": PASSWORD}, headers={"Origin": "https://evil.example"}).status_code, 403)
        for _ in range(5):
            self.assertEqual(self.client.post("/api/login", json={"password": "wrong"}).status_code, 401)
        self.assertEqual(self.client.post("/api/login", json={"password": PASSWORD}).status_code, 429)

    def test_public_origin_allows_proxied_login_and_preserves_csrf_protection(self):
        origin = "https://pets.example.com"
        self.assertEqual(self.client.post("/api/login", json={"password": PASSWORD},
                                        headers={"Origin": origin}).status_code, 403)
        headers = self.login()
        response = self.client.put("/api/config", json={"public_url": origin + "/"}, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["public_url"], origin)
        self.assertEqual(Config(self.root).current.public_url, origin)
        self.assertEqual(self.client.post("/api/login", json={"password": PASSWORD},
                                        headers={"Origin": origin}).status_code, 200)
        csrf = self.client.get("/api/session").json()["csrf"]
        self.assertEqual(self.client.post("/api/logout", headers={"Origin": origin}).status_code, 403)
        self.assertEqual(self.client.post("/api/logout", headers={"Origin": "https://evil.example",
                                                                 "X-CSRF-Token": csrf}).status_code, 403)
        self.assertEqual(self.client.post("/api/login", json={"password": PASSWORD},
                                        headers={"Origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(self.client.post("/api/logout", headers={"Origin": origin,
                                                                 "X-CSRF-Token": csrf}).status_code, 200)

    def test_activity_page_images_and_beijing_midnight_filter(self):
        self.assertEqual(self.client.get("/activities", follow_redirects=False).status_code, 303)
        store = Store(self.root / "data")
        start = datetime.now(timezone.utc).replace(hour=15, minute=59, second=55, microsecond=0)
        end = start + timedelta(seconds=10)
        activity_id = store.add_activity(start.isoformat(), end.isoformat())
        name = "b" * 32 + "-1.jpg"
        (self.root / "data" / "images" / name).write_bytes(b"activity-image")
        store.activity_images(activity_id, [name], [5])
        store.update_activity(activity_id, "done", pets={"dog": {"activity": "sleeping", "confidence": .9},
                                                       "cat": {"activity": "not_visible", "confidence": .95}})
        store.db.close()
        private = f"/api/activities/{activity_id}/images/0"
        self.assertEqual(self.client.get(private).status_code, 401)
        self.login()
        self.assertEqual(self.client.get("/activities").status_code, 200)
        self.assertEqual(self.client.get(private).content, b"activity-image")
        beijing_day = (end + timedelta(hours=8)).date().isoformat()
        rows = self.client.get("/api/activities", params={"day": beijing_day, "hour": 0}).json()["items"]
        self.assertEqual([r["id"] for r in rows], [activity_id])
        self.assertEqual(rows[0]["pets"]["cat"]["activity"], "not_visible")
        self.assertIsNone(rows[0]["video"])
        self.assertEqual(len(self.client.get("/api/activities", params={"day": beijing_day, "limit": 1000}).json()["items"]), 1)
        self.assertEqual(self.client.get("/api/activities", params={"day": beijing_day, "limit": 1001}).status_code, 422)
        self.assertEqual(self.client.get("/api/activities", params={"day": beijing_day, "hour": 1}).json()["items"], [])
        self.assertEqual(self.client.get("/api/activities?hour=24").status_code, 422)

    def test_activity_videos_are_private_support_ranges_and_handle_missing_files(self):
        store = Store(self.root / "data")
        now = datetime.now(timezone.utc).isoformat()
        activity_id = store.add_activity(now, now)
        name = "e" * 32 + "-1.jpg"
        store.activity_images(activity_id, [name], [0])
        store.update_activity(activity_id, "done")
        video = (self.root / "data" / "images" / name).with_suffix(".mp4")
        video.write_bytes(b"exact-model-video")
        url = f"/api/activities/{activity_id}/video"
        self.assertEqual(self.client.get(url).status_code, 401)
        self.assertEqual(self.client.get("/data/images/" + video.name).status_code, 404)
        self.login()
        self.assertEqual(self.client.get("/api/activities").json()["items"][0]["video"], url)
        response = self.client.get(url)
        self.assertEqual(response.content, video.read_bytes())
        self.assertEqual(response.headers["content-type"], "video/mp4")
        self.assertEqual(response.headers["cache-control"], "no-store")
        response = self.client.get(url, headers={"Range": "bytes=0-4"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, b"exact")
        self.assertEqual(self.client.get("/api/activities/999999/video").status_code, 404)
        video.unlink()
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertIsNone(self.client.get("/api/activities").json()["items"][0]["video"])
        video.write_bytes(b"expired-video")
        store.activity_images(activity_id, [], [])
        self.assertEqual(self.client.get(url).status_code, 404)
        (self.root / "data" / "outside.mp4").write_bytes(b"private")
        store.activity_images(activity_id, ["../outside.jpg"], [0])
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertIsNone(self.client.get("/api/activities").json()["items"][0]["video"])
        store.db.close()

    def media_fixture(self, prefix):
        store = Store(self.root / "data")
        now = datetime.now(timezone.utc).isoformat()
        names = [prefix * 32 + f"-{index}.jpg" for index in range(3)]
        for name in names:
            (self.root / "data" / "images" / name).write_bytes(b"private-image")
        (self.root / "data" / "images" / names[1]).with_suffix(".mp4").write_bytes(b"private-video")
        activity_id = store.add_activity(now, now)
        store.activity_images(activity_id, [names[1]], [30])
        store.update_activity(activity_id, "done", pets={"dog": {"activity": "eating"}}, message="保留活动文字")
        event_ids = [store.add(now, kind, .9, names, [6, 30, 54], "confirmed") for kind in ("eating", "destruction")]
        for ident in event_ids:
            store.update(ident, status="confirmed", description="保留事件文字", pet="dog", notification="sent")
        store.db.close()
        return activity_id, event_ids, names

    def test_single_and_batch_media_deletion_cleans_both_pages_and_shared_links(self):
        headers = self.login()
        for collection, prefixes in (("activities", ("1",)), ("events", ("2", "3"))):
            with self.subTest(collection=collection):
                fixtures = [self.media_fixture(prefix) for prefix in prefixes]
                unrelated = self.media_fixture("f")
                ids = [activity if collection == "activities" else events[0] for activity, events, _ in fixtures]
                result = self.client.request("DELETE", f"/api/{collection}/media", json={"ids": ids}, headers=headers)
                self.assertEqual(result.status_code, 200, result.text)
                self.assertCountEqual(result.json()["activities"], [row[0] for row in fixtures])
                self.assertCountEqual(result.json()["events"], [ident for row in fixtures for ident in row[1]])
                store = Store(self.root / "data")
                for activity, events, names in fixtures:
                    saved = store.get_activity(activity)
                    self.assertEqual((saved["images"], saved["frame_offsets"]), ([], []))
                    self.assertEqual((saved["message"], saved["status"]), ("保留活动文字", "done"))
                    self.assertEqual(self.client.get(f"/api/activities/{activity}/images/0").status_code, 404)
                    self.assertEqual(self.client.get(f"/api/activities/{activity}/video").status_code, 404)
                    for ident in events:
                        saved = store.get(ident)
                        self.assertEqual((saved["images"], saved["frame_offsets"]), ([], []))
                        self.assertEqual((saved["description"], saved["notification"]), ("保留事件文字", "sent"))
                        self.assertEqual(self.client.get(f"/api/events/{ident}/images/0").status_code, 404)
                    for name in names:
                        expiry = int(time.time()) + 600
                        signature = self.security.image_signature(name, expiry)
                        self.assertEqual(self.client.get(f"/shared/{name}?expires={expiry}&sig={signature}").status_code, 404)
                        self.assertFalse((self.root / "data" / "images" / name).exists())
                    self.assertFalse((self.root / "data" / "images" / names[1]).with_suffix(".mp4").exists())
                self.assertEqual(self.client.get(f"/api/activities/{unrelated[0]}/video").status_code, 200)
                self.assertEqual(self.client.get(f"/api/events/{unrelated[1][0]}/images/0").status_code, 200)
                self.assertEqual(self.client.request("DELETE", f"/api/{collection}/media", json={"ids": ids + ids}, headers=headers).status_code, 200)
                store.db.close()

    def test_media_deletion_checks_authentication_csrf_ids_and_file_paths(self):
        activity, events, names = self.media_fixture("4")
        for collection, ident in (("activities", activity), ("events", events[0])):
            path, payload = f"/api/{collection}/media", {"ids": [ident]}
            self.assertEqual(self.client.request("DELETE", path, json=payload).status_code, 401)
        headers = self.login()
        for collection, ident in (("activities", activity), ("events", events[0])):
            path, payload = f"/api/{collection}/media", {"ids": [ident]}
            self.assertEqual(self.client.request("DELETE", path, json=payload).status_code, 403)
            self.assertEqual(self.client.request("DELETE", path, json=payload, headers={**headers, "Origin": "https://evil.example"}).status_code, 403)
            for invalid in ([], [0], [-1], [True], ["1"], [2**63], [1] * 5001):
                self.assertEqual(self.client.request("DELETE", path, json={"ids": invalid}, headers=headers).status_code, 422)
            self.assertEqual(self.client.request("DELETE", path, json={"ids": [ident, 999999]}, headers=headers).status_code, 404)
        self.assertTrue((self.root / "data" / "images" / names[1]).exists())
        outside = self.root / "data" / "outside.jpg"
        outside.write_bytes(b"must-stay")
        store = Store(self.root / "data")
        store.activity_images(activity, ["../outside.jpg"], [1])
        response = self.client.request("DELETE", "/api/activities/media", json={"ids": [activity]}, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(outside.read_bytes(), b"must-stay")
        self.assertEqual(store.get_activity(activity)["images"], [])
        store.db.close()

    def test_media_deletion_handles_missing_files_and_reports_io_failures_without_clearing_references(self):
        activity, events, names = self.media_fixture("5")
        headers = self.login()
        payload = {"ids": [events[0]]}
        with patch("million_pound.store.Path.unlink", side_effect=PermissionError("private-path")):
            response = self.client.request("DELETE", "/api/events/media", json=payload, headers=headers)
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("private-path", response.text)
        self.assertEqual(self.client.get(f"/api/events/{events[0]}/images/0").status_code, 200)
        (self.root / "data" / "images" / names[0]).unlink()
        self.assertEqual(self.client.request("DELETE", "/api/events/media", json=payload, headers=headers).status_code, 200)
        self.assertEqual(self.client.get(f"/api/activities/{activity}/video").status_code, 404)

    def test_media_deletion_waits_for_the_initial_screenshot(self):
        store = Store(self.root / "data")
        now = datetime.now(timezone.utc).isoformat()
        activity = store.add_activity(now, now)
        headers = self.login()
        response = self.client.request("DELETE", "/api/activities/media", json={"ids": [activity]}, headers=headers)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(store.get_activity(activity)["status"], "pending")
        store.db.close()

    def test_password_change_checks_authentication_and_revokes_old_sessions(self):
        payload = {"current_password": PASSWORD, "new_password": "new-pass-11"}
        self.assertEqual(self.client.put("/api/password", json=payload).status_code, 401)
        headers = self.login()
        old_token = self.client.cookies.get("pet_session")
        self.assertEqual(self.client.put("/api/password", json=payload).status_code, 403)
        self.assertEqual(self.client.put("/api/password", json=payload,
                                        headers={**headers, "Origin": "https://evil.example"}).status_code, 403)
        for invalid in ("x" * 10, "x" * 257):
            response = self.client.put("/api/password", json={**payload, "new_password": invalid}, headers=headers)
            self.assertEqual(response.status_code, 422)
            self.assertNotIn(invalid, response.text)
        response = self.client.put("/api/password", json={**payload, "current_password": "wrong"}, headers=headers)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get("/api/session").status_code, 200)
        response = self.client.put("/api/password", json=payload, headers=headers)
        self.assertEqual(response.status_code, 200)
        for password in payload.values():
            self.assertNotIn(password, response.text)
        self.assertEqual(self.client.get("/api/session").status_code, 401)
        self.assertEqual(self.client.get("/api/session", headers={"Cookie": "pet_session=" + old_token}).status_code, 401)
        self.assertEqual(self.client.post("/api/login", json={"password": PASSWORD}).status_code, 401)
        self.assertEqual(self.client.post("/api/login", json={"password": payload["new_password"]}).status_code, 200)
        self.assertEqual(self.client.get("/api/config").status_code, 200)
        self.assertTrue(Security(self.root / "data").verify_password(payload["new_password"]))
        self.assertIs(self.client.app.state.monitor.security, self.client.app.state.security)

    def test_password_change_rate_limits_incorrect_current_password(self):
        headers = self.login()
        for _ in range(5):
            response = self.client.put("/api/password", json={"current_password": "wrong", "new_password": "new-pass-11"}, headers=headers)
            self.assertEqual(response.status_code, 400)
        response = self.client.put("/api/password", json={"current_password": PASSWORD, "new_password": "new-pass-11"}, headers=headers)
        self.assertEqual(response.status_code, 429)
        self.assertTrue(Security(self.root / "data").verify_password(PASSWORD))

    def test_password_change_write_failure_preserves_password_and_session(self):
        headers = self.login()
        original = (self.root / "data" / "auth.json").read_bytes()
        with patch("million_pound.security.write_private", side_effect=OSError("disk unavailable")):
            response = self.client.put("/api/password", json={"current_password": PASSWORD, "new_password": "new-pass-11"}, headers=headers)
        self.assertEqual(response.status_code, 500)
        self.assertEqual((self.root / "data" / "auth.json").read_bytes(), original)
        self.assertTrue(self.client.app.state.security.verify_password(PASSWORD))
        self.assertEqual(self.client.get("/api/session").status_code, 200)

    def test_activity_photos_receive_matching_confirmed_events_across_pages(self):
        store = Store(self.root / "data")
        start = datetime.now(timezone.utc).replace(hour=4, minute=0, second=0, microsecond=0)
        ids = []
        for offset in (0, 900):
            when = start + timedelta(seconds=offset)
            activity_id = store.add_activity(when.isoformat(), (when + timedelta(seconds=10)).isoformat())
            store.update_activity(activity_id, "done", pets={})
            ids.append(activity_id)
        for when, kind, pet, status in ((start, "toileting", "dog", "confirmed"), (start, "destruction", "cat", "confirmed"),
                                        (start, "eating", "both", "rejected"), (start + timedelta(seconds=900), "eating", "both", "confirmed")):
            event_id = store.add(when.isoformat(), kind, .9, [], [], status)
            store.update(event_id, status=status, description="fixture", pet=pet)
        for index in range(55):
            when = start + timedelta(seconds=1800 + index * 10)
            event_id = store.add(when.isoformat(), "toileting", .9, [], [], "confirmed")
            store.update(event_id, status="confirmed", description="unrelated", pet="dog")
        store.db.close()
        self.login()
        params = {"day": (start + timedelta(hours=8)).date().isoformat(), "limit": 1}
        page = self.client.get("/api/activities", params=params).json()
        self.assertEqual(page["items"][0]["events"], [{"type": "eating", "pet": "both"}])
        self.assertEqual(page["labels"]["toileting"], "乱拉乱尿")
        page = self.client.get("/api/activities", params={**params, "before": page["next_before"]}).json()
        self.assertEqual(page["items"][0]["id"], ids[0])
        self.assertCountEqual(page["items"][0]["events"], [{"type": "toileting", "pet": "dog"}, {"type": "destruction", "pet": "cat"}])


class ModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_payloads_and_strict_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input"
            path.write_bytes(b"fixture")
            calls = []
            def handler(request):
                payload = json.loads(request.content)
                calls.append((request.url.path, payload))
                if request.url.path.endswith("systemone"):
                    answers = visibility_response()["answers"]
                    answers.update({key: {"noul": .1} for key in ("eating", "destruction", "toileting")})
                    answers.update({"dog": {"choice": "sleeping", "probabilities": {"sleeping": .9}},
                                    "cat": {"choice": "not_visible", "probabilities": {"not_visible": .5}}})
                    return httpx.Response(200, json={"answers": answers})
                return httpx.Response(200, json={"choices": [{"message": {"content": '```json\n{"events":[{"type":"eating","confirmed":false,"pet":"unknown","description":"证据不足"}]}\n```'}}]})
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                models = Models(client)
                cfg = Settings(model_url="http://model.local/v1")
                decision = await models.decide(cfg, path, path)
                self.assertEqual(decision["pets"]["dog"]["activity"], "sleeping")
                self.assertTrue(decision["pets"]["dog"]["identity_verified"])
                self.assertEqual(decision["pets"]["cat"]["activity"], "uncertain")
                result = await models.review(cfg, ["eating"], path, [i * 3 + 1.5 for i in range(20)])
                self.assertFalse(result[0].confirmed)
                with self.assertRaises(ModelError):
                    await models.review(cfg, ["destruction"], path, [i * 3 + 1.5 for i in range(20)])
            self.assertEqual(calls[0][0], "/v1/systemone")
            self.assertTrue(calls[0][1]["videos"][0].startswith("data:video/mp4;base64,"))
            self.assertTrue(calls[0][1]["images"][0].startswith("data:image/jpeg;base64,"))
            self.assertEqual(sum(path == "/v1/systemone" for path, _ in calls), 1)
            self.assertEqual(set(calls[0][1]["questions"]), {"dog_presence", "cat_presence", "eating", "destruction", "toileting", "dog", "cat"})
            self.assertIn("黑色", calls[0][1]["state"])
            self.assertNotIn("drinking", calls[0][1]["questions"]["dog"]["criteria"])
            self.assertNotIn("resting", calls[0][1]["questions"]["dog"]["criteria"])
            self.assertIn("睡觉和休息不区分", calls[0][1]["questions"]["dog"]["instructions"])
            self.assertIn("休息", calls[0][1]["questions"]["dog"]["criteria"]["sleeping"])
            self.assertIn("喝水", calls[0][1]["questions"]["dog"]["criteria"]["eating"])
            self.assertIn("百万", calls[0][1]["questions"]["dog"]["instructions"])
            self.assertIn("英镑", calls[0][1]["questions"]["cat"]["instructions"])
            self.assertEqual(calls[1][1]["model"], "swift-1.5")
            content = calls[1][1]["messages"][0]["content"]
            self.assertEqual([item["type"] for item in content], ["text", "image_url"])
            self.assertIn("4列5行", content[0]["text"])
            self.assertIn("第1至20帧", content[0]["text"])
            self.assertIn("从左到右、从上到下", content[0]["text"])

    async def decide_with_visibility(self, visibility, activities=None, previous=None):
        calls = []
        def handler(request):
            payload = json.loads(request.content)
            calls.append(payload)
            answers = dict(visibility["answers"])
            answers.update({key: {"noul": .1} for key in ("eating", "destruction", "toileting")})
            answers.update({"dog": {"choice": "sleeping", "probabilities": {"sleeping": .99}},
                            "cat": {"choice": "sleeping", "probabilities": {"sleeping": .99}}})
            answers.update(activities or {})
            return httpx.Response(200, json={"answers": answers})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "video.mp4"
            path.write_bytes(b"fixture")
            async with httpx.AsyncClient(trust_env=False, transport=httpx.MockTransport(handler)) as client:
                result = await Models(client).decide(Settings(), path, path, previous)
        self.assertEqual(len(calls), 1)
        return result, calls

    async def test_same_top_activity_continues_regardless_of_score(self):
        previous = {
            "dog": {"activity": "sleeping", "visibility": "present", "identity_verified": True},
            "cat": {"activity": "walking", "visibility": "present", "identity_verified": True},
        }
        for score in (.79, .2, .01, 0) * 4:
            with self.subTest(score=score):
                result, _ = await self.decide_with_visibility(visibility_response(), {
                    "dog": {"choice": "sleeping", "probabilities": {"sleeping": score}},
                    "cat": {"choice": "walking", "probabilities": {"walking": score / 2}},
                }, previous)
                self.assertEqual(result["pets"]["dog"]["activity"], "sleeping")
                self.assertEqual(result["pets"]["cat"]["activity"], "walking")
                for pet in ("dog", "cat"):
                    self.assertEqual(result["pets"][pet]["confidence"], score if pet == "dog" else score / 2)
                    self.assertTrue(result["pets"][pet]["continued"])
                previous = result["pets"]

    async def test_uncertain_activity_keeps_all_candidate_scores(self):
        probabilities = {"uncertain": .8, "not_visible": .1, "walking": .07, "sleeping": .03}
        result, _ = await self.decide_with_visibility(visibility_response(), {
            "dog": {"choice": "uncertain", "probabilities": probabilities}})
        decision = result["pets"]["dog"]
        self.assertEqual(decision["activity"], "uncertain")
        self.assertEqual(decision["proposed_activity"], "uncertain")
        self.assertEqual(decision["confidence"], .8)
        self.assertEqual(decision["probabilities"], probabilities)
        self.assertTrue(decision["identity_verified"])

    async def test_same_candidate_state_continues_even_when_model_selects_uncertain(self):
        for activity in ("sleeping", "eating", "walking", "playing", "other"):
            with self.subTest(activity=activity):
                previous = {"dog": {"activity": activity, "visibility": "present", "identity_verified": True}}
                probabilities = {"uncertain": .9, activity: .1}
                result, _ = await self.decide_with_visibility(visibility_response(), {
                    "dog": {"choice": "uncertain", "probabilities": probabilities}}, previous)
                decision = result["pets"]["dog"]
                self.assertEqual(decision["activity"], activity)
                self.assertTrue(decision["continued"])
                self.assertEqual(decision["proposed_activity"], "uncertain")
                self.assertEqual(decision["probabilities"], probabilities)

    async def test_different_candidate_state_is_suspected_without_inheriting_previous(self):
        previous = {"dog": {"activity": "sleeping", "visibility": "present", "identity_verified": True}}
        result, _ = await self.decide_with_visibility(visibility_response(), {
            "dog": {"choice": "uncertain", "probabilities": {"uncertain": .8, "walking": .15, "sleeping": .05}}}, previous)
        self.assertEqual(result["pets"]["dog"]["activity"], "uncertain")
        self.assertFalse(result["pets"]["dog"]["continued"])

    async def test_continuation_requires_same_pet_and_previously_verified_activity(self):
        for previous in ({}, {"cat": {"activity": "sleeping", "visibility": "present", "identity_verified": True}},
                         {"dog": {"activity": "sleeping"}},
                         {"dog": {"activity": "sleeping", "visibility": "absent", "identity_verified": False}},
                         {"dog": {"activity": "uncertain", "visibility": "present", "identity_verified": True}},
                         {"dog": {"activity": "walking", "visibility": "present", "identity_verified": True}}):
            with self.subTest(previous=previous):
                result, _ = await self.decide_with_visibility(visibility_response(), {
                    "dog": {"choice": "sleeping", "probabilities": {"sleeping": .1}}}, previous)
                self.assertEqual(result["pets"]["dog"]["activity"], "uncertain")
                self.assertFalse(result["pets"]["dog"]["continued"])

    async def test_continuation_stops_when_visibility_or_first_choice_changes(self):
        previous = {"cat": {"activity": "sleeping", "visibility": "present", "identity_verified": True}}
        for presence in (("absent", .95), ("absent", .6), ("uncertain", .95)):
            with self.subTest(presence=presence):
                result, calls = await self.decide_with_visibility(visibility_response(cat=presence), previous=previous)
                self.assertNotEqual(result["pets"]["cat"]["activity"], "sleeping")
                self.assertIn("cat", calls[0]["questions"])
                self.assertFalse(result["pets"]["cat"]["identity_verified"])
        for choice, score in (("uncertain", .9), ("not_visible", .95), ("walking", .2), ("walking", .9)):
            with self.subTest(choice=choice, score=score):
                result, _ = await self.decide_with_visibility(visibility_response(), {
                    "cat": {"choice": choice, "probabilities": {choice: score}}}, previous)
                self.assertEqual(result["pets"]["cat"]["activity"], "walking" if choice == "walking" and score == .9 else "uncertain")
                self.assertFalse(result["pets"]["cat"]["continued"])

    async def test_choice_must_have_the_highest_valid_score(self):
        previous = {"dog": {"activity": "sleeping", "visibility": "present", "identity_verified": True}}
        for probabilities in ({"sleeping": .2, "walking": .8}, {"sleeping": .2, "walking": True},
                              {"sleeping": .2, "walking": float("nan")}, {"sleeping": .2, "invalid": .1}):
            with self.subTest(probabilities=probabilities), self.assertRaises(ModelError):
                await self.decide_with_visibility(visibility_response(), {
                    "dog": {"choice": "sleeping", "probabilities": probabilities}}, previous)
        result, _ = await self.decide_with_visibility(visibility_response(), {
            "dog": {"choice": "sleeping", "probabilities": {"sleeping": .2, "walking": .2}}}, previous)
        self.assertEqual(result["pets"]["dog"]["activity"], "sleeping")
        self.assertTrue(result["pets"]["dog"]["continued"])

    async def test_absent_cat_never_inherits_dogs_sleep(self):
        result, calls = await self.decide_with_visibility(visibility_response(cat=("absent", .95)))
        self.assertEqual(result["pets"]["cat"]["activity"], "not_visible")
        self.assertFalse(result["pets"]["cat"]["identity_verified"])
        self.assertEqual(result["pets"]["dog"]["activity"], "sleeping")
        self.assertIn("cat", calls[0]["questions"])

    async def test_unclear_identity_is_not_classified_as_sleeping(self):
        for visibility in (("uncertain", .95), ("uncertain", .1)):
            with self.subTest(visibility=visibility):
                result, calls = await self.decide_with_visibility(visibility_response(cat=visibility))
                self.assertEqual(result["pets"]["cat"]["activity"], "uncertain")
                self.assertIn("cat", calls[0]["questions"])
                self.assertFalse(result["pets"]["cat"]["identity_verified"])

    async def test_presence_choice_has_no_confidence_threshold(self):
        for score in (0, .1, .6, .7, .79, .8, .99):
            for presence in ("present", "absent"):
                with self.subTest(presence=presence, score=score):
                    result, calls = await self.decide_with_visibility(visibility_response(cat=(presence, score)), {
                        "cat": {"choice": "walking", "probabilities": {"walking": .9}}})
                    decision = result["pets"]["cat"]
                    self.assertEqual(decision["activity"], "walking" if presence == "present" else "not_visible")
                    self.assertEqual(decision["identity_verified"], presence == "present")
                    self.assertEqual(decision["visibility_confidence"], score)
                    self.assertIn("cat", calls[0]["questions"])

    async def test_both_absent_ignore_activities_and_warnings_in_joint_response(self):
        result, calls = await self.decide_with_visibility(visibility_response(dog=("absent", .95), cat=("absent", .95)))
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(pet["activity"] == "not_visible" for pet in result["pets"].values()))
        self.assertTrue(all(score == 0 for score in result["scores"].values()))

    async def test_sleep_requires_stronger_score_and_visibility_must_agree(self):
        result, _ = await self.decide_with_visibility(visibility_response(), {
            "cat": {"choice": "sleeping", "probabilities": {"sleeping": .75}}})
        self.assertEqual(result["pets"]["cat"]["activity"], "uncertain")
        self.assertEqual(result["pets"]["cat"]["proposed_activity"], "sleeping")
        result, _ = await self.decide_with_visibility(visibility_response(), {
            "cat": {"choice": "not_visible", "probabilities": {"not_visible": .95}}})
        self.assertEqual(result["pets"]["cat"]["activity"], "uncertain")
        self.assertFalse(result["pets"]["cat"]["identity_verified"])

    async def test_invalid_visibility_does_not_confirm_presence(self):
        for score in (True, float("nan"), 1.1):
            with self.subTest(score=score), self.assertRaises(ModelError):
                await self.decide_with_visibility(visibility_response(cat=("present", score)))

    async def test_review_supports_eating_and_drinking_and_uses_pet_names(self):
        def handler(request):
            payload = json.loads(request.content)
            prompt = payload["messages"][0]["content"][0]["text"]
            self.assertIn("进食或饮水均可", prompt)
            response = {"events": [{"type": "eating", "confirmed": True, "pet": "both", "description": "拉布拉多犬和缅因猫在碗前吃喝"}]}
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(response)}}]})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "frame.jpg"
            path.write_bytes(b"fixture")
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                result = await Models(client).review(Settings(), ["eating"], path, [i * 3 + 1.5 for i in range(20)])
                self.assertEqual(result[0].description, "百万和英镑在碗前吃喝")

    async def test_invalid_probability_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "video"
            path.write_bytes(b"fixture")
            def handler(request):
                payload = json.loads(request.content)
                return httpx.Response(200, json=visibility_response() if "dog_presence" in payload["questions"] else {"answers": {}})
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with self.assertRaises(ModelError):
                    await Models(client).decide(Settings(), path, path)

    async def test_bark_normalizes_example_url_and_preserves_options(self):
        def handler(request):
            self.assertEqual(str(request.url), "https://bark.local/device-key")
            payload = json.loads(request.content)
            self.assertEqual(payload["title"], "事件")
            self.assertEqual(payload["body"], "正文")
            self.assertEqual(payload["group"], "example")
            self.assertEqual(payload["ttl"], "600")
            self.assertEqual(payload["image"], "https://image.local/signed")
            return httpx.Response(200, json={"code": 200})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await send_bark(client, "https://bark.local/device-key/title/body?group=example&ttl=600", "事件", "正文", "https://image.local/signed")


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        Security.set_password(self.root / "data", PASSWORD)
        self.config = Config(self.root)
        self.config.save(Settings(enabled=True, stream_url="rtsp://camera.local/feed", bark_url="https://bark.local/key", public_url="https://home.local"))
        self.store = Store(self.root / "data")
        self.client = httpx.AsyncClient(trust_env=False)
        self.monitor = Monitor(self.config, self.store, Security(self.root / "data"), self.root / "data", self.client)
        async def generate(source, target, parameter, *extra):
            target.write_bytes(b"fixture")
        async def collage(source, target, duration):
            target.write_bytes(b"collage")
            return [round(duration * (i + .5) / 20, 3) for i in range(20)]
        self.patches = [patch("million_pound.media.accelerate", side_effect=generate),
                        patch("million_pound.media.screenshot", side_effect=generate),
                        patch("million_pound.media.contact_sheet", side_effect=collage),
                        patch("million_pound.monitor.send_bark", new_callable=AsyncMock)]
        self.mocks = [p.start() for p in self.patches]
        self.monitor.models.decide = AsyncMock(return_value={
            "scores": {"eating": .9, "destruction": .9, "toileting": .1},
            "pets": {"dog": {"activity": "eating", "confidence": .9}, "cat": {"activity": "playing", "confidence": .9}},
        })
        self.monitor.models.review = AsyncMock(return_value=[
            ReviewEvent(type="eating", confirmed=True, pet="dog", description="正在进食"),
            ReviewEvent(type="destruction", confirmed=True, pet="cat", description="正在抓坏沙发"),
        ])

    async def asyncTearDown(self):
        for p in self.patches:
            p.stop()
        self.store.db.close()
        await self.client.aclose()
        self.tmp.cleanup()

    async def analyze(self, cfg=None, started=None):
        started = started or datetime.now(timezone.utc)
        cfg = cfg or self.config.current
        duration = cfg.clip_duration
        activity_id = self.store.add_activity(started.isoformat(), (started + timedelta(seconds=duration)).isoformat())
        name = f"{activity_id:032x}-1.jpg"
        (self.monitor.images / name).write_bytes(b"evidence")
        self.store.activity_images(activity_id, [name], [duration / 2])
        raw = self.root / f"raw-{activity_id}.mkv"
        raw.write_bytes(b"original")
        await self.monitor.analyze(cfg, raw, activity_id, started, duration)
        return activity_id

    async def test_state_context_uses_recent_verified_records_and_expires(self):
        self.monitor.models.decide.return_value["scores"] = {"eating": .1, "destruction": .1, "toileting": .1}
        base = datetime.now(timezone.utc) - timedelta(hours=1)
        for i, (status, gap) in enumerate((("done", 0), ("done", -.001), ("done", -.021), ("done", -.1),
                                            ("done", -.101), ("done", 1), ("done", 2), ("done", 60), ("done", 61), ("done", -1),
                                            ("error", 0), ("skipped", 0), ("cancelled", 0), ("pending", 0))):
            with self.subTest(status=status, gap=gap):
                start = base + timedelta(seconds=i * 300)
                prior_id = self.store.add_activity(start.isoformat(), (start + timedelta(seconds=10)).isoformat())
                pets = {"dog": {"activity": "sleeping", "visibility": "present", "identity_verified": True}}
                self.store.update_activity(prior_id, status, pets=pets)
                await self.analyze(started=start + timedelta(seconds=10 + gap))
                expected = pets if status == "done" and -.1 <= gap < 60 else {}
                self.assertEqual(self.monitor.models.decide.call_args.args[3], expected)

    async def test_short_hidden_gaps_keep_each_pets_own_state_as_context(self):
        self.monitor.models.decide.return_value["scores"] = {"eating": .1, "destruction": .1, "toileting": .1}
        start = datetime.now(timezone.utc) - timedelta(hours=1)
        pets = {"dog": {"activity": "walking", "visibility": "present", "identity_verified": True},
                "cat": {"activity": "sleeping", "visibility": "present", "identity_verified": True}}
        first = self.store.add_activity(start.isoformat(), (start + timedelta(seconds=10)).isoformat())
        self.store.update_activity(first, "done", pets=pets)
        gap = self.store.add_activity((start + timedelta(seconds=10)).isoformat(), (start + timedelta(seconds=20)).isoformat())
        self.store.update_activity(gap, "error")
        await self.analyze(started=start + timedelta(seconds=20))
        self.assertEqual(self.monitor.models.decide.call_args.args[3], pets)

    async def test_record_mode_notification_and_review_interval(self):
        cfg = self.config.current.model_copy(update={"review_interval": 60})
        await self.analyze(cfg)
        rows = self.store.recent()
        self.assertEqual({r["type"]: r["notification"] for r in rows}, {"eating": "record_only", "destruction": "sent"})
        self.mocks[-1].assert_awaited_once()
        image_url = self.mocks[-1].call_args.args[-1]
        self.assertTrue(image_url.startswith("https://home.local/shared/"))
        self.assertEqual(rows[0]["frame_offsets"], [6.0, 30.0, 54.0])
        review_args = self.monitor.models.review.call_args.args
        self.assertIsInstance(review_args[2], Path)
        self.assertEqual(review_args[2].name, "review.jpg")
        self.assertEqual(review_args[3], [i * 3 + 1.5 for i in range(20)])
        self.mocks[2].assert_awaited_once()
        await self.analyze(cfg)
        self.assertEqual({r["status"] for r in self.store.recent()[:2]}, {"unreviewed"})
        self.monitor.models.review.assert_awaited_once()
        self.mocks[2].assert_awaited_once()
        self.assertFalse(list((self.root / "data").glob("clip-*")))

    async def test_continuous_event_is_not_notified_twice(self):
        cfg = self.config.current.model_copy(update={"review_interval": 0, "public_url": ""})
        await self.analyze(cfg)
        await self.analyze(cfg)
        self.mocks[-1].assert_awaited_once()
        self.assertEqual(self.store.recent()[0]["notification"], "deduplicated")

    async def test_no_candidate_does_not_call_review(self):
        self.monitor.models.decide.return_value["scores"] = {"eating": .1, "destruction": .2, "toileting": .3}
        activity_id = await self.analyze()
        self.monitor.models.review.assert_not_awaited()
        self.mocks[2].assert_not_awaited()
        self.mocks[-1].assert_not_awaited()
        self.assertEqual(self.store.recent(), [])
        activity = self.store.get_activity(activity_id)
        self.assertEqual(activity["status"], "done")
        self.assertEqual(activity["pets"]["dog"]["activity"], "eating")
        self.assertEqual(len(activity["images"]), 1)

    async def test_keeps_the_exact_video_sent_to_d3_after_analysis(self):
        sent = []
        result = self.monitor.models.decide.return_value
        result["scores"] = {"eating": .1, "destruction": .1, "toileting": .1}
        async def decide(cfg, video, snapshot, prior):
            sent.append(video.read_bytes())
            return result
        self.monitor.models.decide.side_effect = decide
        activity_id = await self.analyze()
        image = self.monitor.images / self.store.get_activity(activity_id)["images"][0]
        self.assertEqual(image.with_suffix(".mp4").read_bytes(), sent[0])
        self.assertFalse(list((self.root / "data").glob("clip-*")))

    async def test_failed_decision_does_not_keep_a_video_or_call_review(self):
        self.monitor.models.decide.side_effect = ModelError("模型失败")
        with self.assertRaises(ModelError):
            await self.analyze()
        self.assertFalse(list(self.monitor.images.glob("*.mp4")))
        self.assertFalse(list((self.root / "data").glob("clip-*")))
        self.monitor.models.review.assert_not_awaited()

    async def test_deleting_media_during_analysis_does_not_recreate_files_or_share_images(self):
        decision = self.monitor.models.decide.return_value
        reviews = self.monitor.models.review.return_value
        for stage in ("accelerate", "decide", "screenshot", "review"):
            with self.subTest(stage=stage):
                def remove():
                    activity = self.store.activities()[0]
                    self.store.delete_media("activities", [activity["id"]], self.monitor.images)

                async def accelerate(source, target, *args):
                    target.write_bytes(b"video")
                    if stage == "accelerate":
                        remove()

                async def screenshot(source, target, *args):
                    target.write_bytes(b"image")
                    if stage == "screenshot":
                        remove()

                async def decide(*args):
                    if stage == "decide":
                        remove()
                    return decision

                async def review(*args):
                    remove()
                    return reviews

                self.mocks[0].side_effect = accelerate
                self.mocks[1].side_effect = screenshot
                self.monitor.models.decide.side_effect = decide
                self.monitor.models.review.side_effect = review
                activity = await self.analyze()
                self.assertEqual(self.store.get_activity(activity)["images"], [])
                self.assertEqual(list(self.monitor.images.iterdir()), [])
                self.assertFalse(list((self.root / "data").glob("clip-*")))
                for event in self.store.recent():
                    self.assertEqual(event["images"], [])
                if stage == "review":
                    self.mocks[-1].assert_awaited_once()
                    self.assertIsNone(self.mocks[-1].call_args.args[-1])

    async def test_failed_review_is_recorded_without_notification(self):
        self.monitor.models.review.side_effect = ModelError("模型失败")
        with self.assertRaises(ModelError):
            await self.analyze()
        self.assertEqual({r["status"] for r in self.store.recent()}, {"review_error"})
        self.assertGreater(self.store.meta("last_review"), 0)
        self.mocks[-1].assert_not_awaited()

    async def test_each_special_event_uses_one_review(self):
        for kind in ("eating", "destruction", "toileting"):
            with self.subTest(kind=kind):
                self.monitor.models.decide.return_value["scores"] = {
                    key: .9 if key == kind else .1 for key in ("eating", "destruction", "toileting")}
                self.monitor.models.review.return_value = [ReviewEvent(
                    type=kind, confirmed=False, pet="unknown", description="证据不足")]
                self.monitor.models.review.reset_mock()
                await self.analyze()
                self.monitor.models.review.assert_awaited_once()
                self.assertEqual(self.monitor.models.review.call_args.args[1], [kind])
                self.assertEqual(self.store.recent()[0]["status"], "rejected")
        self.mocks[-1].assert_not_awaited()

    async def test_failed_collage_does_not_call_review_or_notify(self):
        self.mocks[2].side_effect = MediaError("无法生成20帧复核拼图")
        with self.assertRaises(MediaError):
            await self.analyze()
        self.assertEqual({r["status"] for r in self.store.recent()}, {"review_error"})
        self.monitor.models.review.assert_not_awaited()
        self.mocks[-1].assert_not_awaited()

    async def test_review_cancellation_keeps_activity_video_and_marks_candidates(self):
        entered = asyncio.Event()
        async def wait(*args):
            entered.set()
            await asyncio.Event().wait()
        self.monitor.models.review.side_effect = wait
        task = asyncio.create_task(self.analyze())
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual({r["status"] for r in self.store.recent()}, {"cancelled"})
        self.assertFalse(list((self.root / "data").glob("clip-*")))
        self.assertEqual(len(list(self.monitor.images.glob("*.mp4"))), 1)
        self.mocks[-1].assert_not_awaited()

    async def test_capture_continues_during_model_wait_and_cleans_pending(self):
        deciding, produced = asyncio.Event(), asyncio.Event()
        async def decision(*args):
            deciding.set()
            await asyncio.Event().wait()
        async def source(url, directory, seconds):
            for i in range(2):
                raw = directory / f"part-{i:06d}.mkv"
                raw.write_bytes(b"raw")
                yield raw, i * 10, (i + 1) * 10
                if i == 0:
                    await deciding.wait()
            produced.set()
            await asyncio.Event().wait()
        self.monitor.models.decide.side_effect = decision
        with patch("million_pound.media.segments", source):
            task = asyncio.create_task(self.monitor.capture(self.config.current))
            await asyncio.wait_for(produced.wait(), 2)
            self.assertEqual(len(self.store.activities()), 2)
            self.assertEqual(self.monitor.status["queued"], 1)
            self.assertTrue(all(len(row["images"]) == 1 for row in self.store.activities()))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        rows = self.store.activities()
        self.assertEqual({r["status"] for r in rows}, {"cancelled"})
        self.assertEqual(rows[1]["ended_at"], rows[0]["started_at"])
        self.assertFalse(list((self.root / "data").glob("capture-*")))

    async def test_backlog_keeps_evidence_without_inventing_activity(self):
        deciding, produced = asyncio.Event(), asyncio.Event()
        async def decision(*args):
            deciding.set()
            await asyncio.Event().wait()
        async def source(url, directory, seconds):
            for i in range(35):
                raw = directory / f"part-{i:06d}.mkv"
                raw.write_bytes(b"raw")
                yield raw, i * 10, (i + 1) * 10
                if i == 0:
                    await deciding.wait()
            produced.set()
            await asyncio.Event().wait()
        self.monitor.models.decide.side_effect = decision
        with patch("million_pound.media.segments", source):
            task = asyncio.create_task(self.monitor.capture(self.config.current))
            await asyncio.wait_for(produced.wait(), 2)
            skipped = [row for row in self.store.activities() if row["status"] == "skipped"]
            self.assertEqual(len(skipped), 4)
            self.assertTrue(all(row["images"] and row["pets"] == {} for row in skipped))
            self.assertEqual(self.monitor.status["queued"], 30)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_pause_waits_for_cleanup_instead_of_cancelling_it_twice(self):
        entered, cleaning, release, finished = (asyncio.Event() for _ in range(4))
        async def capture(cfg):
            self.monitor.status["recording"] = True
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
                self.monitor.status["recording"] = False
                finished.set()
        self.monitor.capture = capture
        self.monitor.task = asyncio.create_task(self.monitor.run())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            self.config.save(self.config.current.model_copy(update={"enabled": False}))
            self.monitor.changed()
            await asyncio.wait_for(cleaning.wait(), 2)
            await asyncio.sleep(.02)
            release.set()
            await asyncio.wait_for(finished.wait(), 2)
        finally:
            release.set()
            await self.monitor.close()
        self.assertFalse(self.monitor.status["recording"])

    async def test_unfinished_segment_keeps_a_picture_when_stopping(self):
        entered = asyncio.Event()
        async def source(url, directory, seconds):
            raw = directory / "part-000000.mkv"
            raw.write_bytes(b"partial-video")
            entered.set()
            await asyncio.Event().wait()
            yield raw, 0, 3
        with patch("million_pound.media.segments", source), \
                patch("million_pound.media.probe_duration", new_callable=AsyncMock, return_value=3):
            task = asyncio.create_task(self.monitor.capture(self.config.current))
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        row = self.store.activities()[0]
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(len(row["images"]), 1)
        self.assertEqual(row["pets"], {})


if __name__ == "__main__":
    unittest.main()
