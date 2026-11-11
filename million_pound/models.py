import base64
import json
import math
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError

from .config import EVENTS, Settings


class ModelError(RuntimeError):
    pass


class ReviewEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["eating", "destruction", "toileting"]
    confirmed: StrictBool
    pet: Literal["dog", "cat", "both", "unknown"]
    description: str = Field(min_length=1, max_length=1000)


class ReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    events: list[ReviewEvent] = Field(max_length=3)


def data_url(path: Path, mime: str):
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()


def model_endpoint(settings: Settings, path: str):
    base = settings.model_url.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return base + "/v1/" + path


async def post_json(client: httpx.AsyncClient, url: str, payload: dict, *, key="", service="模型"):
    try:
        response = await client.post(url, json=payload, headers={"Authorization": "Bearer " + key} if key else {})
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as exc:
        raise ModelError(f"{service}服务返回 HTTP {exc.response.status_code}") from None
    except (httpx.HTTPError, ValueError):
        raise ModelError(f"{service}服务连接失败或返回无效数据") from None


class Models:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def decide(self, settings: Settings, video: Path):
        descriptions = {
            "eating": "宠物正在食盆前实际进食，不包括路过、闻嗅或饮水",
            "destruction": "宠物正在撕咬或抓挠家具、门、线缆等家居物品，不包括正常玩宠物玩具",
            "toileting": "宠物正在猫砂盆或指定厕所之外排尿或排便，需有明确行为证据，不包括普通蹲坐",
        }
        payload = {
            "model": "d3",
            "state": f"家庭宠物监控，一只拉布拉多和一只缅因猫。这是 {settings.speed} 倍速视频。"
                     "只根据可见画面判断以下行为是否实际发生，不凭动物品种或静止姿势推测。",
            "questions": {key: {"type": "noul", "instructions": text + "，是否发生？",
                                "criteria": {"true": "明确发生", "false": "未发生或证据不足"}}
                          for key, text in descriptions.items()},
            "videos": [data_url(video, "video/mp4")],
        }
        result = await post_json(self.client, model_endpoint(settings, "systemone"), payload, key=settings.model_key)
        try:
            scores = {key: result["answers"][key]["noul"] for key in EVENTS}
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1
                   for v in scores.values()):
                raise ValueError
            return scores
        except (KeyError, TypeError, ValueError):
            raise ModelError("d3 返回的事件概率格式无效") from None

    async def review(self, settings: Settings, candidates: list[str], images: list[Path], offsets: list[float]):
        prompt = (
            "你在复核家庭宠物监控，宠物是一只拉布拉多和一只缅因猫。截图来自同一段原速视频，"
            f"时间偏移（秒）依次为 {offsets}。初判候选为 {candidates}。逐一复核所有候选，"
            "没有清晰证据则 confirmed=false。吃饭需看见实际进食；拆家不包括玩宠物玩具；"
            "乱拉乱尿需明确排泄证据且在猫砂盆或指定厕所外，仅蹲坐不足以确认。"
            "看不清指定区域或行为时请拒绝确认，不得猜测。描述用中文简述证据。"
            '只返回 JSON：{"events":[{"type":"eating|destruction|toileting",'
            '"confirmed":true,"pet":"dog|cat|both|unknown","description":"证据描述"}]}。'
            "每个候选恰好返回一次，不添加其他事件。"
        )
        result = await post_json(self.client, model_endpoint(settings, "chat/completions"), {
            "model": "swift-1.5", "temperature": 0, "max_tokens": 2048,
            "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}] + [
                {"type": "image_url", "image_url": {"url": data_url(path, "image/jpeg")}} for path in images
            ]}],
        }, key=settings.model_key)
        try:
            choice = result["choices"][0]
            if choice.get("finish_reason") == "length":
                raise ValueError
            content = choice["message"]["content"].strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            parsed = ReviewResult.model_validate_json(content)
            kinds = [event.type for event in parsed.events]
            if len(kinds) != len(candidates) or set(kinds) != set(candidates):
                raise ValueError
            return parsed.events
        except (KeyError, IndexError, TypeError, AttributeError, ValueError, ValidationError):
            raise ModelError("swift-1.5 复核返回格式无效，未确认事件") from None


async def send_bark(client: httpx.AsyncClient, address: str, title: str, body: str, image: str | None = None):
    if not address:
        raise ModelError("尚未配置 Bark 地址")
    parts = urlsplit(address)
    # shortcut: 地址按 /设备密钥/标题/正文 解析，使用带路径前缀的 Bark 代理时需调整。
    key = parts.path.strip("/").split("/")[0]
    endpoint = urlunsplit((parts.scheme, parts.netloc, "/" + key, "", ""))
    payload = dict(parse_qsl(parts.query))
    payload.update(title=title, body=body)
    payload.setdefault("group", "家庭宠物监控")
    payload.pop("image", None)
    if image:
        payload["image"] = image
    result = await post_json(client, endpoint, payload, service="Bark")
    if not isinstance(result, dict) or result.get("code") != 200:
        raise ModelError("Bark 未确认推送成功")
