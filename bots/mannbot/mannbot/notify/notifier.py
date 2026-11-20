"""Уведомления: журнал алертов в БД + Telegram (с антиспамом) + команды управления ботом."""
from __future__ import annotations

import asyncio
import html
import logging
import time
from typing import Awaitable, Callable

import aiohttp

from mannbot.crm.db import Database

log = logging.getLogger("notify")
LEVELS = {"INFO": 10, "WARNING": 20, "CRITICAL": 30}
ICONS = {"INFO": "ℹ️", "WARNING": "⚠️", "CRITICAL": "🚨"}

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

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40))
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def notify(self, level: str, code: str, text: str, *, order_id: str | None = None,
                     throttle_key: str | None = None) -> None:
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
        await self.telegram(f"{ICONS.get(level, '')} <b>{level}</b> · <code>{code}</code>\n{html.escape(text)}")

    async def telegram(self, text: str) -> None:
        if not (self.tg.enabled and self.tg.bot_token and self.tg.admin_chat_ids):
            return
        sess = await self._sess()
        for chat_id in self.tg.admin_chat_ids:
            try:
                async with sess.post(f"https://api.telegram.org/bot{self.tg.bot_token}/sendMessage",
                                     json={"chat_id": chat_id, "text": text[:4000], "parse_mode": "HTML",
                                           "disable_web_page_preview": True}) as r:
                    if r.status != 200:
                        log.warning("Telegram %s: %s", r.status, (await r.text())[:200])
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.warning("Telegram недоступен: %s", e)

    async def poll_commands(self, settings: Callable[[], object] | None = None) -> None:
        """Long-poll Telegram: команды принимаются ТОЛЬКО из admin_chat_ids.
        settings — функция, возвращающая актуальный раздел notifications.telegram: тогда опрос живёт всё время
        работы приложения и подхватывает новые настройки без перезапуска (так его запускает Supervisor)."""
        offset, token, warned = 0, None, ""
        while True:
            tg = settings() if settings else self.tg
            self.tg = tg
            if not (tg.enabled and tg.commands and tg.bot_token and tg.admin_chat_ids):
                if not settings:
                    return
                await asyncio.sleep(15)
                continue
            if tg.bot_token != token:
                offset, token = 0, tg.bot_token
            allowed = {int(x) for x in tg.admin_chat_ids}
            try:
                sess = await self._sess()
                async with sess.get(f"https://api.telegram.org/bot{token}/getUpdates",
                                    params={"timeout": 30, "offset": offset}) as r:
                    data = await r.json(content_type=None)
                if not data.get("ok"):
                    # 409 — этот токен уже читает другая программа (часто LisBot с тем же ботом) или стоит webhook;
                    # 401 — токен неверный. Без паузы цикл крутился бы вхолостую, а команды молча не работали бы.
                    desc = str(data.get("description") or f"HTTP {r.status}")
                    hint = (" Команды получает другая программа с тем же ботом: у LisBot и MannBot должны быть разные "
                            "Telegram-боты (токены)." if data.get("error_code") == 409 else "")
                    if desc != warned:
                        warned = desc
                        await self.notify("WARNING", "telegram_commands", f"Команды Telegram не работают: {desc}.{hint}",
                                          throttle_key="telegram_commands")
                    await asyncio.sleep(30)
                    continue
                warned = ""
                for upd in data.get("result", []):
                    offset = upd["update_id"] + 1
                    msg = upd.get("message") or {}
                    chat_id = (msg.get("chat") or {}).get("id")
                    text = (msg.get("text") or "").strip()
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
                    await sess.post(f"https://api.telegram.org/bot{token}/sendMessage",
                                    json={"chat_id": chat_id, "text": reply[:4000]})
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("Telegram polling: %s", e)
                await asyncio.sleep(10)
