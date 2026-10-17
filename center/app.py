"""Center: держит воркеры ботов, периодически собирает их сводки и тревоги в одно состояние,
которое показывает общая панель. Здесь же точка расширения для будущих модулей центра
(мониторинг, CRM, аналитика, ИИ-координатор): они получают доступ к Center и его EventStore.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from center.bots import BotWorker, LisBotWorker, MannBotWorker
from center.config import CenterConfig
from center.events import EventStore
from center.workers import DISABLED, MISSING_DEPS, RUNNING

log = logging.getLogger("center")


class Center:
    def __init__(self, home: Path, *, demo: bool = False):
        self.home = Path(home)
        self.cfg = CenterConfig(self.home)
        self.events = EventStore(self.home / "center.sqlite3")
        self.demo = demo
        restart = self.cfg["restart"]
        wm, wl = self.cfg.worker("mannbot"), self.cfg.worker("lisbot")
        self.workers: dict[str, BotWorker] = {
            "lisbot": LisBotWorker(self.home / "lisbot", self.events, port=wl.get("port", 8802), restart=restart,
                                   executable=wl.get("node") or ""),
            "mannbot": MannBotWorker(self.home / "mannbot", self.events, port=wm.get("port", 8801), restart=restart,
                                     executable=wm.get("python") or "", demo=demo),
        }
        self.summaries: dict[str, dict | None] = {n: None for n in self.workers}
        self.summary_at: dict[str, float] = {}
        self.quit_event = asyncio.Event()
        self._poll_task: asyncio.Task | None = None
        self.started_at = time.time()

    def enabled(self, name: str) -> bool:
        return bool(self.cfg.worker(name).get("enabled", True))

    async def start(self) -> None:
        self.events.add("center", "info", "center.start", "Центр запущен" + (" (демо)" if self.demo else ""))
        for name, w in self.workers.items():
            if not self.enabled(name):
                w._set(DISABLED, "выключен в center.yaml")
                continue
            problem = w.check_deps()
            if problem:
                w._set(MISSING_DEPS, problem)
                continue
            if self.cfg.worker(name).get("autostart", True):
                await w.start("автозапуск центра")
        self._poll_task = asyncio.create_task(self._poll_loop(), name="center-poll")

    async def shutdown(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
        await asyncio.gather(*(w.stop("завершение центра") for w in self.workers.values()), return_exceptions=True)
        self.events.add("center", "info", "center.stop", "Центр остановлен")
        self.events.close()

    async def refresh(self, name: str) -> None:
        w = self.workers[name]
        if w.state != RUNNING:
            self.summaries[name] = None
            return
        try:
            self.summaries[name] = await w.summary()
            self.summary_at[name] = time.time()
        except Exception as e:  # noqa: BLE001 — бот может быть занят/перезапускаться
            log.debug("summary %s: %s", name, e)

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.gather(*(self.refresh(n) for n in self.workers), return_exceptions=True)
            await asyncio.sleep(float(self.cfg["center"].get("poll_seconds", 10)))

    def overview(self) -> dict:
        bots = []
        for name, w in self.workers.items():
            bots.append({**w.info(), "summary": self.summaries.get(name), "summary_at": self.summary_at.get(name)})
        alerts = self.alerts(30)
        return {
            "uptime": round(time.time() - self.started_at), "demo": self.demo, "bots": bots,
            "alerts": alerts, "open_alerts": sum(1 for a in alerts if not a["acked"] and a["level"] not in ("info", "ok")),
            "events": self.events.recent(30),
        }

    def alerts(self, limit: int = 50) -> list[dict]:
        out: list[dict] = []
        for w in self.workers.values():
            try:
                out += w.alerts(limit)
            except Exception as e:  # noqa: BLE001
                log.debug("alerts %s: %s", w.name, e)
        out.sort(key=lambda a: a["ts"], reverse=True)
        return out[:limit]
