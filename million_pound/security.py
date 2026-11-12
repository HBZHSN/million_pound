import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path

from .config import write_private


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
        salt = secrets.token_hex(16)
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
        auth = {"salt": salt, "hash": digest, "secret": secrets.token_hex(32)}
        write_private(data / "auth.json", auth)
        return auth

    def change_password(self, password: str):
        self.auth = self.set_password(self.path.parent, password)

    def verify_password(self, password: str) -> bool:
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(self.auth["salt"]), n=16384, r=8, p=1).hex()
        return hmac.compare_digest(digest, self.auth["hash"])

    def sign(self, message: str) -> str:
        return hmac.new(bytes.fromhex(self.auth["secret"]), message.encode(), hashlib.sha256).hexdigest()

    def session(self) -> str:
        payload = f"{int(time.time()) + 43200}.{secrets.token_hex(16)}"
        return f"{payload}.{self.sign('session:' + payload)}"

    def valid_session(self, token: str) -> bool:
        try:
            expiry, nonce, sig = token.split(".")
            return int(expiry) > time.time() and hmac.compare_digest(sig.encode(), self.sign(f"session:{expiry}.{nonce}").encode())
        except (ValueError, AttributeError):
            return False

    def csrf(self, token: str) -> str:
        return self.sign("csrf:" + token)

    def image_signature(self, name: str, expiry: int) -> str:
        return self.sign(f"image:{name}:{expiry}")

    def valid_image(self, name: str, expiry: int, sig: str) -> bool:
        return time.time() < expiry <= time.time() + 3600 and hmac.compare_digest(sig.encode(), self.image_signature(name, expiry).encode())
