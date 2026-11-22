"""Клиент API Авито: мессенджер (чаты объявлений) и цены объявлений.

Авторизация: OAuth2 client_credentials — POST /token (client_id, client_secret) → access_token на ~24 часа,
дальше заголовок Authorization: Bearer. Ключи выдаются в профиле: Авито → Для профессионалов → API.
Мессенджер API доступен не на всех тарифах: если Авито отвечает 403 на чаты, проверьте тариф/подписку.

Методы (если Авито поменяет версии или поля, править нужно только этот файл):
  GET  /core/v1/accounts/self                                   — номер профиля (user_id)
  GET  /messenger/v2/accounts/{user_id}/chats                   — чаты (с объявлением в context)
  GET  /messenger/v3/accounts/{user_id}/chats/{chat_id}/messages/ — сообщения чата
  POST /messenger/v1/accounts/{user_id}/chats/{chat_id}/messages  — отправить текст
  POST /messenger/v1/accounts/{user_id}/chats/{chat_id}/read      — отметить прочитанным
  GET  /core/v1/items                                           — свои объявления
  POST /core/v1/items/{item_id}/update_price                    — новая цена объявления
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import aiohttp

log = logging.getLogger("avito")
BASE = "https://api.avito.ru"


class AvitoError(Exception):
    def __init__(self, text: str, code: str = "api"):
        super().__init__(text)
        self.code = code


class AvitoAuthError(AvitoError):
    def __init__(self, text: str, code: str = "auth"):
        super().__init__(text, code)


@dataclass
class AvitoChat:
    chat_id: str
    item_id: int
    item_title: str
    buyer_id: int
    buyer_name: str
    last_message_id: str
    last_created: float
    last_direction: str            # in | out
    updated: float


@dataclass
class AvitoMessage:
    message_id: str
    author_id: int
    text: str
    created: float
    direction: str                 # in — от покупателя или Авито, out — наше
    kind: str                      # text | system | image | ...

    @property
    def is_system(self) -> bool:
        return self.kind == "system" or self.author_id == 0


def _num(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def parse_chat(raw: dict, me: int) -> AvitoChat:
    ctx = (raw.get("context") or {}).get("value") or {}
    users = raw.get("users") or []
    buyer = next((u for u in users if int(u.get("id") or 0) != int(me)), {}) if users else {}
    last = raw.get("last_message") or {}
    return AvitoChat(chat_id=str(raw.get("id") or ""), item_id=int(ctx.get("id") or 0),
                     item_title=str(ctx.get("title") or ""), buyer_id=int(buyer.get("id") or 0),
                     buyer_name=str(buyer.get("name") or "Покупатель Авито"),
                     last_message_id=str(last.get("id") or ""), last_created=_num(last.get("created")),
                     last_direction=str(last.get("direction") or ""), updated=_num(raw.get("updated")))


def parse_message(raw: dict) -> AvitoMessage:
    content = raw.get("content") or {}
    text = content.get("text")
    if text is None and isinstance(content.get("flow_id"), str):
        text = content.get("flow_id")
    return AvitoMessage(message_id=str(raw.get("id") or ""), author_id=int(raw.get("author_id") or 0),
                        text=str(text or ""), created=_num(raw.get("created")),
                        direction=str(raw.get("direction") or "in"), kind=str(raw.get("type") or "text"))


class AvitoClient:
    def __init__(self, client_id: str, client_secret: str, *, user_id: int = 0, base: str = BASE,
                 dry_run: bool = False, min_interval: float = 0.35):
        self.client_id = str(client_id or "").strip()
        self.client_secret = str(client_secret or "").strip()
        self.user_id = int(user_id or 0)
        self.base = (base or BASE).rstrip("/")
        self.dry_run = dry_run
        self.min_interval = min_interval
        self._token = ""
        self._token_until = 0.0
        self._session: aiohttp.ClientSession | None = None
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40))
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def token(self, force: bool = False) -> str:
        if self._token and not force and time.time() < self._token_until:
            return self._token
        if not self.client_id or not self.client_secret:
            raise AvitoAuthError("не заданы client_id и client_secret Авито", "config")
        sess = await self._sess()
        try:
            async with sess.post(self.base + "/token", data={"grant_type": "client_credentials",
                                                             "client_id": self.client_id,
                                                             "client_secret": self.client_secret}) as r:
                text = await r.text()
                if r.status >= 400:
                    raise AvitoAuthError(f"Авито /token {r.status}: {text[:200]}")
                data = await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise AvitoError(f"Авито недоступен: {e}", "network") from e
        self._token = str(data.get("access_token") or "")
        if not self._token:
            raise AvitoAuthError(f"Авито не выдал токен: {str(data)[:200]}")
        self._token_until = time.time() + max(60.0, float(data.get("expires_in") or 86400) - 300)
        return self._token

    async def call(self, method: str, path: str, *, json: dict | None = None, params: dict | None = None) -> Any:
        async with self._lock:
            gap = self.min_interval - (time.monotonic() - self._last)
            if gap > 0:
                await asyncio.sleep(gap)
            self._last = time.monotonic()
        sess = await self._sess()
        refreshed = False
        for attempt in range(3):
            tok = await self.token()
            try:
                async with sess.request(method, self.base + path, json=json, params=params,
                                        headers={"Authorization": f"Bearer {tok}"}) as r:
                    text = await r.text()
                    if r.status == 401 and not refreshed:
                        refreshed = True
                        await self.token(force=True)
                        continue
                    if r.status in (401, 403):
                        raise AvitoAuthError(f"Авито {r.status}: {text[:300]}")
                    if r.status == 429 or r.status >= 500:
                        if attempt < 2:
                            await asyncio.sleep(float(r.headers.get("Retry-After") or 2 * (attempt + 1)))
                            continue
                        raise AvitoError(f"Авито {r.status}: {text[:300]}", "rate_limit" if r.status == 429 else
                                         "network")
                    if not text.strip():
                        return {}
                    try:
                        data = await r.json(content_type=None)
                    except ValueError as e:
                        raise AvitoError(f"Авито вернул не JSON ({r.status}): {text[:200]}", "layout") from e
                    if r.status >= 400:
                        err = data.get("error") if isinstance(data, dict) else None
                        msg = (err.get("message") if isinstance(err, dict) else err) or text[:300]
                        raise AvitoError(f"Авито {r.status}: {msg}", "api")
                    return data
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt < 2:
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                raise AvitoError(f"Авито недоступен: {e}", "network") from e
        raise AvitoError("Авито недоступен", "network")  # pragma: no cover

    # ------------------------------------------------------------------ методы
    async def me(self) -> int:
        if not self.user_id:
            data = await self.call("GET", "/core/v1/accounts/self")
            self.user_id = int(data.get("id") or 0)
            if not self.user_id:
                raise AvitoError(f"Авито не вернул номер профиля: {str(data)[:200]}", "layout")
        return self.user_id

    async def list_chats(self, *, unread_only: bool = False, limit: int = 50) -> list[AvitoChat]:
        me = await self.me()
        data = await self.call("GET", f"/messenger/v2/accounts/{me}/chats",
                               params={"unread_only": "true" if unread_only else "false", "limit": limit,
                                       "chat_types": "u2i"})
        return [parse_chat(c, me) for c in (data.get("chats") or [])]

    async def messages(self, chat_id: str, limit: int = 50) -> list[AvitoMessage]:
        """Сообщения чата, от старых к новым."""
        me = await self.me()
        data = await self.call("GET", f"/messenger/v3/accounts/{me}/chats/{chat_id}/messages/",
                               params={"limit": limit})
        raw = data if isinstance(data, list) else (data.get("messages") or [])
        out = [parse_message(m) for m in raw]
        return sorted(out, key=lambda m: m.created)

    async def send(self, chat_id: str, text: str) -> str:
        me = await self.me()
        if self.dry_run:
            log.info("[dry-run] Авито %s ← %s", chat_id, text[:120])
            return "dry-run"
        data = await self.call("POST", f"/messenger/v1/accounts/{me}/chats/{chat_id}/messages",
                               json={"type": "text", "message": {"text": text[:1000]}})
        return str((data or {}).get("id") or "")

    async def mark_read(self, chat_id: str) -> None:
        me = await self.me()
        if self.dry_run:
            return
        await self.call("POST", f"/messenger/v1/accounts/{me}/chats/{chat_id}/read")

    async def items(self) -> list[dict]:
        data = await self.call("GET", "/core/v1/items", params={"per_page": 100, "status": "active"})
        return list(data.get("resources") or [])

    async def update_price(self, item_id: int, price: int) -> None:
        if self.dry_run:
            log.info("[dry-run] цена объявления Авито %s не изменена → %s ₽", item_id, price)
            return
        await self.call("POST", f"/core/v1/items/{int(item_id)}/update_price", json={"price": int(price)})
