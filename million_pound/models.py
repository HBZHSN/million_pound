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
            "eating": "宠物正在碗或饮水器前实际吃东西或喝水，统一称为吃喝，不区分水盆和饭盆，不包括路过或闻嗅",
            "destruction": "宠物正在撕咬或抓挠家具、门、线缆等家居物品，不包括正常玩宠物玩具",
            "toileting": "宠物正在猫砂盆或指定厕所之外排尿或排便，需有明确行为证据，不包括普通蹲坐",
        }
        payload = {
            "model": "d3",
            "state": f"家庭宠物监控，百万是拉布拉多犬，英镑是缅因猫。这是 {settings.speed} 倍速视频。"
                     f"从原视频每秒抽取 {settings.sample_fps} 帧。"
                     "只根据可见画面判断，不凭动物品种推测，区分拉布拉多与缅因猫。",
            "questions": {key: {"type": "noul", "instructions": text + "，是否发生？",
                                "criteria": {"true": "明确发生", "false": "未发生或证据不足"}}
                          for key, text in descriptions.items()},
            "videos": [data_url(video, "video/mp4")],
        }
        criteria = {
            "sleeping": "该宠物卧躺、闭眼并持续静止，明确在睡觉；不能仅凭蹲坐或单帧静止判断睡觉",
            "eating": "该宠物正在碗、食物或饮水器前实际吃东西或喝水，统一为吃喝；不要求区分水盆和饭盆，不包括路过或闻嗅",
            "walking": "该宠物在画面中走动、散步或跑动",
            "playing": "该宠物正在玩玩具、与另一只宠物互动或嬉戏",
            "resting": "该宠物坐着、站着或卧躺休息，没有明确睡着或其他行为",
            "not_visible": "整段视频中均看不到该宠物，不是仅短暂被遮挡或难以辨认",
            "other": "能看见该宠物，正在进行梳理、抓痒等上述类别之外的活动",
            "uncertain": "画面遮挡、太暗、身份或行为证据不足，无法可靠判断",
        }
        for pet, name in (("dog", "百万"), ("cat", "英镑")):
            payload["questions"][pet] = {
                "type": "choice", "instructions": f"这段视频里{name}最主要的活动是什么？"
                "请选择一个最符合可见证据的类别。不清楚时选 uncertain，不要把看不清等同于未入镜。",
                "criteria": criteria,
            }
        result = await post_json(self.client, model_endpoint(settings, "systemone"), payload, key=settings.model_key)
        try:
            scores = {key: result["answers"][key]["noul"] for key in EVENTS}
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1
                   for v in scores.values()):
                raise ValueError
            pets = {}
            for pet in ("dog", "cat"):
                answer = result["answers"][pet]
                activity = answer["choice"]
                confidence = answer["probabilities"][activity]
                if activity not in criteria or isinstance(confidence, bool) or not isinstance(confidence, (int, float)) \
                        or not math.isfinite(confidence) or not 0 <= confidence <= 1:
                    raise ValueError
                pets[pet] = {"activity": activity if confidence >= 0.6 else "uncertain", "confidence": confidence}
            return {"scores": scores, "pets": pets}
        except (KeyError, TypeError, ValueError):
            raise ModelError("d3 返回的活动或事件判断格式无效") from None

    async def review(self, settings: Settings, candidates: list[str], images: list[Path], offsets: list[float]):
        prompt = (
            "你在复核家庭宠物监控，百万是拉布拉多犬，英镑是缅因猫。截图来自同一段原速视频，"
            f"时间偏移（秒）依次为 {offsets}。初判候选为 {candidates}。逐一复核所有候选，"
            "没有清晰证据则 confirmed=false。eating 统一表示吃喝，进食或饮水均可，不必分辨水盆和饭盆；"
            "但需有实际吃喝证据，不能只是路过或闻嗅。拆家不包括玩宠物玩具；"
            "乱拉乱尿需明确排泄证据且在猫砂盆或指定厕所外，仅蹲坐不足以确认。"
            "看不清指定区域或行为时请拒绝确认，不得猜测。描述用百万、英镑称呼宠物，统一用吃喝，不猜测碗内是水还是食物。"
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
            for event in parsed.events:
                event.description = (event.description.replace("拉布拉多犬", "百万").replace("拉布拉多", "百万")
                                     .replace("缅因猫", "英镑").replace("缅因", "英镑"))
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
