"""Клиент FunPay через веб-интерфейс (cookie golden_key + csrf из data-app-data)."""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from typing import Any

import aiohttp
from yarl import URL

from mannbot.core.models import ChatMessage, CompetitorLot, FunPayOrder
from mannbot.funpay import parsers as P

log = logging.getLogger("funpay")
BASE = "https://funpay.com"


class FunPayError(Exception):
    pass


class FunPayAuthError(FunPayError):
    """golden_key недействителен / сессия разлогинена — критическая ситуация."""


class FunPayClient:
    def __init__(self, golden_key: str, user_agent: str, *, dry_run: bool = False):
        self.golden_key = golden_key
        self.user_agent = user_agent
        self.dry_run = dry_run
        self.session: aiohttp.ClientSession | None = None
        self.csrf: str = ""
        self.user_id: int = 0
        self.username: str = ""
        self.locale: str = "ru"
        self._tags: dict[str, str] = {}
        self._last_request = 0.0
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ http
    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            jar = aiohttp.CookieJar()
            self.session = aiohttp.ClientSession(
                cookie_jar=jar, timeout=aiohttp.ClientTimeout(total=30),
                headers={"User-Agent": self.user_agent, "Accept-Language": "ru,en;q=0.8"})
            self.session.cookie_jar.update_cookies({"golden_key": self.golden_key},
                                                   response_url=URL(BASE))
        return self.session

    async def _request(self, method: str, path: str, *, ajax: bool = False, retries: int = 3,
                       **kw: Any) -> tuple[int, str]:
        sess = await self._ensure_session()
        url = path if path.startswith("http") else BASE + path
        headers = kw.pop("headers", {})
        if ajax:
            headers.update({"X-Requested-With": "XMLHttpRequest", "Accept": "application/json, text/javascript, */*"})
        for attempt in range(1, retries + 1):
            async with self._lock:  # не чаще ~3 запросов в секунду
                wait = 0.35 - (time.monotonic() - self._last_request)
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last_request = time.monotonic()
            try:
                async with sess.request(method, url, headers=headers, allow_redirects=True, **kw) as r:
                    body = await r.text()
                    if r.status == 429 or r.status >= 500:
                        raise FunPayError(f"HTTP {r.status}")
                    if r.status in (401, 403):
                        raise FunPayAuthError(f"HTTP {r.status} на {path}")
                    if "/account/login" in str(r.url) and "login" not in path:
                        raise FunPayAuthError("FunPay перенаправил на страницу входа — golden_key истёк")
                    return r.status, body
            except FunPayAuthError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, FunPayError) as e:
                if attempt == retries:
                    raise FunPayError(f"{method} {path}: {e}") from e
                await asyncio.sleep(1.5 * attempt + random.random())
        raise FunPayError("unreachable")

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()

    # ------------------------------------------------------------------ auth
    async def login(self) -> None:
        _, page = await self._request("GET", "/")
        data = P.parse_app_data(page)
        if not data.get("userId"):
            raise FunPayAuthError("не удалось войти на FunPay: проверьте golden_key")
        self.csrf = data.get("csrf-token", "")
        self.user_id = int(data["userId"])
        self.locale = data.get("locale", "ru")
        self.username = P.parse_username(page) or ""
        log.info("FunPay: вошли как %s (id=%s)", self.username, self.user_id)

    # ------------------------------------------------------------------ runner
    async def runner(self, objects: list[dict], request: dict | None = None) -> dict:
        form = {"objects": json.dumps(objects), "request": json.dumps(request) if request else "false",
                "csrf_token": self.csrf}
        _, body = await self._request("POST", "/runner/", ajax=True, data=form)
        try:
            return json.loads(body)
        except json.JSONDecodeError as e:
            raise FunPayError(f"runner: не JSON ({body[:120]!r})") from e

    async def poll_updates(self) -> dict:
        """Опрос счётчиков заказов и списка диалогов. Возвращает
        {'orders_changed': bool, 'bookmarks': [ChatBookmark], 'counters': {...}}."""
        objs = []
        for typ in ("orders_counters", "chat_bookmarks"):
            objs.append({"type": typ, "id": str(self.user_id), "tag": self._tags.get(typ, "00000000"),
                         "data": False})
        resp = await self.runner(objs)
        result: dict[str, Any] = {"orders_changed": False, "bookmarks": [], "counters": {}}
        for obj in resp.get("objects", []):
            typ = obj.get("type")
            old = self._tags.get(typ)
            self._tags[typ] = obj.get("tag", old)
            data = obj.get("data") or {}
            if typ == "orders_counters":
                result["counters"] = data
                result["orders_changed"] = old != obj.get("tag")
            elif typ == "chat_bookmarks" and isinstance(data, dict) and data.get("html"):
                result["bookmarks"] = P.parse_chat_bookmarks(data["html"])
        return result

    # ------------------------------------------------------------------ chat
    async def get_chat_history(self, chat_id: str, last_message: int = 0) -> list[ChatMessage]:
        _, body = await self._request("GET", f"/chat/history?node={chat_id}&last_message={last_message}",
                                      ajax=True)
        data = json.loads(body)
        out: list[ChatMessage] = []
        prev: tuple[int, str] | None = None
        for m in (data.get("chat") or {}).get("messages", []):
            parsed = P.parse_chat_messages(m.get("html", ""), chat_id, prev)
            for msg in parsed:
                if m.get("author") is not None:
                    msg.author_id = int(m["author"])
                    msg.is_system = msg.is_system or msg.author_id == 0
                msg.message_id = int(m.get("id", msg.message_id))
                prev = (msg.author_id, msg.author_name)
                out.append(msg)
        return [m for m in out if m.message_id > last_message]

    async def send_message(self, chat_id: str, text: str) -> bool:
        if self.dry_run:
            log.info("[DRY-RUN] сообщение в чат %s:\n%s", chat_id, text)
            return True
        obj = {"type": "chat_node", "id": str(chat_id), "tag": "00000000",
               "data": {"node": str(chat_id), "last_message": -1, "content": ""}}
        req = {"action": "chat_message", "data": {"node": str(chat_id), "last_message": -1, "content": text}}
        resp = await self.runner([obj], req)
        r = resp.get("response") or {}
        if r.get("error"):
            raise FunPayError(f"сообщение не отправлено: {r.get('error')}")
        return True

    # ------------------------------------------------------------------ orders
    async def get_sales(self, state: str | None = None) -> list[FunPayOrder]:
        path = "/orders/trade" + (f"?state={state}" if state else "")
        _, page = await self._request("GET", path)
        return P.parse_orders_list(page)

    async def get_order(self, order_id: str) -> P.OrderPage:
        _, page = await self._request("GET", f"/orders/{order_id}/")
        return P.parse_order_page(page, order_id)

    async def refund(self, order_id: str) -> bool:
        if self.dry_run:
            log.warning("[DRY-RUN] возврат по заказу %s", order_id)
            return True
        _, body = await self._request("POST", "/orders/refund", ajax=True,
                                      data={"id": order_id, "csrf_token": self.csrf})
        data = json.loads(body)
        if data.get("error"):
            raise FunPayError(f"возврат не выполнен: {data.get('msg') or data}")
        return True

    # ------------------------------------------------------------------ lots
    async def get_lot_fields(self, offer_id: int, node_id: int) -> dict[str, str]:
        _, page = await self._request("GET", f"/lots/offerEdit?node={node_id}&offer={offer_id}")
        fields = P.parse_offer_edit_form(page)
        if not fields:
            raise FunPayError(f"не удалось открыть редактор лота {offer_id}")
        return fields

    async def save_lot(self, fields: dict[str, str]) -> None:
        if self.dry_run:
            log.info("[DRY-RUN] сохранение лота %s: price=%s amount=%s active=%s", fields.get("offer_id"),
                     fields.get("price"), fields.get("amount"), fields.get("active"))
            return
        form = dict(fields)
        form["csrf_token"] = self.csrf
        _, body = await self._request("POST", "/lots/offerSave", ajax=True, data=form)
        data = json.loads(body)
        if data.get("error") or data.get("errors"):
            raise FunPayError(f"лот не сохранён: {data.get('errors') or data.get('error')}")

    async def update_lot(self, offer_id: int, node_id: int, *, price: float | None = None,
                         amount: int | None = None, active: bool | None = None) -> dict[str, str]:
        fields = await self.get_lot_fields(offer_id, node_id)
        if price is not None:
            fields["price"] = f"{price:.2f}"
        if amount is not None:
            fields["amount"] = str(int(amount))
        if active is not None:
            if active:
                fields["active"] = "on"
            else:
                fields.pop("active", None)
        await self.save_lot(fields)
        return fields

    async def raise_lots(self, game_id: int, node_id: int) -> tuple[bool, str]:
        if self.dry_run:
            return True, "dry-run"
        _, body = await self._request("POST", "/lots/raise", ajax=True,
                                      data={"game_id": game_id, "node_id": node_id, "node_ids[]": node_id})
        data = json.loads(body)
        return (not data.get("error"), str(data.get("msg", "")))

    async def get_public_lots(self, node_id: int, filter_regex: str | None = None) -> list[CompetitorLot]:
        _, page = await self._request("GET", f"/lots/{node_id}/")
        return P.parse_lots_page(page, filter_regex)
