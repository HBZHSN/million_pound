import asyncio
import hmac
import html
import sqlite3
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlencode

import httpx
from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field, ValidationError

from .config import ACTIVITIES, BEIJING, EVENTS, ROOT, Config, Settings
from .media import MediaError
from .models import PET_NAMES, ModelError, send_bark
from .monitor import Monitor
from .security import REMEMBER_SESSION_SECONDS, SESSION_RENEW_THRESHOLD, Security
from .store import IMAGE_NAME, Store


ASSETS = Path(__file__).parent / "web"
SHARE_PRIORITY = {
    "toileting": 0,
    "destruction": 1,
    "eating": 2,
    "on_table": 3,
    "on_sofa": 4,
    "on_chair": 5,
    "playing": 6,
    "walking": 7,
    "grooming": 8,
    "sleeping": 9,
}
SHARE_LABELS = {**ACTIVITIES, **EVENTS, "sleeping": "休息", "resting": "休息", "drinking": "吃喝", "walking": "走动"}


class Login(BaseModel):
    password: str = Field(min_length=1, max_length=256)
    remember: bool = True


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=11, max_length=256)


class ViewerPasswordCreate(BaseModel):
    password: str = Field(min_length=6, max_length=256)
    label: str = Field(default="", max_length=64)


class ActivityShareCreate(BaseModel):
    pet: Literal["", "dog", "cat"] = ""
    days: int = Field(default=7, ge=1, le=7)


class MediaDelete(BaseModel):
    ids: list[Annotated[int, Field(strict=True, ge=1, le=2**63 - 1)]] = Field(min_length=1, max_length=5000)


def _share_activity_label(item: dict, events: list[dict], pet: str) -> tuple[str, str]:
    def best_for_pet(target_pet: str) -> tuple[int, str] | None:
        decision = (item.get("pets") or {}).get(target_pet) or {}
        if decision.get("visibility") != "present":
            return None
        chosen = decision.get("activity")
        if chosen == "uncertain":
            probs = {
                k: v for k, v in (decision.get("probabilities") or {}).items()
                if k in ACTIVITIES and k not in ("uncertain", "not_visible") and isinstance(v, (int, float))
            }
            chosen = max(probs, key=probs.get, default=decision.get("proposed_activity"))
        if chosen in ("not_visible", "uncertain", None):
            chosen = None
        candidates = []
        if chosen:
            normalized = "sleeping" if chosen == "resting" else "eating" if chosen == "drinking" else chosen
            candidates.append((SHARE_PRIORITY.get(normalized, 10), normalized))
        for event in events:
            if event.get("pet") in (target_pet, "both"):
                candidates.append((SHARE_PRIORITY.get(event["type"], 10), event["type"]))
        return min(candidates, key=lambda pair: pair[0], default=None)

    if pet in PET_NAMES:
        best = best_for_pet(pet)
        label = SHARE_LABELS.get(best[1], "日常瞬间") if best else "日常瞬间"
        return PET_NAMES[pet], label
    both_best = [res for p in PET_NAMES if (res := best_for_pet(p))]
    best = min(both_best, key=lambda pair: pair[0], default=None)
    return "百万和英镑", SHARE_LABELS.get(best[1], "日常瞬间") if best else "日常瞬间"


