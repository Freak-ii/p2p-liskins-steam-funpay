"""Проверка Steam для LisBot без входа в аккаунт: DNS, часы и код Steam Guard из maFile.

Вход в Steam тратит суточный лимит логинов, поэтому эта проверка не входит, а смотрит то, из-за чего вход обычно
ломается: системный DNS не находит адрес Steam («getaddrinfo ENOENT»), часы компьютера убежали и код SDA
не принимается, shared_secret скопирован неверно. Текущий код можно сверить с кодом в приложении SDA.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import re
import socket
import struct
import time

import aiohttp

SECRET_RE = re.compile(r"^[A-Za-z0-9+/]{27}=$")
CHARS = "23456789BCDFGHJKMNPQRTVWXY"
HOSTS = ("api.steampowered.com", "steamcommunity.com")
TIME_URL = "https://api.steampowered.com/ITwoFactorService/QueryTime/v0001"


def auth_code(shared_secret: str, ts: float) -> str:
    """Код Steam Guard (как в SDA и steam-totp) для момента ts."""
    key = base64.b64decode(shared_secret)
    digest = hmac.new(key, struct.pack(">Q", int(ts) // 30), hashlib.sha1).digest()
    start = digest[19] & 0x0F
    n = struct.unpack(">I", digest[start:start + 4])[0] & 0x7FFFFFFF
    out = ""
    for _ in range(5):
        out += CHARS[n % len(CHARS)]
        n //= len(CHARS)
    return out


def _resolve(host: str) -> str:
    return socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)[0][4][0]


async def check(secrets: dict, *, time_url: str = TIME_URL, hosts=HOSTS, resolve=_resolve) -> dict:
    """{ok, message, lines, code}. В тестах подставляются time_url, hosts и resolve."""
    lines: list[str] = []
    bad = False
    secret = str(secrets.get("STEAM_SHARED_SECRET") or "").strip()
    ident = str(secrets.get("STEAM_IDENTITY_SECRET") or "").strip()
    if not secret:
        lines.append("✗ shared_secret не задан: без него LisBot не получит код Steam Guard")
        bad = True
    elif not SECRET_RE.match(secret):
        lines.append("✗ shared_secret не похож на ключ из maFile (28 символов, в конце «=»). "
                     "Если maFile зашифрован, снимите шифрование в SDA или загрузите maFile кнопкой выше")
        bad = True
    if ident and not SECRET_RE.match(ident):
        lines.append("✗ identity_secret не похож на ключ из maFile (28 символов, в конце «=»)")
        bad = True

    loop = asyncio.get_running_loop()
    dns_ok = True
    for h in hosts:
        try:
            ip = await loop.run_in_executor(None, resolve, h)
            lines.append(f"✓ DNS: {h} → {ip}")
        except OSError as e:
            dns_ok = False
            lines.append(f"✗ DNS: компьютер не находит адрес {h} ({e}). LisBot в этом случае сам берёт адрес "
                         "у запасного DNS, но надёжнее поправить сеть: выключите VPN или прокси, "
                         "или пропишите DNS 1.1.1.1 / 8.8.8.8 в настройках сетевого адаптера")

    offset = None
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
            async with s.post(time_url, data={"steamid": "0"}) as r:
                d = await r.json(content_type=None)
        offset = int((d.get("response") or {}).get("server_time")) - int(time.time())
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError, TypeError) as e:
        lines.append(f"✗ Нет ответа от Steam ({e.__class__.__name__}): проверьте интернет, VPN, антивирус")
        bad = True
    if offset is not None:
        if abs(offset) >= 30:
            lines.append(f"⚠ Часы компьютера расходятся со Steam на {offset:+d} с. LisBot делает поправку сам, "
                         "но SDA и другие программы — нет: включите «Синхронизировать время автоматически» в Windows")
        else:
            lines.append(f"✓ Часы совпадают со Steam (разница {offset:+d} с)")

    code = ""
    if secret and SECRET_RE.match(secret):
        code = auth_code(secret, time.time() + (offset or 0))
        lines.append(f"Код Steam Guard сейчас: {code}. Он должен совпасть с кодом в SDA для этого аккаунта; "
                     "если не совпадает, в LisBot вставлен shared_secret от другого maFile")
    ok = not bad and dns_ok
    head = "Steam: всё в порядке, проверка не входила в аккаунт и не тратила лимит входов." if ok else \
        "Steam: есть проблемы, подробности ниже. Проверка не входила в аккаунт и не тратила лимит входов."
    return {"ok": ok, "message": head, "lines": lines, "code": code}
