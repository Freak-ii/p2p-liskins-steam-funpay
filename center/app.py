"""Center: держит воркеры ботов, периодически собирает их сводки и тревоги в одно состояние,
которое показывает общая панель. Здесь же точка расширения для будущих модулей центра
(мониторинг, CRM, аналитика, ИИ-координатор): они получают доступ к Center и его EventStore.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from center.analytics import Analytics
from center.backup import Backup
from center.inventory import Inventory
from center.readiness import Readiness
from center.tgcontrol import TelegramControl
from center.updates import Updates
from center.bots import BotWorker, LisBotWorker, MannBotWorker, ShopBotWorker, StarsBotWorker, TgGiftWorker
from center.config import CenterConfig
from center.crm import Crm
from center.coordinator import Coordinator
from center.agents import Agents
from center.events import EventStore
from center.vault import SecretStore
from center.rtba import Rtba
from center.steamshare import SteamShare
from center.monitor import Monitor
from center.workers import DISABLED, MISSING_DEPS, RUNNING

log = logging.getLogger("center")


class Center:
    def __init__(self, home: Path, *, demo: bool = False):
        self.home = Path(home)
        self.cfg = CenterConfig(self.home, demo=demo)
        self.events = EventStore(self.home / "center.sqlite3")
        self.demo = demo
        self.listen: tuple[str, int] | None = None  # (host, port) панели центра; задаёт center.__main__
        self.vault = SecretStore(self.home)       # зашифрованные ключи ботов и центра (DPAPI / Fernet)
        self.steamshare = SteamShare(self)        # один аккаунт Steam на оба бота: реестр исходящих обменов
        restart = self.cfg["restart"]
        wm, wl, ws = self.cfg.worker("mannbot"), self.cfg.worker("lisbot"), self.cfg.worker("starsbot")
        self.workers: dict[str, BotWorker] = {
            "lisbot": LisBotWorker(self.home / "lisbot", self.events, port=wl.get("port", 8802), restart=restart,
                                   executable=wl.get("node") or "", demo=demo, vault=self.vault),
            "mannbot": MannBotWorker(self.home / "mannbot", self.events, port=wm.get("port", 8801), restart=restart,
                                     executable=wm.get("python") or "", demo=demo, vault=self.vault),
            "starsbot": StarsBotWorker(self.home / "starsbot", self.events, port=ws.get("port", 8803),
                                       restart=restart, executable=ws.get("python") or "", demo=demo, vault=self.vault),
            "shopbot": ShopBotWorker(self.home / "shopbot", self.events, port=self.cfg.worker("shopbot").get("port", 8806),
                                     restart=restart, executable=self.cfg.worker("shopbot").get("python") or "",
                                     demo=demo, vault=self.vault),
        }
        wg = self.cfg.worker("tggift")
        self.workers["tggift"] = TgGiftWorker(self.home / "tggift", self.events, port=wg.get("port", 8805),
                                              restart=restart, executable=wg.get("python") or "", demo=demo,
                                              vault=self.vault)
        for w in self.workers.values():             # адрес и токен реестра обменов (center/steamshare.py)
            w.extra_env = self.steamshare.bot_env
        if demo:                                    # история сделок LisBot за месяц; дальше он торгует на эмуляции LIS/Steam
            from center.demo import seed_lisbot
            seed_lisbot(self.workers["lisbot"].data_dir)
        self.summaries: dict[str, dict | None] = {n: None for n in self.workers}
        self.summary_at: dict[str, float] = {}
        self.quit_event = asyncio.Event()
        self._poll_task: asyncio.Task | None = None
        self.started_at = time.time()
        self.analytics = Analytics(self)
        self.crm = Crm(self)
        self.rtba = Rtba(self)
        self.monitor = Monitor(self)       # до записи «Центр запущен»: проверяет, не выключался ли центр аварийно
        for level, text in self.cfg.problems:      # ошибки и опечатки center.yaml — в журнал (оттуда в Telegram)
            self.events.add("center", level, "center.config", text)
        self.coordinator = Coordinator(self)
        self.agents = Agents(self)           # раздел «Агенты»: сами не запускаются (кроме agents.<id>.autostart)
        self.backup = Backup(self)
        self.inventory = Inventory(self)
        self.readiness = Readiness(self)
        self.tg = TelegramControl(self)
        self.updates = Updates(self)

    def enabled(self, name: str) -> bool:
        return bool(self.cfg.worker(name).get("enabled", True))

    async def start(self) -> None:
        self.events.add("center", "info", "center.start", "Центр запущен" + (" (демо)" if self.demo else ""))
        await self.monitor.start()
        await self.coordinator.start()
        await self.agents.start()
        await self.backup.start()
        await self.inventory.start()
        await self.tg.start()
        await self.updates.start()
        for name, w in self.workers.items():
            if not self.enabled(name):
                w._set(DISABLED, "выключен в center.yaml")
                continue
            problem = w.check_deps()
            if problem:
                w._set(MISSING_DEPS, problem)
                continue
            if self.cfg.autostart(name):
                await w.start("автозапуск центра")
            elif not w.detail:
                w._set(w.state, "не запущен: запустите кнопкой «Запустить»")
        self._poll_task = asyncio.create_task(self._poll_loop(), name="center-poll")

    async def shutdown(self, stop_timeout: float = 15) -> None:
        if self._poll_task:
            self._poll_task.cancel()
        await asyncio.gather(*(w.stop("завершение центра", timeout=stop_timeout) for w in self.workers.values()),
                             return_exceptions=True)
        await self.backup.stop()
        await self.inventory.stop()
        await self.tg.stop()
        await self.updates.stop()
        await self.agents.stop()
        await self.coordinator.stop()
        await self.monitor.stop()
        self.events.add("center", "info", "center.stop", "Центр остановлен")
        self.events.close()
        self.steamshare.close()

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
            try:
                await self.remote_inbox_once()
            except Exception:  # noqa: BLE001
                log.exception("remote_cmd.txt")
            await asyncio.gather(*(self.refresh(n) for n in self.workers), return_exceptions=True)
            await asyncio.sleep(float(self.cfg["center"].get("poll_seconds", 10)))

    # ------------------------------------------------------------------ удалённые команды файлом
    REMOTE_HELP = ("команды: status | <бот> start|stop|restart|autostart_on|autostart_off | "
                   "<бот> bot_start|bot_stop|pause|resume (действия панели бота); боты: lisbot, mannbot, starsbot")

    async def remote_command(self, line: str) -> str:
        parts = line.split()
        if not parts or parts[0].lower() in ("help", "помощь"):
            return self.REMOTE_HELP
        if parts[0].lower() == "status":
            rows = []
            for name, w in self.workers.items():
                sm = self.summaries.get(name) or {}
                rows.append(f"{name}: {w.state}{' · ' + w.detail if w.detail else ''}"
                            f"{' · торговля ' + str(sm.get('bot_state')) if sm.get('bot_state') else ''}"
                            f" · автозапуск {'вкл' if self.cfg.autostart(name) else 'выкл'}")
            return "\n".join(rows)
        name, action = parts[0].lower(), (parts[1].lower() if len(parts) > 1 else "")
        w = self.workers.get(name)
        if w is None:
            return f"неизвестный бот {name}; " + self.REMOTE_HELP
        if action == "start":
            return await w.start("файл remote_cmd.txt")
        if action == "stop":
            return await w.stop("файл remote_cmd.txt")
        if action == "restart":
            return await w.restart("файл remote_cmd.txt")
        if action in ("autostart_on", "autostart_off"):
            on = action == "autostart_on"
            self.cfg.save({"workers": {name: {"autostart": on}}})
            return f"{name}: автозапуск с центром {'включён' if on else 'выключен'}"
        if action in w.actions:
            if w.state != "running":
                return f"{w.title} не запущен"
            return await w.control(action)
        return f"неизвестное действие {action!r}; " + self.REMOTE_HELP

    async def remote_inbox_once(self) -> int:
        """Команды центру файлом <папка данных>/remote_cmd.txt (по строке, например «mannbot start»,
        «lisbot autostart_on», «status»). Файл забирается и удаляется, ответы дописываются в remote_cmd.out.txt.
        Так центром можно управлять через доступ к папке, без входа в панель (как data/remote_cmd.txt у StarsBot)."""
        inbox, out = self.home / "remote_cmd.txt", self.home / "remote_cmd.out.txt"
        if not inbox.exists():
            return 0
        work = self.home / "remote_cmd.work.txt"
        inbox.replace(work)
        lines = [ln.strip() for ln in work.read_text(encoding="utf-8-sig").splitlines()]
        work.unlink()
        done = 0
        for line in lines:
            if not line or line.startswith("#"):
                continue
            try:
                res = await self.remote_command(line)
            except Exception as e:  # noqa: BLE001
                res = f"ошибка: {e!r}"
            done += 1
            self.events.add("center", "info", "remote_cmd", f"remote_cmd.txt: {line} — {str(res)[:200]}")
            with out.open("a", encoding="utf-8") as fh:
                fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} > {line}\n{res}\n\n")
        return done

    def overview(self) -> dict:
        bots = []
        for name, w in self.workers.items():
            bots.append({**w.info(), "autostart": self.cfg.autostart(name), "autostart_allowed": bool(self.cfg["center"].get("bots_autostart")), "summary": self.summaries.get(name), "summary_at": self.summary_at.get(name)})
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
