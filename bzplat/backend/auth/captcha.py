"""自建图形/算术验证码（captcha + Pillow）。

GET 生成：返回 captcha_id + base64 PNG；校验一次性消费，TTL 300s。
"""
from __future__ import annotations

import base64
import random
import secrets
import string
import threading
import time
from dataclasses import dataclass

from captcha.image import ImageCaptcha

CAPTCHA_TTL_SEC = 300
_ALPHABET = string.ascii_uppercase + string.digits
_ALPHABET = "".join(c for c in _ALPHABET if c not in "0O1IL")


@dataclass
class _Challenge:
    answer: str
    expires_at: float


class CaptchaStore:
    """进程内验证码仓库（单 uvicorn worker 够用）。"""

    def __init__(self, *, ttl_sec: int = CAPTCHA_TTL_SEC) -> None:
        self.ttl_sec = ttl_sec
        self._lock = threading.Lock()
        self._items: dict[str, _Challenge] = {}
        self._image = ImageCaptcha(width=160, height=56)

    def create(self) -> tuple[str, str, bytes]:
        """返回 (captcha_id, answer, png_bytes)。答案仅存服务端。"""
        if random.random() < 0.5:
            a = random.randint(1, 20)
            b = random.randint(1, 20)
            if random.random() < 0.5:
                text = f"{a}+{b}"
                answer = str(a + b)
            else:
                if a < b:
                    a, b = b, a
                text = f"{a}-{b}"
                answer = str(a - b)
        else:
            text = "".join(secrets.choice(_ALPHABET) for _ in range(4))
            answer = text.lower()

        captcha_id = secrets.token_urlsafe(16)
        png = self._image.generate(text).read()
        with self._lock:
            self._purge_locked()
            self._items[captcha_id] = _Challenge(
                answer=answer, expires_at=time.monotonic() + self.ttl_sec
            )
        return captcha_id, answer, png

    def verify(self, captcha_id: str, answer: str) -> bool:
        if not captcha_id or answer is None:
            return False
        with self._lock:
            self._purge_locked()
            item = self._items.pop(captcha_id, None)
        if item is None:
            return False
        if time.monotonic() > item.expires_at:
            return False
        got = str(answer).strip().lower()
        return secrets.compare_digest(got, item.answer.lower())

    def _purge_locked(self) -> None:
        now = time.monotonic()
        dead = [k for k, v in self._items.items() if v.expires_at < now]
        for k in dead:
            del self._items[k]


def png_to_data_url(png: bytes) -> str:
    b64 = base64.b64encode(png).decode("ascii")
    return f"data:image/png;base64,{b64}"
