"""Воркеры центра: каждый бот работает отдельным процессом под присмотром центра.

Worker умеет запускать процесс, собирать его вывод (кольцевой буфер + файл), замечать падения
и перезапускать с растущей паузой. Если бот падает сразу после запуска несколько раз подряд,
центр перестаёт его перезапускать и пишет критическое событие: так ошибка настройки не превращается
в бесконечный цикл перезапусков. Конкретные боты (команда запуска, проверка здоровья, сводка)
описаны в center/bots.py.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from collections import deque
from pathlib import Path

from center import winutil
from center.events import EventStore

log = logging.getLogger("center.workers")

# Состояния процесса
STOPPED = "stopped"            # не запущен (вручную или ещё не стартовал)
STARTING = "starting"          # процесс запущен, ждём, когда ответит
RUNNING = "running"            # процесс жив и отвечает
BACKOFF = "backoff"            # упал, ждём перед перезапуском
CRASHED = "crashed"            # упал несколько раз подряд — перезапуск остановлен
NOT_CONFIGURED = "not_configured"   # бот сообщил, что не заполнены настройки
MISSING_DEPS = "missing_deps"  # не хватает зависимостей (node, node_modules, python-пакетов)
DISABLED = "disabled"          # выключен в center.yaml

STATE_TEXT = {
    STOPPED: "остановлен", STARTING: "запускается", RUNNING: "работает", BACKOFF: "перезапуск после сбоя",
    CRASHED: "упал, перезапуск остановлен", NOT_CONFIGURED: "нужна настройка", MISSING_DEPS: "нет зависимостей",
    DISABLED: "выключен",
}


class Worker:
    """Базовый воркер. Наследники задают name/title и методы command(), env(), check_deps(), health()."""

    name = "worker"
    title = "Worker"
    log_max_bytes = 5_000_000

    def __init__(self, home: Path, events: EventStore, *, restart: dict | None = None):
        self.home = Path(home)
        self.events = events
        r = restart or {}
        self.backoff_start = float(r.get("backoff_start", 2))
        self.backoff_max = float(r.get("backoff_max", 300))
        self.fast_fail_seconds = float(r.get("fast_fail_seconds", 20))
        self.max_fast_fails = int(r.get("max_fast_fails", 5))

        self.state = STOPPED
        self.detail = ""                 # пояснение к состоянию (почему не настроен, чего не хватает)
        self.proc: asyncio.subprocess.Process | None = None
        self.pid: int | None = None
        self.started_at: float | None = None
        self.last_exit: int | None = None
        self.restarts = 0
        self.fast_fails = 0
        self.lines: deque[dict] = deque(maxlen=2000)
        self.seq = 0
        self._run_seq = 0
        self._want_running = False
        self.soft_stopped = False        # последняя остановка прошла через команду боту, а не kill
        self._supervise_task: asyncio.Task | None = None
        self._readers: list[asyncio.Task] = []
        self._log_path: Path | None = self.home.parent / "logs" / f"{self.name}.log"

    # ------------------------------------------------------------------ для наследников
    def command(self) -> list[str]:
        raise NotImplementedError

    def cwd(self) -> Path:
        return self.home

    def env(self) -> dict[str, str]:
        return {}

    def check_deps(self) -> str | None:
        """Вернуть текст проблемы, если запускать нельзя (нет node, node_modules и т.п.)."""
        return None

    def prepare(self) -> None:
        """Подготовить файлы перед запуском (конфиг, папки)."""

    def classify_exit(self, code: int | None, tail: str) -> tuple[str, str] | None:
        """По коду выхода и последним строкам вывода решить, что это не сбой, а, например, «нужна настройка».
        Вернуть (состояние, пояснение) или None — тогда это обычное падение."""
        return None

    async def health(self) -> bool:
        """Отвечает ли бот (HTTP-пинг). По умолчанию — живой процесс считается здоровым."""
        return self.proc is not None and self.proc.returncode is None

    async def soft_stop(self) -> bool:
        """Попросить бота завершиться самому (через его API), чтобы он дописал базу и закрыл сессии.
        Вернуть True, если команда принята. По умолчанию мягкой остановки нет."""
        return False

    # ------------------------------------------------------------------ управление
    def info(self) -> dict:
        return {
            "name": self.name, "title": self.title, "state": self.state, "state_text": STATE_TEXT.get(self.state, self.state),
            "detail": self.detail, "pid": self.pid, "uptime": round(time.time() - self.started_at) if self.started_at and
            self.state in (STARTING, RUNNING) else 0, "restarts": self.restarts, "last_exit": self.last_exit,
        }

    async def start(self, reason: str = "панель") -> str:
        if self.proc and self.proc.returncode is None:
            return f"{self.title} уже запущен"
        problem = self.check_deps()
        if problem:
            self._set(MISSING_DEPS, problem)
            self.events.add(self.name, "warn", "worker.missing_deps", f"{self.title}: {problem}")
            return f"{self.title}: {problem}"
        self._want_running = True
        self.fast_fails = 0
        if self._supervise_task is None or self._supervise_task.done():
            self._supervise_task = asyncio.create_task(self._supervise(reason), name=f"supervise-{self.name}")
        return f"{self.title}: запуск"

    async def stop(self, reason: str = "панель", timeout: float = 15) -> str:
        self._want_running = False
        if self._supervise_task and not self._supervise_task.done():
            self._supervise_task.cancel()
            try:
                await self._supervise_task
            except asyncio.CancelledError:
                pass
        await self._terminate(timeout)
        if self.state not in (NOT_CONFIGURED, MISSING_DEPS, DISABLED):
            self._set(STOPPED, "")
        self.events.add(self.name, "info", "worker.stop", f"{self.title} остановлен ({reason})")
        return f"{self.title} остановлен"

    async def restart(self, reason: str = "панель") -> str:
        await self.stop(reason)
        return await self.start(reason)

    def logs(self, after: int = 0, limit: int = 500) -> list[dict]:
        out = [ln for ln in self.lines if ln["n"] > after]
        return out[-limit:]

    # ------------------------------------------------------------------ внутреннее
    def _set(self, state: str, detail: str = "") -> None:
        if state != self.state or detail != self.detail:
            log.info("%s: %s %s", self.name, state, detail)
        self.state, self.detail = state, detail

    def _append(self, text: str, stream: str) -> None:
        self.seq += 1
        self.lines.append({"n": self.seq, "ts": time.time(), "stream": stream, "text": text})
        if self._log_path:
            try:
                self._log_path.parent.mkdir(parents=True, exist_ok=True)
                if self._log_path.exists() and self._log_path.stat().st_size > self.log_max_bytes:
                    os.replace(self._log_path, self._log_path.with_suffix(".log.1"))
                with self._log_path.open("a", encoding="utf-8") as f:
                    f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {stream} {text}\n")
            except OSError:
                pass

    async def _read(self, stream: asyncio.StreamReader, name: str) -> None:
        while True:
            line = await stream.readline()
            if not line:
                return
            self._append(line.decode("utf-8", "replace").rstrip("\r\n"), name)

    async def _spawn(self) -> None:
        self.prepare()
        env = {**os.environ, **self.env(), "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x08000000      # CREATE_NO_WINDOW: без лишних консольных окон
        else:
            kwargs["start_new_session"] = True        # своя группа процессов — остановка не задевает центр
        cmd = self.command()
        self._append(f"$ {' '.join(cmd)}", "center")
        self._run_seq = self.seq                       # строки вывода именно этого запуска идут после этой отметки
        self.proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(self.cwd()), env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **kwargs)
        self.pid = self.proc.pid
        winutil.bind_child(self.pid)                   # Windows: бот завершится вместе с центром, даже при аварии
        self.started_at = time.time()
        self._readers = [asyncio.create_task(self._read(self.proc.stdout, "out")),
                         asyncio.create_task(self._read(self.proc.stderr, "err"))]

    async def _terminate(self, timeout: float) -> None:
        """Мягкая остановка: команда боту через его API и ожидание выхода. Если не вышло — SIGTERM
        (на Linux бот сам корректно завершается по сигналу), и только потом принудительно.
        На Windows terminate() = TerminateProcess без шанса что-то сохранить, поэтому там
        мягкая команда — единственный способ остановить бота аккуратно."""
        p = self.proc
        if not p or p.returncode is not None:
            return
        asked = False
        try:
            asked = bool(await asyncio.wait_for(self.soft_stop(), 5))
        except Exception as e:  # noqa: BLE001 — бот занят или уже не отвечает
            self._append(f"мягкая остановка не удалась: {e or type(e).__name__}", "center")
        if asked:
            self._append("мягкая остановка: команда отправлена боту", "center")
            try:
                await asyncio.wait_for(p.wait(), timeout)
            except asyncio.TimeoutError:
                self._append(f"бот не завершился за {int(timeout)} с после команды", "center")
        try:
            if p.returncode is None and sys.platform != "win32":
                p.terminate()
                try:
                    await asyncio.wait_for(p.wait(), timeout if not asked else 5)
                except asyncio.TimeoutError:
                    pass
            if p.returncode is None:
                self._append("процесс не завершился сам — принудительная остановка", "center")
                p.kill()
                await p.wait()
        except ProcessLookupError:
            pass
        for t in self._readers:
            t.cancel()
        self.last_exit = p.returncode
        self.soft_stopped = asked and p.returncode is not None

    async def _wait_healthy(self, timeout: float = 60) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc is None or self.proc.returncode is not None:
                return False
            try:
                if await self.health():
                    return True
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(1)
        return False

    async def _supervise(self, reason: str) -> None:
        delay = self.backoff_start
        first = True
        while self._want_running:
            self._set(STARTING, "")
            try:
                await self._spawn()
            except (OSError, ValueError) as e:
                self._set(MISSING_DEPS, f"не удалось запустить: {e}")
                self.events.add(self.name, "critical", "worker.spawn_failed", f"{self.title}: не удалось запустить: {e}")
                self._want_running = False
                return
            self.events.add(self.name, "info", "worker.start",
                            f"{self.title} запущен ({reason if first else 'автоперезапуск'})", {"pid": self.pid})
            first = False
            healthy_task = asyncio.create_task(self._wait_healthy())
            wait_task = asyncio.create_task(self.proc.wait())
            done, _ = await asyncio.wait({healthy_task, wait_task}, return_when=asyncio.FIRST_COMPLETED)
            if healthy_task in done and healthy_task.result():
                self._set(RUNNING, "")
            elif healthy_task in done and wait_task not in done:
                self._set(RUNNING, "процесс жив, но не отвечает на проверку")
                self.events.add(self.name, "warn", "worker.unhealthy", f"{self.title} не отвечает после запуска")
            code = await wait_task
            healthy_task.cancel()
            for t in self._readers:
                try:
                    await asyncio.wait_for(t, 2)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
            self.last_exit = code
            uptime = time.time() - (self.started_at or time.time())
            if not self._want_running:
                return
            tail = "\n".join(ln["text"] for ln in list(self.lines)[-60:] if ln["n"] > self._run_seq)
            special = self.classify_exit(code, tail)
            if special:
                self._set(*special)
                self.events.add(self.name, "warn", f"worker.{special[0]}", f"{self.title}: {special[1]}")
                self._want_running = False
                return
            self.fast_fails = self.fast_fails + 1 if uptime < self.fast_fail_seconds else 0
            if self.fast_fails == 0:
                delay = self.backoff_start
            if self.fast_fails >= self.max_fast_fails:
                self._set(CRASHED, f"упал {self.fast_fails} раз подряд сразу после запуска (код {code})")
                self.events.add(self.name, "critical", "worker.crashed",
                                f"{self.title} падает сразу после запуска — перезапуск остановлен (код {code})",
                                {"tail": tail[-2000:]})
                self._want_running = False
                return
            self.restarts += 1
            self._set(BACKOFF, f"код выхода {code}, перезапуск через {int(delay)} с")
            self.events.add(self.name, "critical" if self.fast_fails else "warn", "worker.exit",
                            f"{self.title} завершился с кодом {code} после {int(uptime)} с работы — перезапуск через {int(delay)} с",
                            {"tail": tail[-2000:]})
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.backoff_max)
