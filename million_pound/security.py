import hashlib
import hmac
import json
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import write_private

ADMIN_SESSION_SECONDS = 43200
REMEMBER_SESSION_SECONDS = 30 * 86400
SESSION_RENEW_THRESHOLD = 29 * 86400
IMAGE_SHARE_SECONDS = 3600
ACTIVITY_SHARE_SECONDS = 7 * 86400
MAX_VIEWER_PASSWORDS = 50


def _scrypt_hash(password: str, salt: str) -> str:
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()


def _verify_hash(password: str, record: dict) -> bool:
    return hmac.compare_digest(_scrypt_hash(password, record["salt"]), record["hash"])


class Security:
    def __init__(self, data: Path):
        self.path = data / "auth.json"
        if not self.path.exists():
            raise RuntimeError("请先使用 .venv/bin/python -m million_pound init 初始化登录密码")
        self.auth = json.loads(self.path.read_text())

    @staticmethod
    def set_password(data: Path, password: str):
        if not 11 <= len(password) <= 256:
            raise ValueError("密码长度需要 11 到 256 个字符")
        auth_path = data / "auth.json"
        existing = json.loads(auth_path.read_text()) if auth_path.exists() else {}
        viewer_passwords = [
            vp for vp in existing.get("viewer_passwords", [])
            if not _verify_hash(password, vp)
        ]
        salt = secrets.token_hex(16)
        digest = _scrypt_hash(password, salt)
        auth = {
            "salt": salt,
            "hash": digest,
            "secret": secrets.token_hex(32),
            "viewer_passwords": viewer_passwords,
        }
        write_private(auth_path, auth)
        return auth

    def change_password(self, password: str):
        self.auth = self.set_password(self.path.parent, password)

    def verify_password(self, password: str) -> bool:
        return _verify_hash(password, self.auth)

    def authenticate(self, password: str) -> tuple[str, str] | None:
        if self.verify_password(password):
            return ("admin", "admin")
        for item in self.auth.get("viewer_passwords", []):
            if _verify_hash(password, item):
                return ("viewer", item["id"])
        return None

    def viewer_passwords(self) -> list[dict]:
        return [
            {"id": item["id"], "label": item.get("label", ""), "created_at": item.get("created_at", "")}
            for item in self.auth.get("viewer_passwords", [])
        ]

    def add_viewer_password(self, password: str, label: str = "") -> list[dict]:
        if not 6 <= len(password) <= 256:
            raise ValueError("浏览密码长度需要 6 到 256 个字符")
        clean_label = label.strip()[:64]
        current = list(self.auth.get("viewer_passwords", []))
        if len(current) >= MAX_VIEWER_PASSWORDS:
            raise ValueError(f"最多只能配置 {MAX_VIEWER_PASSWORDS} 个浏览密码")
        if self.verify_password(password):
            raise ValueError("浏览密码不能与管理员密码相同")
        if any(_verify_hash(password, item) for item in current):
            raise ValueError("该浏览密码已存在")
        salt = secrets.token_hex(16)
        current.append({
            "id": secrets.token_hex(8),
            "label": clean_label,
            "salt": salt,
            "hash": _scrypt_hash(password, salt),
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        updated = {**self.auth, "viewer_passwords": current}
        write_private(self.path, updated)
        self.auth = updated
        return self.viewer_passwords()

    def remove_viewer_password(self, password_id: str) -> list[dict]:
        current = list(self.auth.get("viewer_passwords", []))
        remaining = [item for item in current if item.get("id") != password_id]
        if len(remaining) == len(current):
            raise LookupError("浏览密码不存在")
        updated = {**self.auth, "viewer_passwords": remaining}
        write_private(self.path, updated)
        self.auth = updated
        return self.viewer_passwords()

    def sign(self, message: str) -> str:
        return hmac.new(bytes.fromhex(self.auth["secret"]), message.encode(), hashlib.sha256).hexdigest()

    def session(self, role: str = "admin", key_id: str = "admin", remember: bool = True) -> str:
        ttl = REMEMBER_SESSION_SECONDS if remember else ADMIN_SESSION_SECONDS
        flag = "1" if remember else "0"
        payload = f"{int(time.time()) + ttl}.{flag}.{role}.{key_id}.{secrets.token_hex(16)}"
        return f"{payload}.{self.sign('session:' + payload)}"

    def session_info(self, token: str) -> dict | None:
        try:
            expiry_raw, flag, role, key_id, nonce, sig = token.split(".")
            expiry = int(expiry_raw)
            if expiry <= time.time() or flag not in {"0", "1"} or role not in {"admin", "viewer"}:
                return None
            payload = f"{expiry_raw}.{flag}.{role}.{key_id}.{nonce}"
            if not hmac.compare_digest(sig.encode(), self.sign("session:" + payload).encode()):
                return None
            if role == "admin" and key_id != "admin":
                return None
            if role == "viewer" and not any(item.get("id") == key_id for item in self.auth.get("viewer_passwords", [])):
                return None
            return {"expiry": expiry, "remember": flag == "1", "role": role, "key_id": key_id}
        except (ValueError, AttributeError):
            return None

    def valid_session(self, token: str) -> bool:
        return self.session_info(token) is not None

    def csrf(self, token: str) -> str:
        return self.sign("csrf:" + token)

    def image_signature(self, name: str, expiry: int) -> str:
        return self.sign(f"image:{name}:{expiry}")

    def valid_image(self, name: str, expiry: int, sig: str) -> bool:
        return time.time() < expiry <= time.time() + IMAGE_SHARE_SECONDS and hmac.compare_digest(
            sig.encode(), self.image_signature(name, expiry).encode()
        )

    def activity_share_signature(self, activity_id: int, expiry: int, pet: str = "") -> str:
        return self.sign(f"activity-share:{activity_id}:{pet}:{expiry}")

    def valid_activity_share(self, activity_id: int, expiry: int, sig: str, pet: str = "") -> bool:
        if pet not in {"", "dog", "cat"}:
            return False
        return time.time() < expiry <= time.time() + ACTIVITY_SHARE_SECONDS and hmac.compare_digest(
            sig.encode(), self.activity_share_signature(activity_id, expiry, pet).encode()
        )
