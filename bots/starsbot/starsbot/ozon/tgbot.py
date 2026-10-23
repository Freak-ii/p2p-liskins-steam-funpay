"""Telegram-бот активации для покупателей Ozon: код из заказа + @username → звёзды.

Работает через long polling (getUpdates), поэтому не нужен ни белый IP, ни сайт — подходит для запуска
на домашнем компьютере. Это ОТДЕЛЬНЫЙ бот (не тот, что шлёт уведомления продавцу): его видят покупатели.
"""
from __future__ import annotations

import asyncio
import logging
import time

import aiohttp

from starsbot.core.stars import username_from_text
from starsbot.ozon import texts as T

log = logging.getLogger("ozon.bot")
MAX_BAD_CODES_PER_HOUR = 6


class ActivationBot:
    def __init__(self, token: str, channel, *, prefix: str = "TGS", api: str = "https://api.telegram.org"):
        self.token = token
        self.channel = channel
        self.api = api
        prefix = T.normalize_code(prefix)[:6]
        self.example = f"{prefix}-XXXX-XXXX-XXXX" if prefix else "XXXX-XXXX-XXXX"
        self.username = ""
        self.pending: dict[int, str] = {}          # chat_id → код, для которого ждём username
        self.bad: dict[int, list[float]] = {}
        self.sent: list[tuple[int, str]] = []      # для тестов
        self._session: aiohttp.ClientSession | None = None

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45))
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def send(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))
        if not self.token:
            return
        try:
            sess = await self._sess()
            async with sess.post(f"{self.api}/bot{self.token}/sendMessage",
                                 json={"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True}) as r:
                if r.status != 200:
                    log.warning("бот активации: Telegram %s %s", r.status, (await r.text())[:200])
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.warning("бот активации: Telegram недоступен: %s", e)

    # ------------------------------------------------------------------ логика диалога
    def _limited(self, chat_id: int) -> bool:
        now = time.time()
        hits = [t for t in self.bad.get(chat_id, []) if now - t < 3600]
        self.bad[chat_id] = hits
        return len(hits) >= MAX_BAD_CODES_PER_HOUR

    def _pending_code(self, chat_id: int) -> str | None:
        if chat_id in self.pending:
            return self.pending[chat_id]
        for o in self.channel.orders_for_chat(chat_id):        # после перезапуска бота — из базы
            if o["status"] == "awaiting_username":
                return o["code"]
        return None

    async def handle(self, chat_id: int, text: str) -> str:
        """Ответ на сообщение покупателя ('' — ответ уже отправлен сообщениями о заказе)."""
        t = (text or "").strip()
        low = t.lower()
        if low.startswith("/start"):
            payload = t.split(maxsplit=1)[1] if len(t.split(maxsplit=1)) > 1 else ""
            if payload and T.find_code(payload.replace("_", "-")):
                return await self.handle(chat_id, payload.replace("_", "-"))
            return T.BOT["start"].format(example=self.example)
        if low.startswith("/help"):
            return T.BOT["help"]
        if low.startswith("/status"):
            rows = self.channel.orders_for_chat(chat_id)
            return "\n".join(f"{r['code']}: " + self.channel.status_line(r) for r in rows) or T.BOT["status_none"]
        code = T.find_code(t)
        if code:
            if self._limited(chat_id):
                return T.BOT["too_many"]
            rest = t.upper().replace(code, " ")
            chk = self.channel.check_code(code)
            if not chk.ok:
                if chk.text == T.BOT["bad_code"]:
                    self.bad.setdefault(chat_id, []).append(time.time())
                return chk.text
            if chk.need != "username":
                self.channel._bind(code, chat_id)
                return chk.text
            # username мог прийти в том же сообщении: ищем его в исходном регистре без кода
            uname = username_from_text(t.replace(code, " ").replace(code.lower(), " "), allow_bare=False) \
                if rest.strip() else None
            if uname:
                res = await self.channel.redeem(code, uname, tg_chat_id=chat_id)
                if res.need == "username":
                    self.pending[chat_id] = code
                return res.text
            self.pending[chat_id] = code
            self.channel._bind(code, chat_id)
            return chk.text
        pending = self._pending_code(chat_id)
        if pending:
            uname = username_from_text(t) or t
            res = await self.channel.redeem(pending, uname, tg_chat_id=chat_id)
            if res.need != "username":
                self.pending.pop(chat_id, None)
            return res.text
        return T.BOT["need_code"].format(example=self.example)

    # ------------------------------------------------------------------ long polling
    async def run(self) -> None:
        sess = await self._sess()
        try:
            async with sess.get(f"{self.api}/bot{self.token}/getMe") as r:
                me = await r.json()
                self.username = (me.get("result") or {}).get("username", "")
        except Exception as e:  # noqa: BLE001
            log.warning("бот активации: getMe: %s", e)
        offset = 0
        while True:
            try:
                async with sess.get(f"{self.api}/bot{self.token}/getUpdates",
                                    params={"timeout": 30, "offset": offset, "allowed_updates": '["message"]'}) as r:
                    if r.status == 409:
                        await self.channel.notifier.notify(
                            "CRITICAL", "ozon_bot_conflict", "Бот активации Ozon: этот токен уже опрашивает другая "
                            "программа. Покупатели не смогут активировать коды — укажите отдельного бота.")
                        return
                    if r.status == 401:
                        await self.channel.notifier.notify("CRITICAL", "ozon_bot_token",
                                                           "Бот активации Ozon: неверный токен (@BotFather)")
                        return
                    data = await r.json()
                for upd in data.get("result", []):
                    offset = upd["update_id"] + 1
                    msg = upd.get("message") or {}
                    chat = msg.get("chat") or {}
                    if chat.get("type") != "private" or not msg.get("text"):
                        continue
                    try:
                        reply = await self.handle(int(chat["id"]), msg["text"])
                    except Exception:  # noqa: BLE001
                        log.exception("бот активации: ошибка обработки")
                        reply = "Что-то пошло не так 🙏 Попробуйте ещё раз через минуту."
                    if reply:
                        await self.send(int(chat["id"]), reply)
                self.channel.engine.last_ok["ozon_bot"] = time.time()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("бот активации: %s", e)
                await asyncio.sleep(10)
