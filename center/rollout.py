"""Орбита: обновление на ходу (серверный режим, CENTER_SERVER=1).

Как обновляется работающая система (подробно — docs/server.md, сценарий — deploy/orbita):

1. deploy/orbita готовит новый релиз в releases/<имя> (зависимости, проверка импорта), снимает копии баз
   и переключает ссылку current на новый релиз.
2. Пишет server/rollout-pending.json и просит работающий центр «передать эстафету» (POST /api/server/handover):
   центр выходит, НЕ останавливая ботов (их вывод и так идёт в файлы журналов), systemd сразу запускает центр
   уже из нового релиза. Панель недоступна 2–3 секунды, торговля в это время идёт.
3. Новый центр подхватывает работающих ботов и по одному перезапускает их на новом коде (Rollout.roll):
   бот → проверка, что он поднялся и не падает verify_seconds → следующий бот. Если бот на новом коде
   не поднялся или упал — он сразу возвращается на прежний релиз, обновление остальных останавливается,
   в журнал (и в Telegram) уходит критическое событие, а deploy/orbita возвращает центр на прежний релиз.

Остановленные боты не запускаются: правило «центр сам ботов не запускает» (PR #37) сохраняется.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

from center.config import ROOT
from center.workers import BACKOFF, CRASHED, RUNNING, STARTING, STOPPED, release_name

log = logging.getLogger("center.rollout")

# порядок обновления: сначала то, что меньше всего зарабатывает, StarsBot — последним
ORDER = ("shopbot", "tggift", "lisbot", "mannbot", "starsbot")


def release_meta(root: Path = ROOT) -> dict:
    """Описание релиза, которое кладёт deploy/orbita: коммит, ветка, время сборки."""
    try:
        return json.loads((Path(root) / "RELEASE.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


class Rollout:
    def __init__(self, center, *, verify_seconds: float | None = None, healthy_timeout: float = 120):
        self.c = center
        self.dir = Path(center.home) / "server"
        self.status_path = self.dir / "rollout.json"
        self.pending_path = self.dir / "rollout-pending.json"
        self.verify_seconds = float(verify_seconds if verify_seconds is not None
                                    else os.environ.get("ORBITA_VERIFY_SECONDS", 45))
        self.healthy_timeout = healthy_timeout
        self.task: asyncio.Task | None = None
        self.handover = False
        self.status: dict = self._load()

    @property
    def server_mode(self) -> bool:
        return any(w.detached for w in self.c.workers.values())

    def _load(self) -> dict:
        try:
            return json.loads(self.status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.status_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.status, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.status_path)
        except OSError as e:
            log.warning("rollout.json: %s", e)

    def info(self) -> dict:
        return {"server_mode": self.server_mode, "release": release_name(ROOT), "root": str(ROOT),
                "meta": release_meta(), "rollout": self.status, "busy": bool(self.task and not self.task.done()),
                "bots": {n: {"release": release_name(w.code_root), "state": w.state, "pid": w.pid}
                         for n, w in self.c.workers.items()}}

    # ------------------------------------------------------------------ эстафета
    def request_handover(self) -> str:
        """Центр выходит, оставляя ботов работать; systemd запускает центр уже из нового релиза."""
        if not self.server_mode:
            raise RuntimeError("передача эстафеты работает только в серверном режиме (CENTER_SERVER=1, Linux)")
        if self.task and not self.task.done():
            raise RuntimeError("идёт обновление ботов — дождитесь окончания")
        self.handover = True
        for w in self.c.workers.values():
            w.leave_running = True
        self.c.events.add("center", "info", "server.handover",
                          f"Центр {release_name(ROOT)} передаёт работу новой версии, боты не останавливаются")
        asyncio.get_running_loop().call_later(0.3, self.c.quit_event.set)
        return "центр перезапускается на новой версии, боты продолжают работать"

    # ------------------------------------------------------------------ после запуска центра
    async def start(self) -> None:
        if not self.server_mode or not self.pending_path.exists():
            return
        try:
            pending = json.loads(self.pending_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pending = {}
        self.pending_path.unlink(missing_ok=True)
        self.task = asyncio.create_task(self._after_handover(pending), name="rollout")

    async def _after_handover(self, pending: dict) -> None:
        # подождать, пока центр подхватит работающих ботов
        deadline = time.time() + 30
        await asyncio.sleep(1)
        while time.time() < deadline and any(w._want_running and w.state in (STOPPED, STARTING)
                                             for w in self.c.workers.values()):
            await asyncio.sleep(0.5)
        await self.roll(ROOT, reason=pending.get("reason") or "обновление")

    def start_roll(self, target: Path = ROOT, only: list[str] | None = None) -> str:
        if self.task and not self.task.done():
            raise RuntimeError("обновление уже идёт")
        self.task = asyncio.create_task(self.roll(Path(target), only=only), name="rollout")
        return "обновление ботов запущено"

    async def roll(self, target: Path, *, only: list[str] | None = None, reason: str = "обновление") -> dict:
        target = Path(target).resolve()
        name = release_name(target)
        self.status = {"state": "running", "target": name, "started_at": time.time(), "finished_at": None,
                       "reason": reason, "bots": {}}
        self._save()
        self.c.events.add("center", "info", "server.rollout", f"Обновление ботов до {name}: по одному, с проверкой")
        failed = None
        for bot in ORDER:
            w = self.c.workers.get(bot)
            if w is None or (only and bot not in only):
                continue
            if not w.alive or w.state != RUNNING:
                self.status["bots"][bot] = "не запущен — обновится при следующем запуске"
                if not w.alive:
                    w.code_root = target            # ручной «Запустить» поднимет бота уже на новом коде
                continue
            old = Path(w.code_root)
            if old.resolve() == target:
                self.status["bots"][bot] = "уже на этой версии"
                continue
            res = await self._roll_one(w, old, target)
            self.status["bots"][bot] = res
            self._save()
            if res != "обновлён":
                failed = bot
                break
        self.status["state"] = "failed" if failed else "done"
        self.status["finished_at"] = time.time()
        self._save()
        if failed:
            self.c.events.add("center", "critical", "server.rollout_failed",
                              f"Обновление до {name} остановлено: {self.c.workers[failed].title} не заработал на новом "
                              f"коде и возвращён на прежний релиз ({self.status['bots'][failed]})")
        else:
            self.c.events.add("center", "ok", "server.rollout_done", f"Все запущенные боты работают на {name}")
        return self.status

    async def _roll_one(self, w, old: Path, target: Path) -> str:
        w.code_root = target
        problem = w.check_deps()
        if problem:
            w.code_root = old
            return f"не обновлён: {problem}"
        await w.restart(f"обновление до {release_name(target)}")
        if await self._verify(w):
            return "обновлён"
        log.warning("%s: новый код не заработал — откат на %s", w.name, old)
        w.code_root = old
        await w.restart(f"откат на {release_name(old)}")
        ok = await self._verify(w)
        return "откат на прежний релиз" + ("" if ok else " (и там не поднялся — проверьте журнал)")

    async def _verify(self, w) -> bool:
        """Бот поднялся (RUNNING) и verify_seconds не падал и не перезапускался."""
        deadline = time.time() + self.healthy_timeout
        while w.state != RUNNING:
            if w.state in (CRASHED, BACKOFF) or time.time() > deadline or not w._want_running:
                return False
            await asyncio.sleep(0.5)
        restarts = w.restarts
        end = time.time() + self.verify_seconds
        while time.time() < end:
            if w.restarts != restarts or w.state != RUNNING or not w.alive:
                return False
            await asyncio.sleep(0.5)
        return True


def routes(center, web):
    async def status(request):
        return web.json_response(center.rollout.info())

    async def handover(request):
        try:
            return web.json_response({"ok": True, "message": center.rollout.request_handover()})
        except RuntimeError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=409)

    async def roll(request):
        body = await request.json() if request.can_read_body else {}
        try:
            msg = center.rollout.start_roll(Path(body.get("target") or ROOT), only=body.get("only"))
        except RuntimeError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=409)
        return web.json_response({"ok": True, "message": msg})

    return [web.get("/api/server/status", status), web.post("/api/server/handover", handover),
            web.post("/api/server/rollout", roll)]
