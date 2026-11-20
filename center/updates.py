"""Новая версия центра: раз в сутки центр смотрит выпуски center-v* на GitHub и, если есть версия новее,
показывает её в панели и один раз сообщает в журнал (оттуда в Telegram). Сам центр ничего не скачивает.
Выключается в center.yaml: center.update_check: false.
"""
from __future__ import annotations

import asyncio
import os
import logging
import re
import time

import aiohttp
from aiohttp import web

from center import __version__

log = logging.getLogger("center.updates")
REPO = "Freak-ii/p2p-liskins-steam-funpay"
RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases?per_page=30"


def version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:4])


class Updates:
    def __init__(self, center, clock=time.time):
        self.center = center
        self.clock = clock
        self.state: dict = {"current": __version__, "latest": None, "url": None, "newer": False, "checked_at": None, "error": None}
        self._task: asyncio.Task | None = None

    @property
    def enabled(self) -> bool:
        if os.environ.get("CENTER_DESKTOP_UPDATES") == "1":    # обновлениями занимается оболочка Windows (packaging/windows/desktop)
            return False
        return bool(self.center.cfg["center"].get("update_check", True)) and not self.center.demo

    async def _fetch(self) -> list[dict]:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
            async with s.get(RELEASES_URL, headers={"Accept": "application/vnd.github+json"}) as r:
                if r.status != 200:
                    raise RuntimeError(f"GitHub ответил {r.status}")
                return await r.json()

    async def check(self) -> dict:
        try:
            releases = await self._fetch()
        except Exception as e:  # noqa: BLE001 — нет сети или репозиторий закрыт: молча пробуем завтра
            self.state.update(error=str(e) or type(e).__name__, checked_at=self.clock())
            return self.state
        best = None
        for rel in releases:
            tag = str(rel.get("tag_name") or "")
            if not tag.startswith("center-v") or rel.get("draft"):
                continue
            if best is None or version_tuple(tag) > version_tuple(best["tag_name"]):
                best = rel
        latest = best["tag_name"][len("center-v"):] if best else None
        newer = bool(latest) and version_tuple(latest) > version_tuple(__version__)
        self.state.update(latest=latest, url=best.get("html_url") if best else None, newer=newer,
                          checked_at=self.clock(), error=None)
        mon = self.center.monitor
        if newer and mon.kv_get("update_notified") != latest:
            mon.kv_set("update_notified", latest)
            self.center.events.add("center", "info", "center.update",
                                   f"Вышла новая версия центра {latest} (у вас {__version__}): {self.state['url']}")
        return self.state

    async def _loop(self) -> None:
        await asyncio.sleep(60)                     # не мешаем запуску ботов
        while True:
            if self.enabled:
                await self.check()
            await asyncio.sleep(86400)

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="center-updates")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()


def routes(center) -> list:
    async def update(request: web.Request):
        return web.json_response(center.updates.state | {"enabled": center.updates.enabled})

    return [web.get("/api/update", update)]
