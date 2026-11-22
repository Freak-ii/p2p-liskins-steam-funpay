"""Steam Guard: генерация 2FA-кодов и ключей мобильных подтверждений (как в мобильном приложении)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import time

_CHARS = "23456789BCDFGHJKMNPQRTVWXY"


def generate_auth_code(shared_secret: str, timestamp: int | None = None) -> str:
    ts = int(timestamp if timestamp is not None else time.time())
    msg = struct.pack(">Q", ts // 30)
    digest = hmac.new(base64.b64decode(shared_secret), msg, hashlib.sha1).digest()
    start = digest[19] & 0x0F
    full = struct.unpack(">I", digest[start:start + 4])[0] & 0x7FFFFFFF
    code = ""
    for _ in range(5):
        full, idx = divmod(full, len(_CHARS))
        code += _CHARS[idx]
    return code


def generate_confirmation_key(identity_secret: str, tag: str, timestamp: int | None = None) -> str:
    ts = int(timestamp if timestamp is not None else time.time())
    buf = struct.pack(">Q", ts) + tag.encode("ascii")
    return base64.b64encode(hmac.new(base64.b64decode(identity_secret), buf, hashlib.sha1).digest()).decode()


def generate_device_id(steam_id64: str) -> str:
    h = hashlib.sha1(str(steam_id64).encode()).hexdigest()
    return f"android:{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"
