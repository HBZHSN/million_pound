import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator


ROOT = Path(__file__).resolve().parent.parent
BEIJING = ZoneInfo("Asia/Shanghai")
EVENTS = {
    "eating": "吃喝",
    "destruction": "拆家",
    "toileting": "乱拉乱尿",
    "on_sofa": "上沙发",
    "on_table": "上桌子",
    "on_chair": "上椅子",
}
DEFAULT_EVENT_MODES = {
    "eating": "record",
    "destruction": "notify",
    "toileting": "notify",
    "on_sofa": "off",
    "on_table": "off",
    "on_chair": "off",
}
ACTIVITIES = {
    "sleeping": "休息",
    "drinking": "吃喝",
    "eating": "吃喝",
    "walking": "走动 / 散步",
    "playing": "玩耍",
    "grooming": "舔毛",
    "on_sofa": "上沙发",
    "on_table": "上桌子",
    "on_chair": "上椅子",
    "resting": "休息",
    "not_visible": "未入镜",
    "other": "其他活动",
    "uncertain": "判断不清",
}


def write_private(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".saving-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


class TimeWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    end: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool = False
    stream_url: str = Field(default="", max_length=2048)
    clip_duration: int = Field(default=60, ge=2, le=120)
    sample_fps: float = Field(default=2, ge=0.5, le=5)
    speed: float = Field(default=6, ge=1, le=16)
    review_interval: int = Field(default=0, ge=0, le=86400)
    windows: list[TimeWindow] = Field(default_factory=list, max_length=24)
    event_modes: dict[str, Literal["off", "record", "notify"]] = Field(
        default_factory=lambda: dict(DEFAULT_EVENT_MODES)
    )
    model_url: str = "http://127.0.0.1:18080"
    model_key: str = Field(default="", max_length=2048)
    bark_url: str = Field(default="", max_length=4096)
    public_url: str = ""
    retention_days: int = Field(default=7, ge=1, le=365)

    @field_validator("stream_url")
    @classmethod
    def stream_valid(cls, value):
        if value:
            parts = urlsplit(value)
            if parts.scheme not in {"rtsp", "rtsps", "http", "https"} or not parts.hostname:
                raise ValueError("视频源必须是 RTSP 或 HTTP(S) 地址")
        return value

    @field_validator("model_url", "public_url", "bark_url")
    @classmethod
    def http_valid(cls, value, info):
        if not value and info.field_name != "model_url":
            return value
        p = urlsplit(value)
        if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password or p.fragment:
            raise ValueError("需要有效的 HTTP(S) 地址")
        if info.field_name != "bark_url" and (p.query or p.path.rstrip("/") not in {"", "/v1"}):
            raise ValueError("请填写服务根地址，模型地址也可包含 /v1")
        if info.field_name == "public_url" and p.path.rstrip("/"):
            raise ValueError("公网地址请填写域名根地址")
        if info.field_name == "bark_url" and not p.path.strip("/"):
            raise ValueError("Bark 地址必须包含设备密钥")
        return value.rstrip("/")

    @field_validator("event_modes")
    @classmethod
    def modes_valid(cls, value):
        if set(value) != set(EVENTS):
            raise ValueError("必须完整配置吃喝、拆家、乱拉乱尿、上沙发、上桌子、上椅子事件模式")
        return value


def in_schedule(windows: list[TimeWindow], now: datetime) -> bool:
    current = now.astimezone(BEIJING).strftime("%H:%M")
    return not windows or any(
        w.start == w.end
        or (w.start <= current < w.end if w.start < w.end else current >= w.start or current < w.end)
        for w in windows
    )


class Config:
    def __init__(self, root: Path):
        self.path = root / "config.json"
        value = json.loads(self.path.read_text()) if self.path.exists() else {}
        value.pop("sample_interval", None)
        modes = value.get("event_modes")
        if isinstance(modes, dict) and {"eating", "destruction", "toileting"}.issubset(modes) and set(modes).issubset(EVENTS):
            value["event_modes"] = {**DEFAULT_EVENT_MODES, **modes}
        self.current = Settings.model_validate(value)

    def save(self, settings: Settings):
        write_private(self.path, settings.model_dump())
        self.current = settings

    def public(self):
        value = self.current.model_dump()
        for key in ("model_key", "bark_url", "stream_url"):
            value[key + "_configured"] = bool(value.pop(key))
        return value
