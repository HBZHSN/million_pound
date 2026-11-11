import asyncio
import shutil
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import media
from .config import BEIJING, EVENTS, in_schedule
from .models import ModelError, Models, send_bark


class Monitor:
    def __init__(self, config, store, security, data: Path, client):
        self.config, self.store, self.security = config, store, security
        self.data, self.client = data, client
        for prefix in ("capture-", "clip-", "preview-"):
            for old in data.glob(prefix + "*"):
                if old.is_dir() and not old.is_symlink():
                    shutil.rmtree(old)
        self.images = data / "images"
        self.images.mkdir(mode=0o700, exist_ok=True)
        self.models = Models(client)
        self.wake = asyncio.Event()
        self.capture_lock = asyncio.Lock()
        self.session_task = None
        self.task = None
        self.stopping = False
        self.status = {"phase": "starting", "last_error": None, "last_started": None,
                       "last_finished": None, "last_capture": None, "last_scores": None,
                       "recording": False, "queued": 0, "skipped": 0}

    def changed(self):
        if self.session_task and not self.session_task.done() and not self.session_task.cancelling():
            self.session_task.cancel()
        self.wake.set()

    async def close(self):
        self.stopping = True
        self.changed()
        if self.task:
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    async def run(self):
        retry_at, cleaned = 0, 0
        try:
            while not self.stopping:
                self.wake.clear()
                cfg = self.config.current.model_copy(deep=True)
                if self.session_task and self.session_task.done():
                    try:
                        await self.session_task
                    except asyncio.CancelledError:
                        retry_at = 0
                    except Exception as exc:
                        self.status.update(phase="error", last_error=self.error_message(exc))
                        retry_at = time.monotonic() + 10
                    self.session_task = None
                phase = ("disabled" if not cfg.enabled else "needs_camera" if not cfg.stream_url
                         else "outside_schedule" if not in_schedule(cfg.windows, datetime.now(timezone.utc)) else None)
                if phase:
                    if self.session_task:
                        if not self.session_task.cancelling():
                            self.session_task.cancel()
                        await asyncio.gather(self.session_task, return_exceptions=True)
                        self.session_task = None
                    self.status["phase"] = phase
                    retry_at = 0
                elif self.session_task is None and time.monotonic() >= retry_at:
                    self.status.update(phase="recording", last_error=None)
                    self.session_task = asyncio.create_task(self.capture(cfg))
                if time.monotonic() - cleaned > 3600:
                    self.store.cleanup(self.images, cfg.retention_days)
                    cleaned = time.monotonic()
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=1)
                except TimeoutError:
                    pass
        finally:
            if self.session_task:
                if not self.session_task.cancelling():
                    self.session_task.cancel()
                await asyncio.gather(self.session_task, return_exceptions=True)
            self.status.update(recording=False, queued=0)

    @staticmethod
    def error_message(exc):
        return str(exc) if isinstance(exc, (media.MediaError, ModelError)) else "处理失败，请检查本地磁盘和配置"

    async def capture(self, cfg):
        if shutil.disk_usage(self.data).free < 1024**3:
            raise media.MediaError("可用空间不足 1GB，已暂停采集，请清理磁盘")
        with tempfile.TemporaryDirectory(prefix="capture-", dir=self.data) as work:
            directory = Path(work)
            queue = asyncio.Queue(maxsize=30)
            pending = set()
            stream = media.segments(cfg.stream_url, directory, cfg.clip_duration)
            last_closed = None

            async def produce():
                nonlocal last_closed
                origin = None
                async for raw, start, end in stream:
                    last_closed = raw.name
                    if origin is None:
                        # shortcut: 按首段落盘时间估算采集时段，需秒级精度时改用摄像头时间戳。
                        origin = datetime.fromtimestamp(raw.stat().st_mtime - end, timezone.utc)
                    started = origin + timedelta(seconds=start)
                    ended = origin + timedelta(seconds=end)
                    activity_id = self.store.add_activity(started.isoformat(), ended.isoformat())
                    pending.add(activity_id)
                    self.status.update(last_started=started.isoformat(), last_capture=ended.isoformat())
                    if shutil.disk_usage(self.data).free < 1024**3:
                        raise media.MediaError("可用空间不足 1GB，已暂停采集，请清理磁盘")
                    duration = end - start
                    name = f"{uuid.uuid4().hex}-1.jpg"
                    try:
                        await media.screenshot(raw, self.images / name, round(duration / 2, 3))
                        self.store.activity_images(activity_id, [name], [round(duration / 2, 3)])
                    except (media.MediaError, OSError) as exc:
                        self.store.update_activity(activity_id, "error", message=self.error_message(exc))
                        pending.discard(activity_id)
                        raw.unlink(missing_ok=True)
                        self.status["last_error"] = self.error_message(exc)
                        continue
                    # shortcut: 最多缓存 30 段或 256MiB，持续积压时需降低抽帧率或扩充模型算力。
                    if queue.full() or sum(p.stat().st_size for p in directory.glob("part-*.mkv")) > 256 * 1024**2:
                        self.store.update_activity(activity_id, "skipped", message="分析积压，本段仅保留截图，未判断活动")
                        pending.discard(activity_id)
                        raw.unlink(missing_ok=True)
                        self.status["skipped"] += 1
                    else:
                        queue.put_nowait((raw, activity_id, started, duration))
                    self.status["queued"] = queue.qsize()
                raise media.MediaError("连续视频流已结束，正在重新连接")

            async def consume():
                while True:
                    raw, activity_id, started, duration = await queue.get()
                    self.status["queued"] = queue.qsize()
                    try:
                        await self.analyze(cfg, raw, activity_id, started, duration)
                        self.status["last_finished"] = datetime.now(timezone.utc).isoformat()
                    except (media.MediaError, ModelError, OSError) as exc:
                        if self.store.get_activity(activity_id)["status"] == "pending":
                            self.store.update_activity(activity_id, "error", message=self.error_message(exc))
                        self.status["last_error"] = self.error_message(exc)
                    finally:
                        raw.unlink(missing_ok=True)
                        pending.discard(activity_id)
                        if self.store.get_activity(activity_id)["status"] == "pending":
                            self.store.update_activity(activity_id, "cancelled", message="本段判断已中断")

            self.status["recording"] = True
            tasks = [asyncio.create_task(produce()), asyncio.create_task(consume())]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await stream.aclose()
                last = max(directory.glob("part-*.mkv"), default=None)
                if last is not None and (last_closed is None or last.name > last_closed):
                    try:
                        duration = await media.probe_duration(last)
                        if duration <= cfg.clip_duration + 2:
                            ended = datetime.fromtimestamp(last.stat().st_mtime, timezone.utc)
                            activity_id = self.store.add_activity((ended - timedelta(seconds=duration)).isoformat(), ended.isoformat())
                            pending.add(activity_id)
                            name = f"{uuid.uuid4().hex}-1.jpg"
                            await media.screenshot(last, self.images / name, round(duration / 2, 3))
                            self.store.activity_images(activity_id, [name], [round(duration / 2, 3)])
                    except (media.MediaError, OSError):
                        pass
                for activity_id in pending:
                    if self.store.get_activity(activity_id)["status"] == "pending":
                        self.store.update_activity(activity_id, "cancelled", message="采集停止，本段未完成判断")
                self.status.update(recording=False, queued=0)

    async def analyze(self, cfg, raw, activity_id, started, duration):
        with tempfile.TemporaryDirectory(prefix="clip-", dir=self.data) as work:
            fast = Path(work) / "fast.mp4"
            self.status["phase"] = "deciding"
            await media.accelerate(raw, fast, cfg.speed, cfg.sample_fps)
            center = self.store.get_activity(activity_id)["images"][0]
            previous = self.store.previous_activity(activity_id)
            previous_pets = {}
            if previous and previous["status"] == "done":
                gap = (started - datetime.fromisoformat(previous["ended_at"])).total_seconds()
                if 0 <= gap <= 1:
                    previous_pets = previous["pets"]
            result = await self.models.decide(cfg, fast, self.images / center, previous_pets)
            scores = result["scores"]
            self.store.update_activity(activity_id, "done", pets=result["pets"])
            self.status["last_scores"] = scores
            candidates = [key for key, score in scores.items() if score >= 0.7]
            if not candidates:
                self.status["phase"] = "no_event"
                return
            offsets = [round(duration * fraction, 3) for fraction in (0.1, 0.5, 0.9)]
            prefix = center.rsplit("-", 1)[0]
            paths = [self.images / f"{prefix}-{i}.jpg" for i in range(3)]
            for i in (0, 2):
                await media.screenshot(raw, paths[i], offsets[i])
            eligible = time.time() - self.store.meta("last_review") >= cfg.review_interval
            ids = {key: self.store.add(started.isoformat(), key, scores[key], [p.name for p in paths], offsets,
                                       "reviewing" if eligible else "unreviewed") for key in candidates}
            if not eligible:
                self.status["phase"] = "review_cooldown"
                return
            self.store.set_meta("last_review", time.time())
            self.status["phase"] = "reviewing"
            try:
                results = await self.models.review(cfg, candidates, paths, offsets)
                for event in results:
                    event_id = ids[event.type]
                    status = "confirmed" if event.confirmed else "rejected"
                    notification = "none"
                    if event.confirmed:
                        if cfg.event_modes[event.type] == "record":
                            notification = "record_only"
                        elif self.store.recently_notified(event.type, event.pet, datetime.now(timezone.utc)):
                            notification = "deduplicated"
                        elif not cfg.bark_url:
                            notification = "not_configured"
                        else:
                            notification = "sending"
                    self.store.update(event_id, status=status, description=event.description,
                                      pet=event.pet, notification=notification)
                    if notification == "sending":
                        image_url = None
                        if cfg.public_url:
                            expiry = int(time.time()) + 3600
                            name = paths[1].name
                            sig = self.security.image_signature(name, expiry)
                            image_url = f"{cfg.public_url}/shared/{name}?expires={expiry}&sig={sig}"
                        try:
                            when = started.astimezone(BEIJING).strftime("%m-%d %H:%M:%S")
                            await send_bark(self.client, cfg.bark_url, "宠物提醒 · " + EVENTS[event.type],
                                            f"{when}（北京时间采样）\n{event.description}", image_url)
                            notification = "sent" if image_url else "sent_text"
                        except ModelError as exc:
                            notification = "failed"
                            self.status["last_error"] = str(exc)
                        except asyncio.CancelledError:
                            self.store.update(event_id, status=status, description=event.description,
                                              pet=event.pet, notification="unknown")
                            raise
                        self.store.update(event_id, status=status, description=event.description,
                                          pet=event.pet, notification=notification)
            except ModelError:
                for event_id in ids.values():
                    if self.store.get(event_id)["status"] == "reviewing":
                        self.store.update(event_id, status="review_error", description="模型复核失败，未确认事件")
                raise
            finally:
                for event_id in ids.values():
                    if self.store.get(event_id)["status"] == "reviewing":
                        self.store.update(event_id, status="cancelled", description="本轮处理已中断，未确认事件")
            self.status["phase"] = "complete"

    async def preview(self):
        source = self.config.current.stream_url
        if not source:
            raise media.MediaError("请先保存视频源地址")
        async with self.capture_lock:
            with tempfile.TemporaryDirectory(prefix="preview-", dir=self.data) as work:
                raw = Path(work) / "preview.mkv"
                duration = await media.record(source, raw, 2)
                await media.screenshot(raw, self.data / "preview.jpg", duration / 2)