def _status_html(status_code: int, eyebrow: str, title: str, message: str, link_href: str = "", link_label: str = "") -> HTMLResponse:
    link = (
        f'<p><a class="primary button-link" href="{html.escape(link_href)}">{html.escape(link_label)}</a></p>'
        if link_href and link_label else ""
    )
    body = (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>{html.escape(title)} · Million Pound</title>"
        "<link rel=\"stylesheet\" href=\"/assets/style.css\"></head>"
        "<body class=\"login-body\"><main class=\"login-card status-card\">"
        "<div class=\"brand-mark\" aria-hidden=\"true\">M</div>"
        f"<p class=\"eyebrow\">{html.escape(eyebrow)}</p>"
        f"<h1>{html.escape(title)}</h1>"
        f"<p class=\"muted\">{html.escape(message)}</p>{link}"
        "</main></body></html>"
    )
    return HTMLResponse(body, status_code=status_code)


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
    password_lock = asyncio.Lock()

    def same_origin(request):
        origin = request.headers.get("origin")
        allowed = {str(request.base_url).rstrip("/"), request.app.state.config.current.public_url}
        if origin and origin not in allowed:
            raise HTTPException(403, "请求来源无效")

    def set_session_cookie(response, request: Request, token: str, remember: bool):
        kwargs = {
            "httponly": True,
            "secure": request.url.scheme == "https",
            "samesite": "lax",
        }
        if remember:
            kwargs["max_age"] = REMEMBER_SESSION_SECONDS
        response.set_cookie("pet_session", token, **kwargs)

    def maybe_renew_session(response, request: Request, session_data: dict) -> str:
        if session_data["remember"] and session_data["expiry"] - time.time() < SESSION_RENEW_THRESHOLD:
            new_token = request.app.state.security.session(
                role=session_data["role"], key_id=session_data["key_id"], remember=True
            )
            set_session_cookie(response, request, new_token, True)
            return new_token
        return session_data["token"]

    async def authenticated(request: Request):
        token = request.cookies.get("pet_session", "")
        info = request.app.state.security.session_info(token)
        if not info:
            raise HTTPException(401, "请先登录")
        if request.method not in {"GET", "HEAD"}:
            same_origin(request)
            csrf = request.headers.get("x-csrf-token", "")
            if not hmac.compare_digest(csrf.encode(), request.app.state.security.csrf(token).encode()):
                raise HTTPException(403, "页面已过期，请刷新后重试")
        return {"token": token, **info}

    async def admin_authenticated(session_data: dict = Depends(authenticated)):
        if session_data["role"] != "admin":
            raise HTTPException(403, "需要管理员密码权限")
        return session_data

    async def verify_with_rate_limit(verifier, password: str):
        if len(failures) == 5 and time.monotonic() - failures[0] < 60:
            raise HTTPException(429, "尝试过于频繁，请一分钟后重试")
        result = await asyncio.to_thread(verifier, password)
        if not result:
            failures.append(time.monotonic())
            return None
        failures.clear()
        return result

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
    async def login_page(request: Request):
        if request.app.state.security.valid_session(request.cookies.get("pet_session", "")):
            return RedirectResponse("/", status_code=303)
        return FileResponse(ASSETS / "login.html")

    @app.get("/")
    @app.get("/activities")
    @app.get("/settings")
    async def index(request: Request):
        token = request.cookies.get("pet_session", "")
        info = request.app.state.security.session_info(token)
        if not info:
            return RedirectResponse("/login", status_code=303)
        if request.url.path == "/settings" and info["role"] != "admin":
            return _status_html(
                403,
                "MILLION POUND · ACCESS",
                "仅可查看时间线",
                "当前使用的是浏览密码，只能访问活动时间线页面。",
                "/",
                "返回活动时间线",
            )
        response = FileResponse(ASSETS / ("index.html" if request.url.path == "/settings" else "activities.html"))
        maybe_renew_session(response, request, {"token": token, **info})
        return response

    @app.get("/assets/{name}")
    async def asset(name: str):
        if name not in {"app.js", "login.js", "activities.js", "timeline.js", "client.js", "share.js", "style.css"}:
            raise HTTPException(404)
        return FileResponse(ASSETS / name)

    @app.post("/api/login")
    async def login(body: Login, request: Request):
        same_origin(request)
        async with password_lock:
            auth_result = await verify_with_rate_limit(request.app.state.security.authenticate, body.password)
            if not auth_result:
                raise HTTPException(401, "密码错误")
            role, key_id = auth_result
            token = request.app.state.security.session(role=role, key_id=key_id, remember=body.remember)
            response = JSONResponse({"ok": True, "role": role})
            set_session_cookie(response, request, token, body.remember)
            return response

    @app.put("/api/password", dependencies=[Depends(admin_authenticated)])
    async def change_password(body: PasswordChange, request: Request):
        async with password_lock:
            if not await verify_with_rate_limit(request.app.state.security.verify_password, body.current_password):
                raise HTTPException(400, "当前密码错误")
            try:
                await asyncio.to_thread(request.app.state.security.change_password, body.new_password)
            except OSError:
                raise HTTPException(500, "密码保存失败，请检查磁盘和权限") from None
            response = JSONResponse({"ok": True})
            response.delete_cookie("pet_session", httponly=True, samesite="lax")
            return response

    @app.get("/api/viewer-passwords", dependencies=[Depends(admin_authenticated)])
    async def list_viewer_passwords(request: Request):
        return {"items": request.app.state.security.viewer_passwords()}

    @app.post("/api/viewer-passwords", dependencies=[Depends(admin_authenticated)])
    async def create_viewer_password(body: ViewerPasswordCreate, request: Request):
        async with password_lock:
            try:
                items = await asyncio.to_thread(request.app.state.security.add_viewer_password, body.password, body.label)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from None
            except OSError:
                raise HTTPException(500, "浏览密码保存失败，请检查磁盘和权限") from None
            return {"items": items}

    @app.delete("/api/viewer-passwords/{password_id}", dependencies=[Depends(admin_authenticated)])
    async def delete_viewer_password(password_id: str, request: Request):
        async with password_lock:
            try:
                items = await asyncio.to_thread(request.app.state.security.remove_viewer_password, password_id)
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from None
            except OSError:
                raise HTTPException(500, "浏览密码删除失败，请检查磁盘和权限") from None
            return {"items": items}

    @app.post("/api/logout", dependencies=[Depends(authenticated)])
    async def logout():
        response = JSONResponse({"ok": True})
        response.delete_cookie("pet_session", httponly=True, samesite="lax")
        return response

    @app.get("/api/session")
    async def session(request: Request, session_data: dict = Depends(authenticated)):
        security = request.app.state.security
        active_token = session_data["token"]
        renew = session_data["remember"] and session_data["expiry"] - time.time() < SESSION_RENEW_THRESHOLD
        if renew:
            active_token = security.session(role=session_data["role"], key_id=session_data["key_id"], remember=True)
        response = JSONResponse({"csrf": security.csrf(active_token), "role": session_data["role"]})
        if renew:
            set_session_cookie(response, request, active_token, True)
        return response

    @app.get("/api/config", dependencies=[Depends(admin_authenticated)])
    async def get_config(request: Request):
        return request.app.state.config.public()

    @app.put("/api/config", dependencies=[Depends(admin_authenticated)])
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

    @app.get("/api/status", dependencies=[Depends(admin_authenticated)])
    async def status(request: Request):
        state = request.app.state
        return {**state.monitor.status, "enabled": state.config.current.enabled,
                "last_review": state.store.meta("last_review"), "timezone": "Asia/Shanghai"}

    @app.get("/api/events", dependencies=[Depends(admin_authenticated)])
    async def events(request: Request, before: int | None = None):
        items = request.app.state.store.recent(before)
        return {"items": items, "next_before": items[-1]["id"] if len(items) == 50 else None}

    @app.delete("/api/{collection}/media", dependencies=[Depends(admin_authenticated)])
    async def delete_media(collection: Literal["activities", "events"], body: MediaDelete, request: Request):
        try:
            return request.app.state.store.delete_media(collection, body.ids, root / "data" / "images")
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        except (OSError, sqlite3.Error):
            raise HTTPException(500, "媒体删除未完成，请重试并检查磁盘和权限") from None

    @app.get("/api/activities", dependencies=[Depends(authenticated)])
    async def activities(request: Request, before: int | None = Query(default=None, ge=1),
                         limit: int = Query(default=50, ge=1, le=1000),
                         day: date | None = None, hour: int | None = Query(default=None, ge=0, le=23)):
        start = end = None
        if day is not None or hour is not None:
            selected = day or datetime.now(BEIJING).date()
            begin = datetime.combine(selected, datetime.min.time(), tzinfo=BEIJING) + timedelta(hours=hour or 0)
            finish = begin + (timedelta(hours=1) if hour is not None else timedelta(days=1))
            start, end = begin.astimezone(timezone.utc).isoformat(), finish.astimezone(timezone.utc).isoformat()
        state = request.app.state
        items = state.store.activities(before, start, end, limit)
        events = state.store.activity_events(items)
        for item in items:
            item["events"] = events.get(item["started_at"], [])
            item["video"] = f"/api/activities/{item['id']}/video" if activity_video_path(item) else None
        return {
            "items": items,
            "next_before": items[-1]["id"] if len(items) == limit else None,
            "labels": {**ACTIVITIES, **EVENTS},
            "capture_status": {
                "recording": state.monitor.status["recording"],
                "enabled": state.config.current.enabled,
                "has_error": bool(state.monitor.status["last_error"]),
            },
        }

    def activity_video_path(item):
        if item and item["images"] and IMAGE_NAME.fullmatch(item["images"][0]):
            path = (root / "data" / "images" / item["images"][0]).with_suffix(".mp4")
            if path.is_file():
                return path

    @app.get("/api/activities/{activity_id}/video", dependencies=[Depends(authenticated)])
    async def activity_video(activity_id: int, request: Request):
        path = activity_video_path(request.app.state.store.get_activity(activity_id))
        if path is None:
            raise HTTPException(404, "录像已过期或不存在")
        return FileResponse(path, media_type="video/mp4")

    @app.post("/api/activities/{activity_id}/share", dependencies=[Depends(authenticated)])
    async def share_activity_video(
        activity_id: int,
        request: Request,
        body: ActivityShareCreate = Body(default_factory=ActivityShareCreate),
    ):
        item = request.app.state.store.get_activity(activity_id)
        if activity_video_path(item) is None:
            raise HTTPException(404, "录像已过期或不存在")
        expiry = int(time.time()) + body.days * 86400
        sig = request.app.state.security.activity_share_signature(activity_id, expiry, body.pet)
        params = {"expires": expiry, "sig": sig}
        if body.pet:
            params["pet"] = body.pet
        share_path = f"/share/{activity_id}?{urlencode(params)}"
        base = request.app.state.config.current.public_url or str(request.base_url).rstrip("/")
        return {
            "path": share_path,
            "url": base + share_path,
            "expires_at": datetime.fromtimestamp(expiry, timezone.utc).isoformat(),
        }

    def validate_share(request: Request, activity_id: int, expires: int, sig: str, pet: str, *, html_errors: bool = False):
        if not request.app.state.security.valid_activity_share(activity_id, expires, sig, pet):
            if html_errors:
                return None, None, _status_html(
                    403,
                    "MILLION POUND · SHARE",
                    "分享链接无效或已过期",
                    "该视频分享链接可能已超过 7 天有效期，或链接地址不完整。",
                )
            raise HTTPException(403, "分享链接无效或已过期")
        item = request.app.state.store.get_activity(activity_id)
        path = activity_video_path(item)
        if item is None or path is None:
            if html_errors:
                return None, None, _status_html(
                    404,
                    "MILLION POUND · SHARE",
                    "视频已过期或已删除",
                    "这段录像已被管理员删除，或已超过媒体保留期。",
                )
            raise HTTPException(404, "录像已过期或不存在")
        return item, path, None

    @app.get("/share/{activity_id}")
    async def shared_activity_page(activity_id: int, expires: int, sig: str, request: Request, pet: str = ""):
        _, _, error_page = validate_share(request, activity_id, expires, sig, pet, html_errors=True)
        if error_page is not None:
            return error_page
        return FileResponse(ASSETS / "share.html")

    @app.get("/share/{activity_id}/meta")
    async def shared_activity_meta(activity_id: int, expires: int, sig: str, request: Request, pet: str = ""):
        item, _, _ = validate_share(request, activity_id, expires, sig, pet)
        events_map = request.app.state.store.activity_events([item])
        pet_name, activity_label = _share_activity_label(item, events_map.get(item["started_at"], []), pet)
        params = {"expires": expires, "sig": sig}
        if pet:
            params["pet"] = pet
        query = urlencode(params)
        return {
            "pet": pet,
            "pet_name": pet_name,
            "activity_label": activity_label,
            "started_at": item["started_at"],
            "ended_at": item["ended_at"],
            "expires_at": datetime.fromtimestamp(expires, timezone.utc).isoformat(),
            "video": f"/share/{activity_id}/video?{query}",
            "poster": f"/share/{activity_id}/poster?{query}" if item["images"] else None,
        }

    @app.get("/share/{activity_id}/video")
    async def shared_activity_video(activity_id: int, expires: int, sig: str, request: Request, pet: str = ""):
        _, path, _ = validate_share(request, activity_id, expires, sig, pet)
        return FileResponse(path, media_type="video/mp4")

    @app.get("/share/{activity_id}/poster")
    async def shared_activity_poster(activity_id: int, expires: int, sig: str, request: Request, pet: str = ""):
        item, _, _ = validate_share(request, activity_id, expires, sig, pet)
        if not item["images"]:
            raise HTTPException(404, "截图已过期或不存在")
        return image_file(item["images"][0], root / "data" / "images")

    @app.get("/api/activities/{activity_id}/images/{index}", dependencies=[Depends(authenticated)])
    async def activity_image(activity_id: int, index: int, request: Request):
        item = request.app.state.store.get_activity(activity_id)
        if not item or not 0 <= index < len(item["images"]):
            raise HTTPException(404, "截图已过期或不存在")
        return image_file(item["images"][index], root / "data" / "images")

    @app.get("/api/events/{event_id}/images/{index}", dependencies=[Depends(admin_authenticated)])
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

    @app.post("/api/test/camera", dependencies=[Depends(admin_authenticated)])
    async def test_camera(request: Request):
        try:
            await request.app.state.monitor.preview()
        except (MediaError, OSError):
            raise HTTPException(400, "取流失败，请检查视频源、桥接授权和镜头开关") from None
        return {"image": "/api/preview"}

    @app.get("/api/preview", dependencies=[Depends(admin_authenticated)])
    async def preview():
        path = root / "data" / "preview.jpg"
        if not path.is_file():
            raise HTTPException(404, "请先测试摄像头")
        return FileResponse(path, media_type="image/jpeg")

    @app.post("/api/test/bark", dependencies=[Depends(admin_authenticated)])
    async def test_bark(request: Request):
        try:
            await send_bark(request.app.state.client, request.app.state.config.current.bark_url,
                            "家庭宠物监控 · 测试", "通知通道已连通。这是一条开发测试消息。")
        except ModelError as exc:
            raise HTTPException(400, str(exc)) from None
        return {"ok": True}

    return app


app = create_app()
