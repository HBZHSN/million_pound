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
from million_pound.models import ModelError, Models, ReviewEvent, send_bark
from million_pound.monitor import Monitor
from million_pound.security import Security
from million_pound.store import Store


PASSWORD = "test-password-with-12-characters"


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
        for value in ({"clip_duration": 0}, {"speed": 0}, {"enabled": "false"},
                      {"stream_url": "file:///etc/passwd"}, {"event_modes": {"eating": "notify"}},
                      {"model_url": "http://example.com/v1/chat/completions"},
                      {"public_url": "https://example.com/path"}, {"bark_url": "https://api.day.app"}):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                Settings(**value)


class LocalSetup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        Security.set_password(self.root / "data", PASSWORD)
        self.security = Security(self.root / "data")

    def tearDown(self):
        self.tmp.cleanup()


class LocalTests(LocalSetup):
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
        for path in ("/api/config", "/api/events", "/api/status", "/api/preview"):
            self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.client.get("/", follow_redirects=False).status_code, 303)
        self.assertEqual(self.client.get("/docs").status_code, 404)
        self.assertEqual(self.client.post("/api/login", json={"password": "wrong"}).status_code, 401)
        headers = self.login()
        response = self.client.get("/api/config")
        for secret in ("private-model-key", "private-bark-key", "private-camera-password"):
            self.assertNotIn(secret, response.text)
        self.assertTrue(response.json()["stream_url_configured"])
        self.assertEqual(self.client.put("/api/config", json={"sample_interval": 90}).status_code, 403)
        self.assertEqual(self.client.put("/api/config", json={"sample_interval": 90}, headers=headers).status_code, 200)
        restored = Config(self.root).current
        self.assertEqual(restored.sample_interval, 90)
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

    def test_login_rejects_foreign_origin_and_rate_limits(self):
        self.assertEqual(self.client.post("/api/login", json={"password": PASSWORD}, headers={"Origin": "https://evil.example"}).status_code, 403)
        for _ in range(5):
            self.assertEqual(self.client.post("/api/login", json={"password": "wrong"}).status_code, 401)
        self.assertEqual(self.client.post("/api/login", json={"password": PASSWORD}).status_code, 429)


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
                    return httpx.Response(200, json={"answers": {key: {"noul": .1} for key in ("eating", "destruction", "toileting")}})
                return httpx.Response(200, json={"choices": [{"message": {"content": '```json\n{"events":[{"type":"eating","confirmed":false,"pet":"unknown","description":"证据不足"}]}\n```'}}]})
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                models = Models(client)
                cfg = Settings(model_url="http://model.local/v1")
                await models.decide(cfg, path)
                result = await models.review(cfg, ["eating"], [path], [1])
                self.assertFalse(result[0].confirmed)
                with self.assertRaises(ModelError):
                    await models.review(cfg, ["destruction"], [path], [1])
            self.assertEqual(calls[0][0], "/v1/systemone")
            self.assertTrue(calls[0][1]["videos"][0].startswith("data:video/mp4;base64,"))
            self.assertEqual(set(calls[0][1]["questions"]), {"eating", "destruction", "toileting"})
            self.assertEqual(calls[1][1]["model"], "swift-1.5")

    async def test_invalid_probability_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "video"
            path.write_bytes(b"fixture")
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"answers": {}}))) as client:
                with self.assertRaises(ModelError):
                    await Models(client).decide(Settings(), path)

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
        async def record(source, target, seconds):
            target.write_bytes(b"original")
            return float(seconds)
        async def generate(source, target, parameter):
            target.write_bytes(b"fixture")
        self.patches = [patch("million_pound.media.record", side_effect=record),
                        patch("million_pound.media.accelerate", side_effect=generate),
                        patch("million_pound.media.screenshot", side_effect=generate),
                        patch("million_pound.monitor.send_bark", new_callable=AsyncMock)]
        self.mocks = [p.start() for p in self.patches]
        self.monitor.models.decide = AsyncMock(return_value={"eating": .9, "destruction": .9, "toileting": .1})
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

    async def test_record_mode_notification_and_review_interval(self):
        await self.monitor.cycle(self.config.current)
        rows = self.store.recent()
        self.assertEqual({r["type"]: r["notification"] for r in rows}, {"eating": "record_only", "destruction": "sent"})
        self.mocks[-1].assert_awaited_once()
        image_url = self.mocks[-1].call_args.args[-1]
        self.assertTrue(image_url.startswith("https://home.local/shared/"))
        self.assertEqual(rows[0]["frame_offsets"], [1.0, 5.0, 9.0])
        await self.monitor.cycle(self.config.current)
        self.assertEqual({r["status"] for r in self.store.recent()[:2]}, {"unreviewed"})
        self.monitor.models.review.assert_awaited_once()
        self.assertFalse(list((self.root / "data").glob("clip-*")))

    async def test_continuous_event_is_not_notified_twice(self):
        cfg = self.config.current.model_copy(update={"review_interval": 0, "public_url": ""})
        await self.monitor.cycle(cfg)
        await self.monitor.cycle(cfg)
        self.mocks[-1].assert_awaited_once()
        self.assertEqual(self.store.recent()[0]["notification"], "deduplicated")

    async def test_no_candidate_does_not_call_review(self):
        self.monitor.models.decide.return_value = {"eating": .1, "destruction": .2, "toileting": .3}
        await self.monitor.cycle(self.config.current)
        self.monitor.models.review.assert_not_awaited()
        self.assertEqual(self.store.recent(), [])

    async def test_failed_review_is_recorded_without_notification(self):
        self.monitor.models.review.side_effect = ModelError("模型失败")
        with self.assertRaises(ModelError):
            await self.monitor.cycle(self.config.current)
        self.assertEqual({r["status"] for r in self.store.recent()}, {"review_error"})
        self.assertGreater(self.store.meta("last_review"), 0)
        self.mocks[-1].assert_not_awaited()

    async def test_cancellation_cleans_video_and_marks_candidates(self):
        entered = asyncio.Event()
        async def wait(*args):
            entered.set()
            await asyncio.Event().wait()
        self.monitor.models.review.side_effect = wait
        task = asyncio.create_task(self.monitor.cycle(self.config.current))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual({r["status"] for r in self.store.recent()}, {"cancelled"})
        self.assertFalse(list((self.root / "data").glob("clip-*")))
        self.mocks[-1].assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
