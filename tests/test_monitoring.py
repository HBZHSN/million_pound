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

    def test_activity_pagination_and_evidence_retention(self):
        store = Store(self.root / "data")
        images = self.root / "data" / "images"
        images.mkdir()
        old = datetime.now(timezone.utc) - timedelta(days=8)
        name = "c" * 32 + "-1.jpg"
        path = images / name
        path.write_bytes(b"proof")
        import os
        os.utime(path, (old.timestamp(), old.timestamp()))
        for i in range(55):
            when = old + timedelta(seconds=i * 10)
            activity_id = store.add_activity(when.isoformat(), (when + timedelta(seconds=10)).isoformat())
            store.activity_images(activity_id, [name], [5])
            store.update_activity(activity_id, "done", pets={"dog": {"activity": "resting", "confidence": .9}})
        first = store.activities()
        second = store.activities(before=first[-1]["id"])
        self.assertEqual((len(first), len(second)), (50, 5))
        self.assertFalse({row["id"] for row in first} & {row["id"] for row in second})
        store.cleanup(images, 7)
        self.assertFalse(path.exists())
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

    def test_login_rejects_foreign_origin_and_rate_limits(self):
        self.assertEqual(self.client.post("/api/login", json={"password": PASSWORD}, headers={"Origin": "https://evil.example"}).status_code, 403)
        for _ in range(5):
            self.assertEqual(self.client.post("/api/login", json={"password": "wrong"}).status_code, 401)
        self.assertEqual(self.client.post("/api/login", json={"password": PASSWORD}).status_code, 429)

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
        self.assertEqual(self.client.get("/api/activities", params={"day": beijing_day, "hour": 1}).json()["items"], [])
        self.assertEqual(self.client.get("/api/activities?hour=24").status_code, 422)


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
                    if "dog_presence" in payload["questions"]:
                        return httpx.Response(200, json=visibility_response())
                    answers = {key: {"noul": .1} for key in ("eating", "destruction", "toileting")}
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
                result = await models.review(cfg, ["eating"], [path], [1])
                self.assertFalse(result[0].confirmed)
                with self.assertRaises(ModelError):
                    await models.review(cfg, ["destruction"], [path], [1])
            self.assertEqual(calls[0][0], "/v1/systemone")
            self.assertTrue(calls[0][1]["videos"][0].startswith("data:video/mp4;base64,"))
            self.assertTrue(calls[0][1]["images"][0].startswith("data:image/jpeg;base64,"))
            self.assertEqual(set(calls[0][1]["questions"]), {"dog_presence", "cat_presence"})
            self.assertIn("黑色", calls[0][1]["state"])
            self.assertEqual(set(calls[1][1]["questions"]), {"eating", "destruction", "toileting", "dog", "cat"})
            self.assertNotIn("drinking", calls[1][1]["questions"]["dog"]["criteria"])
            self.assertIn("喝水", calls[1][1]["questions"]["dog"]["criteria"]["eating"])
            self.assertIn("百万", calls[1][1]["questions"]["dog"]["instructions"])
            self.assertIn("英镑", calls[1][1]["questions"]["cat"]["instructions"])
            self.assertEqual(calls[2][1]["model"], "swift-1.5")

    async def decide_with_visibility(self, visibility, activities=None):
        calls = []
        def handler(request):
            payload = json.loads(request.content)
            calls.append(payload)
            if "dog_presence" in payload["questions"]:
                return httpx.Response(200, json=visibility)
            answers = {key: {"noul": .1} for key in ("eating", "destruction", "toileting")}
            answers.update({"dog": {"choice": "sleeping", "probabilities": {"sleeping": .99}},
                            "cat": {"choice": "sleeping", "probabilities": {"sleeping": .99}}})
            answers.update(activities or {})
            return httpx.Response(200, json={"answers": answers})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "video.mp4"
            path.write_bytes(b"fixture")
            async with httpx.AsyncClient(trust_env=False, transport=httpx.MockTransport(handler)) as client:
                result = await Models(client).decide(Settings(), path, path)
        return result, calls

    async def test_absent_cat_never_inherits_dogs_sleep(self):
        result, calls = await self.decide_with_visibility(visibility_response(cat=("absent", .95)))
        self.assertEqual(result["pets"]["cat"]["activity"], "not_visible")
        self.assertFalse(result["pets"]["cat"]["identity_verified"])
        self.assertEqual(result["pets"]["dog"]["activity"], "sleeping")
        self.assertNotIn("cat", calls[1]["questions"])
        self.assertLessEqual(len(calls[1]["questions"]), 6)

    async def test_unclear_identity_is_not_classified_as_sleeping(self):
        for visibility in (("present", .7), ("present", .79), ("uncertain", .95), ("absent", .6)):
            with self.subTest(visibility=visibility):
                result, calls = await self.decide_with_visibility(visibility_response(cat=visibility))
                self.assertEqual(result["pets"]["cat"]["activity"], "uncertain")
                self.assertNotIn("cat", calls[1]["questions"])

    async def test_visible_cat_is_not_suppressed_by_presence_gate(self):
        result, calls = await self.decide_with_visibility(visibility_response(cat=("present", .8)), {
            "cat": {"choice": "walking", "probabilities": {"walking": .9}}})
        self.assertEqual(result["pets"]["cat"]["activity"], "walking")
        self.assertTrue(result["pets"]["cat"]["identity_verified"])
        self.assertIn("cat", calls[1]["questions"])

    async def test_both_absent_skip_activity_and_warning_calls(self):
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
                result = await Models(client).review(Settings(), ["eating"], [path], [1])
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
        self.patches = [patch("million_pound.media.accelerate", side_effect=generate),
                        patch("million_pound.media.screenshot", side_effect=generate),
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

    async def analyze(self, cfg=None):
        started = datetime.now(timezone.utc)
        activity_id = self.store.add_activity(started.isoformat(), (started + timedelta(seconds=10)).isoformat())
        name = f"{activity_id:032x}-1.jpg"
        (self.monitor.images / name).write_bytes(b"evidence")
        self.store.activity_images(activity_id, [name], [5])
        raw = self.root / f"raw-{activity_id}.mkv"
        raw.write_bytes(b"original")
        await self.monitor.analyze(cfg or self.config.current, raw, activity_id, started, 10)
        return activity_id

    async def test_record_mode_notification_and_review_interval(self):
        await self.analyze()
        rows = self.store.recent()
        self.assertEqual({r["type"]: r["notification"] for r in rows}, {"eating": "record_only", "destruction": "sent"})
        self.mocks[-1].assert_awaited_once()
        image_url = self.mocks[-1].call_args.args[-1]
        self.assertTrue(image_url.startswith("https://home.local/shared/"))
        self.assertEqual(rows[0]["frame_offsets"], [1.0, 5.0, 9.0])
        await self.analyze()
        self.assertEqual({r["status"] for r in self.store.recent()[:2]}, {"unreviewed"})
        self.monitor.models.review.assert_awaited_once()
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
        self.assertEqual(self.store.recent(), [])
        activity = self.store.get_activity(activity_id)
        self.assertEqual(activity["status"], "done")
        self.assertEqual(activity["pets"]["dog"]["activity"], "eating")
        self.assertEqual(len(activity["images"]), 1)

    async def test_failed_review_is_recorded_without_notification(self):
        self.monitor.models.review.side_effect = ModelError("模型失败")
        with self.assertRaises(ModelError):
            await self.analyze()
        self.assertEqual({r["status"] for r in self.store.recent()}, {"review_error"})
        self.assertGreater(self.store.meta("last_review"), 0)
        self.mocks[-1].assert_not_awaited()

    async def test_cancellation_cleans_video_and_marks_candidates(self):
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
