import asyncio
import hmac
import re
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field, ValidationError

from .config import ACTIVITIES, BEIJING, ROOT, Config, Settings
from .media import MediaError
from .models import ModelError, send_bark
from .monitor import Monitor
from .security import Security
from .store import Store


ASSETS = Path(__file__).parent / "web"
IMAGE_NAME = re.compile(r"^[0-9a-f]{32}-[0-2]\.jpg$")


class Login(BaseModel):
    password: str = Field(min_length=1, max_length=256)


def create_app(root: Path = ROOT):
    @asynccontextmanager
    async def lifespan(app):
        data = root / "data"
        app.state.security = Security(data)
        app.state.config = Config(root)
        app.state.store = Store(data)
        async with httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(180, connect=10)) as client:
            app.state.client = client
            app.state.monitor = Monitor(app.state.config, app.state.store, app.state.security, data, client)
            app.state.monitor.task = asyncio.create_task(app.state.monitor.run())
            try:
                yield
            finally:
                await app.state.monitor.close()
                app.state.store.db.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    failures = deque(maxlen=5)

    def same_origin(request):
        origin = request.headers.get("origin")
        allowed = {str(request.base_url).rstrip("/"), request.app.state.config.current.public_url}
        if origin and origin not in allowed:
            raise HTTPException(403, "请求来源无效")

    async def authenticated(request: Request):
        token = request.cookies.get("pet_session", "")
        if not request.app.state.security.valid_session(token):
            raise HTTPException(401, "请先登录")
        if request.method not in {"GET", "HEAD"}:
            same_origin(request)
            csrf = request.headers.get("x-csrf-token", "")
            if not hmac.compare_digest(csrf.encode(), request.app.state.security.csrf(token).encode()):
                raise HTTPException(403, "页面已过期，请刷新后重试")
        return token

    @app.middleware("http")
    async def headers(request, call_next):
        length = request.headers.get("content-length", "0")
        if not length.isdigit() or int(length) > 65536:
            return JSONResponse({"detail": "请求内容过大或长度无效"}, status_code=413)
        response = await call_next(request)
        response.headers.update({
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'self'; img-src 'self' blob:; style-src 'self'; "
                                       "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
                                       "base-uri 'none'; form-action 'self'",
        })
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({"detail": [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]}, status_code=422)

    @app.get("/login")
    async def login_page():
        return FileResponse(ASSETS / "login.html")

    @app.get("/")
    @app.get("/activities")
    async def index(request: Request):
        if not request.app.state.security.valid_session(request.cookies.get("pet_session", "")):
            return RedirectResponse("/login", status_code=303)
        return FileResponse(ASSETS / ("activities.html" if request.url.path == "/activities" else "index.html"))

    @app.get("/assets/{name}")
    async def asset(name: str):
        if name not in {"app.js", "login.js", "activities.js", "timeline.js", "client.js", "style.css"}:
            raise HTTPException(404)
        return FileResponse(ASSETS / name)

    @app.post("/api/login")
    async def login(body: Login, request: Request):
        same_origin(request)
        if len(failures) == 5 and time.monotonic() - failures[0] < 60:
            raise HTTPException(429, "尝试过于频繁，请一分钟后重试")
        if not await asyncio.to_thread(request.app.state.security.verify_password, body.password):
            failures.append(time.monotonic())
            raise HTTPException(401, "密码错误")
        failures.clear()
        response = JSONResponse({"ok": True})
        response.set_cookie("pet_session", request.app.state.security.session(), max_age=43200,
                            httponly=True, secure=request.url.scheme == "https", samesite="strict")
        return response

    @app.post("/api/logout", dependencies=[Depends(authenticated)])
    async def logout():
        response = JSONResponse({"ok": True})
        response.delete_cookie("pet_session", httponly=True, samesite="strict")
        return response

    @app.get("/api/session")
    async def session(request: Request, token: str = Depends(authenticated)):
        return {"csrf": request.app.state.security.csrf(token)}

    @app.get("/api/config", dependencies=[Depends(authenticated)])
    async def get_config(request: Request):
        return request.app.state.config.public()

    @app.put("/api/config", dependencies=[Depends(authenticated)])
    async def put_config(patch: dict, request: Request):
        config = request.app.state.config
        try:
            settings = Settings.model_validate({**config.current.model_dump(), **patch})
        except ValidationError as exc:
            raise HTTPException(422, [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]) from None
        try:
            config.save(settings)
        except OSError:
            raise HTTPException(500, "配置保存失败，请检查磁盘和权限") from None
        request.app.state.monitor.changed()
        return config.public()

    @app.get("/api/status", dependencies=[Depends(authenticated)])
    async def status(request: Request):
        state = request.app.state
        return {**state.monitor.status, "enabled": state.config.current.enabled,
                "last_review": state.store.meta("last_review"), "timezone": "Asia/Shanghai"}

    @app.get("/api/events", dependencies=[Depends(authenticated)])
    async def events(request: Request, before: int | None = None):
        items = request.app.state.store.recent(before)
        return {"items": items, "next_before": items[-1]["id"] if len(items) == 50 else None}

    @app.get("/api/activities", dependencies=[Depends(authenticated)])
    async def activities(request: Request, before: int | None = Query(default=None, ge=1),
                         day: date | None = None, hour: int | None = Query(default=None, ge=0, le=23)):
        start = end = None
        if day is not None or hour is not None:
            selected = day or datetime.now(BEIJING).date()
            begin = datetime.combine(selected, datetime.min.time(), tzinfo=BEIJING) + timedelta(hours=hour or 0)
            finish = begin + (timedelta(hours=1) if hour is not None else timedelta(days=1))
            start, end = begin.astimezone(timezone.utc).isoformat(), finish.astimezone(timezone.utc).isoformat()
        items = request.app.state.store.activities(before, start, end)
        return {"items": items, "next_before": items[-1]["id"] if len(items) == 50 else None, "labels": ACTIVITIES}

    @app.get("/api/activities/{activity_id}/images/{index}", dependencies=[Depends(authenticated)])
    async def activity_image(activity_id: int, index: int, request: Request):
        item = request.app.state.store.get_activity(activity_id)
        if not item or not 0 <= index < len(item["images"]):
            raise HTTPException(404, "截图已过期或不存在")
        return image_file(item["images"][index], root / "data" / "images")

    @app.get("/api/events/{event_id}/images/{index}", dependencies=[Depends(authenticated)])
    async def event_image(event_id: int, index: int, request: Request):
        event = request.app.state.store.get(event_id)
        if not event or not 0 <= index < len(event["images"]):
            raise HTTPException(404, "截图已过期或不存在")
        return image_file(event["images"][index], root / "data" / "images")

    def image_file(name: str, directory: Path):
        if not IMAGE_NAME.fullmatch(name) or not (directory / name).is_file():
            raise HTTPException(404, "截图已过期或不存在")
        return FileResponse(directory / name, media_type="image/jpeg")

    @app.get("/shared/{name}")
    async def shared(name: str, expires: int, sig: str, request: Request):
        if not request.app.state.security.valid_image(name, expires, sig):
            raise HTTPException(403, "图片链接无效或已过期")
        return image_file(name, root / "data" / "images")

    @app.post("/api/test/camera", dependencies=[Depends(authenticated)])
    async def test_camera(request: Request):
        try:
            await request.app.state.monitor.preview()
        except (MediaError, OSError):
            raise HTTPException(400, "取流失败，请检查视频源、桥接授权和镜头开关") from None
        return {"image": "/api/preview"}

    @app.get("/api/preview", dependencies=[Depends(authenticated)])
    async def preview():
        path = root / "data" / "preview.jpg"
        if not path.is_file():
            raise HTTPException(404, "请先测试摄像头")
        return FileResponse(path, media_type="image/jpeg")

    @app.post("/api/test/bark", dependencies=[Depends(authenticated)])
    async def test_bark(request: Request):
        try:
            await send_bark(request.app.state.client, request.app.state.config.current.bark_url,
                            "家庭宠物监控 · 测试", "通知通道已连通。这是一条开发测试消息。")
        except ModelError as exc:
            raise HTTPException(400, str(exc)) from None
        return {"ok": True}

    return app


app = create_app()
