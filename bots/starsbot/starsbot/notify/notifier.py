"""Уведомления: журнал алертов в БД + Telegram (с антиспамом) + команды управления ботом."""
from __future__ import annotations

import asyncio
import html
import logging
import time
from typing import Awaitable, Callable

import aiohttp

from starsbot.crm.db import Database

log = logging.getLogger("notify")
LEVELS = {"INFO": 10, "WARNING": 20, "CRITICAL": 30}
ICONS = {"INFO": "ℹ️", "WARNING": "⚠️", "CRITICAL": "🚨"}

# что можно сделать кнопкой: обратимые действия (возврат денег — только командой /refund или в панели)
BUTTON_COMMANDS = {"approve", "reject", "status", "orders", "balance", "withdraw_ok", "withdraw_no",
                   "treasury_ok", "treasury_no", "treasury_done", "treasury_skip", "treasury"}

CommandHandler = Callable[[str, list[str]], Awaitable[str]]


class Notifier:
    def __init__(self, cfg, db: Database):
        self.cfg = cfg
        self.db = db
        self.tg = cfg.telegram
        self._last: dict[str, float] = {}
        self._session: aiohttp.ClientSession | None = None
        self.command_handler: CommandHandler | None = None
        self.sent: list[tuple[str, str]] = []   # для тестов/демо
        self.api = "https://api.telegram.org"

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40))
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def notify(self, level: str, code: str, text: str, *, order_id: str | None = None,
                     throttle_key: str | None = None, buttons: list[tuple[str, str]] | None = None) -> None:
        """buttons: [(надпись, "команда аргументы")] — кнопки под сообщением в Telegram (только безопасные команды)."""
        level = level.upper()
        key = throttle_key or f"{code}:{order_id or ''}"
        now = time.time()
        if now - self._last.get(key, 0) < float(self.cfg.throttle_seconds) and level != "CRITICAL":
            return
        if level == "CRITICAL" and now - self._last.get(key, 0) < 60:
            return
        self._last[key] = now
        self.db.add_alert(level, code, text, order_id)
        getattr(log, "critical" if level == "CRITICAL" else level.lower())("[%s] %s", code, text)
        self.sent.append((level, text))
        if LEVELS.get(level, 10) < LEVELS.get(str(self.cfg.min_level).upper(), 10):
            return
        prefix = str(self.cfg.get("prefix") or "")
        await self.telegram(f"{ICONS.get(level, '')} {html.escape(prefix) + ' · ' if prefix else ''}<b>{level}</b> · "
                            f"<code>{code}</code>\n{html.escape(text)}", buttons=buttons)

    async def telegram(self, text: str, buttons: list[tuple[str, str]] | None = None) -> None:
        if not (self.tg.enabled and self.tg.bot_token and self.tg.admin_chat_ids):
            return
        sess = await self._sess()
        for chat_id in self.tg.admin_chat_ids:
            try:
                body = {"chat_id": chat_id, "text": text[:4000], "parse_mode": "HTML",
                        "disable_web_page_preview": True}
                if buttons and self.tg.commands:
                    body["reply_markup"] = {"inline_keyboard": [[{"text": t, "callback_data": f"cmd:{c}"[:64]}
                                                                 for t, c in buttons]]}
                async with sess.post(f"{self.api}/bot{self.tg.bot_token}/sendMessage", json=body) as r:
                    if r.status != 200:
                        log.warning("Telegram %s: %s", r.status, (await r.text())[:200])
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.warning("Telegram недоступен: %s", e)

    async def poll_commands(self) -> None:
        """Long-poll Telegram: команды принимаются ТОЛЬКО из admin_chat_ids."""
        if not (self.tg.enabled and self.tg.commands and self.tg.bot_token):
            return
        allowed = {int(x) for x in self.tg.admin_chat_ids}
        offset = 0
        sess = await self._sess()
        while True:
            try:
                async with sess.get(f"{self.api}/bot{self.tg.bot_token}/getUpdates",
                                    params={"timeout": 30, "offset": offset}) as r:
                    data = await r.json()
                    if r.status == 409:
                        # тот же токен опрашивает другая программа (например, MannBot) — не воюем за обновления
                        await self.notify("WARNING", "telegram_conflict", "Telegram-бот с этим токеном уже принимает "
                                          "команды в другой программе. Команды StarsBot отключены — заведите для "
                                          "StarsBot отдельного бота у @BotFather. Уведомления продолжают приходить.")
                        return
                for upd in data.get("result", []):
                    offset = upd["update_id"] + 1
                    msg = upd.get("message") or {}
                    chat_id = (msg.get("chat") or {}).get("id")
                    text = (msg.get("text") or "").strip()
                    cb = upd.get("callback_query")
                    if cb:                                   # нажатие кнопки под уведомлением
                        chat_id = ((cb.get("message") or {}).get("chat") or {}).get("id")
                        await sess.post(f"{self.api}/bot{self.tg.bot_token}/answerCallbackQuery",
                                        json={"callback_query_id": cb.get("id")})
                        data_ = str(cb.get("data") or "")
                        text = "/" + data_[4:] if data_.startswith("cmd:") and data_[4:].split()[0] in \
                            BUTTON_COMMANDS else ""
                    if chat_id not in allowed or not text.startswith("/"):
                        continue
                    parts = text.split()
                    cmd = parts[0].split("@")[0].lstrip("/").lower()
                    reply = "обработчик команд не подключён"
                    if self.command_handler:
                        try:
                            reply = await self.command_handler(cmd, parts[1:])
                        except Exception as e:  # noqa: BLE001
                            reply = f"Ошибка: {e}"
                    await sess.post(f"{self.api}/bot{self.tg.bot_token}/sendMessage",
                                    json={"chat_id": chat_id, "text": reply[:4000]})
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("Telegram polling: %s", e)
                await asyncio.sleep(10)
