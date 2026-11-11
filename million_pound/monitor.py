import asyncio
import shutil
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import media
from .config import BEIJING, EVENTS, in_schedule
from .models import ModelError, Models, send_bark


class Monitor:
    def __init__(self, config, store, security, data: Path, client):
        self.config, self.store, self.security = config, store, security
        self.data, self.client = data, client
        for prefix in ("clip-", "preview-"):
            for old in data.glob(prefix + "*"):
                if old.is_dir() and not old.is_symlink():
                    shutil.rmtree(old)
        self.images = data / "images"
        self.images.mkdir(mode=0o700, exist_ok=True)
        self.models = Models(client)
        self.wake = asyncio.Event()
        self.capture_lock = asyncio.Lock()
        self.cycle_task = None
        self.task = None
        self.stopping = False
        self.status = {"phase": "starting", "last_error": None, "last_started": None,
                       "last_finished": None, "next_sample": None, "last_scores": None}

    def changed(self):
        if self.cycle_task and not self.cycle_task.done():
            self.cycle_task.cancel()
        self.wake.set()

    async def close(self):
        self.stopping = True
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    async def run(self):
        next_due, cleaned = 0, 0
        while True:
            if self.wake.is_set():
                self.wake.clear()
                next_due = 0
            cfg = self.config.current.model_copy(deep=True)
            now = datetime.now(timezone.utc)
            self.status["next_sample"] = None
            try:
                if time.monotonic() - cleaned > 3600:
                    self.store.cleanup(self.images, cfg.retention_days)
                    cleaned = time.monotonic()
                if not cfg.enabled:
                    self.status["phase"] = "disabled"
                elif not cfg.stream_url:
                    self.status["phase"] = "needs_camera"
                elif not in_schedule(cfg.windows, now):
                    self.status["phase"] = "outside_schedule"
                elif time.monotonic() >= next_due:
                    started = time.monotonic()
                    self.status.update(last_started=now.isoformat(), last_error=None)
                    self.cycle_task = asyncio.create_task(self.cycle(cfg))
                    try:
                        await self.cycle_task
                    except asyncio.CancelledError:
                        if self.stopping:
                            raise
                        self.status["phase"] = "waiting"
                    next_due = max(started + cfg.sample_interval, time.monotonic() + 1)
                    self.status["last_finished"] = datetime.now(timezone.utc).isoformat()
                else:
                    self.status["phase"] = "waiting"
                if cfg.enabled and cfg.stream_url and in_schedule(cfg.windows, now):
                    self.status["next_sample"] = time.time() + max(0, next_due - time.monotonic())
            except (media.MediaError, ModelError, OSError) as exc:
                self.status.update(phase="error", last_error=str(exc) if isinstance(exc, (media.MediaError, ModelError))
                                   else "本地文件读写失败，请检查磁盘空间和权限")
                next_due = time.monotonic() + min(30, cfg.sample_interval)
            except Exception:
                self.status.update(phase="error", last_error="监控处理异常，请检查本地配置后重试")
                next_due = time.monotonic() + 30
            delay = min(10, max(1, next_due - time.monotonic())) if next_due else 10
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=delay)
            except TimeoutError:
                pass

    async def cycle(self, cfg):
        with tempfile.TemporaryDirectory(prefix="clip-", dir=self.data) as work:
            raw, fast = Path(work) / "original.mkv", Path(work) / "fast.mp4"
            self.status["phase"] = "recording"
            async with self.capture_lock:
                started = datetime.now(timezone.utc)
                duration = await media.record(cfg.stream_url, raw, cfg.clip_duration)
            self.status["phase"] = "deciding"
            await media.accelerate(raw, fast, cfg.speed)
            scores = await self.models.decide(cfg, fast)
            self.status["last_scores"] = scores
            candidates = [key for key, score in scores.items() if score >= 0.7]
            if not candidates:
                self.status["phase"] = "no_event"
                return
            offsets = [round(duration * fraction, 3) for fraction in (0.1, 0.5, 0.9)]
            prefix = uuid.uuid4().hex
            paths = [self.images / f"{prefix}-{i}.jpg" for i in range(3)]
            for path, offset in zip(paths, offsets):
                await media.screenshot(raw, path, offset)
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
