"""Воркеры центра: каждый бот работает отдельным процессом под присмотром центра.

Worker умеет запускать процесс, собирать его вывод (кольцевой буфер + файл), замечать падения
и перезапускать с растущей паузой. Если бот падает сразу после запуска несколько раз подряд,
центр перестаёт его перезапускать и пишет критическое событие: так ошибка настройки не превращается
в бесконечный цикл перезапусков. Если центр упал, а бот остался жить (Linux/Docker), новый центр
«подхватывает» этот процесс, а не запускает второй экземпляр рядом. Конкретные боты (команда запуска, проверка здоровья, сводка)
описаны в center/bots.py.

Серверный режим «Орбита» (CENTER_SERVER=1, см. deploy/ и docs/server.md): вывод бота пишется прямо в его
файл журнала, а не в канал центра. Тогда бот не зависит от жизни процесса центра: центр можно перезапустить
на новой версии (передача «эстафеты»), а новый центр подхватит работающих ботов и дочитает их журнал.
У каждого воркера свой code_root — папка релиза, из которой запущен бот: так боты обновляются по одному
и при сбое возвращаются на прежний релиз (center/rollout.py).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
import sys
import time
from collections import deque
from pathlib import Path

from center import winutil
from center.config import ROOT
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
    adopt_poll_seconds = 5.0          # как часто проверять подхваченный процесс
    adopt_max_misses = 3              # столько проверок подряд без ответа — процесс считается упавшим

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
        self.adopted = False             # процесс запущен не этим центром, а подхвачен от прошлого запуска
        self.extra_env = None            # функция -> dict: переменные от центра (реестр обменов Steam)
        self._supervise_task: asyncio.Task | None = None
        self._readers: list[asyncio.Task] = []
        self._log_path: Path | None = self.home.parent / "logs" / f"{self.name}.log"
        # серверный режим: бот пишет вывод прямо в файл журнала и переживает перезапуск центра
        self.detached = os.environ.get("CENTER_SERVER", "") not in ("", "0") and sys.platform != "win32"
        self.code_root: Path = ROOT          # папка релиза, из которой запускается (запущен) бот
        self.leave_running = False           # центр уходит на обновление: процесс бота не останавливать
        self._tail_task: asyncio.Task | None = None

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

    async def find_running(self) -> int | None:
        """Найти уже работающий экземпляр этого бота, оставшийся от прошлого запуска центра.
        Вернуть его pid (0 — pid неизвестен) или None, если подхватывать нечего."""
        return None

    @property
    def alive(self) -> bool:
        """Есть ли у воркера живой процесс: свой или подхваченный."""
        return self.adopted or (self.proc is not None and self.proc.returncode is None)

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
            "release": release_name(self.code_root),
        }

    async def start(self, reason: str = "панель") -> str:
        if self.alive:
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
        if self.leave_running and self.detached and self.alive:
            # передача эстафеты: центр уходит на новую версию, бот продолжает торговать и будет подхвачен
            await self._stop_tail()
            self.events.add(self.name, "info", "worker.handover",
                            f"{self.title} продолжает работать: центр перезапускается на новой версии", {"pid": self.pid})
            return f"{self.title} оставлен работать"
        await self._terminate(timeout)
        await self._stop_tail()
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

    def _append(self, text: str, stream: str, *, to_file: bool = True) -> None:
        self.seq += 1
        self.lines.append({"n": self.seq, "ts": time.time(), "stream": stream, "text": text})
        if self._log_path and to_file:
            try:
                self._log_path.parent.mkdir(parents=True, exist_ok=True)
                self._rotate_log()
                with self._log_path.open("a", encoding="utf-8") as f:
                    f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {stream} {text}\n")
            except OSError:
                pass

    def _rotate_log(self) -> None:
        p = self._log_path
        if not p or not p.exists() or p.stat().st_size <= self.log_max_bytes:
            return
        if self.detached:
            # бот держит файл открытым (O_APPEND): копия и обрезка, а не переименование — иначе он писал бы в .log.1
            import shutil
            shutil.copyfile(p, p.with_suffix(".log.1"))
            with p.open("r+b") as f:
                f.truncate(0)
        else:
            os.replace(p, p.with_suffix(".log.1"))

    CENTER_LINE = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d center ")

    async def _tail(self, offset: int | None = None) -> None:
        """Серверный режим: дочитывать файл журнала, куда бот пишет сам, в кольцевой буфер панели.
        Свои строки центр уже положил в буфер — их пропускаем."""
        p = self._log_path
        if p is None:
            return
        pos = offset if offset is not None else (p.stat().st_size if p.exists() else 0)
        buf = b""
        while True:
            try:
                size = p.stat().st_size if p.exists() else 0
                if size < pos:                    # файл обрезан при ротации
                    pos, buf = 0, b""
                if size > pos:
                    with p.open("rb") as f:
                        f.seek(pos)
                        chunk = f.read(min(size - pos, 1_000_000))
                    pos += len(chunk)
                    buf += chunk
                    *lines, buf = buf.split(b"\n")
                    for raw in lines:
                        text = raw.decode("utf-8", "replace").rstrip("\r")
                        if text and not self.CENTER_LINE.match(text):
                            self._append(text, "out", to_file=False)
                    self._rotate_log()
            except OSError:
                pass
            await asyncio.sleep(0.5)

    def _start_tail(self, offset: int | None = None) -> None:
        if self._tail_task is None or self._tail_task.done():
            self._tail_task = asyncio.create_task(self._tail(offset), name=f"tail-{self.name}")

    async def _stop_tail(self) -> None:
        t, self._tail_task = self._tail_task, None
        if t:
            await asyncio.sleep(0.6)              # дочитать последние строки
            t.cancel()

    async def _read(self, stream: asyncio.StreamReader, name: str) -> None:
        while True:
            line = await stream.readline()
            if not line:
                return
            self._append(line.decode("utf-8", "replace").rstrip("\r\n"), name)

    async def _spawn(self) -> None:
        self.prepare()
        env = {**os.environ, **(self.extra_env() if self.extra_env else {}), **self.env(),
               "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x08000000      # CREATE_NO_WINDOW: без лишних консольных окон
        else:
            kwargs["start_new_session"] = True        # своя группа процессов — остановка не задевает центр
        cmd = self.command()
        self._append(f"$ {' '.join(cmd)}", "center")
        self._run_seq = self.seq                       # строки вывода именно этого запуска идут после этой отметки
        if self.detached and self._log_path:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            offset = self._log_path.stat().st_size if self._log_path.exists() else 0
            out = open(self._log_path, "ab")           # noqa: SIM115 — дескриптор нужен только до запуска процесса
            try:
                self.proc = await asyncio.create_subprocess_exec(
                    *cmd, cwd=str(self.cwd()), env=env, stdin=asyncio.subprocess.DEVNULL,
                    stdout=out, stderr=asyncio.subprocess.STDOUT, **kwargs)
            finally:
                out.close()
            self._readers = []
            self._start_tail(offset)
        else:
            self.proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=str(self.cwd()), env=env, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **kwargs)
            self._readers = [asyncio.create_task(self._read(self.proc.stdout, "out")),
                             asyncio.create_task(self._read(self.proc.stderr, "err"))]
        self.pid = self.proc.pid
        winutil.bind_child(self.pid)                   # Windows: бот завершится вместе с центром, даже при аварии
        self.started_at = time.time()

    async def _terminate(self, timeout: float) -> None:
        """Мягкая остановка: команда боту через его API и ожидание выхода. Если не вышло — SIGTERM
        (на Linux бот сам корректно завершается по сигналу), и только потом принудительно.
        На Windows terminate() = TerminateProcess без шанса что-то сохранить, поэтому там
        мягкая команда — единственный способ остановить бота аккуратно."""
        if self.adopted:
            await self._terminate_adopted(timeout)
            return
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

    async def _responds(self) -> bool:
        try:
            return bool(await asyncio.wait_for(self.health(), 10))
        except Exception:  # noqa: BLE001
            return False

    async def _terminate_adopted(self, timeout: float) -> None:
        """Остановить подхваченный процесс: своего объекта процесса у центра нет, есть только API бота и pid."""
        pid = _signal_pid(self.pid)
        asked = False
        try:
            asked = bool(await asyncio.wait_for(self.soft_stop(), 5))
        except Exception as e:  # noqa: BLE001
            self._append(f"мягкая остановка не удалась: {e or type(e).__name__}", "center")
        if asked:
            self._append("мягкая остановка: команда отправлена боту", "center")
        deadline = time.time() + (timeout if asked else 0)
        while time.time() < deadline and (_pid_alive(pid) if pid else await self._responds()):
            await asyncio.sleep(0.5)
        if pid and _pid_alive(pid):
            self._append("подхваченный процесс не завершился сам — остановка по pid", "center")
            _kill(pid, signal.SIGTERM)
            for _ in range(10):
                if not _pid_alive(pid):
                    break
                await asyncio.sleep(0.5)
            if _pid_alive(pid):
                _kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        self.soft_stopped = asked and not (pid and _pid_alive(pid))
        self.adopted = False

    async def _watch_adopted(self, pid: int) -> None:
        """Присматривать за подхваченным процессом, пока он отвечает. Вернуться, когда он пропал."""
        self.adopted, self.proc = True, None
        self.pid = pid or None
        self.started_at = time.time()
        self._set(RUNNING, "подхвачен процесс, оставшийся от прошлого запуска центра")
        if self.detached:
            self._start_tail()
            root = release_of(pid)
            if root is not None:
                self.code_root = root
        self.events.add(self.name, "info", "worker.adopted",
                        f"{self.title} уже работал (центр перезапускался) — процесс подхвачен, второй экземпляр не запускается",
                        {"pid": pid or None})
        pid = _signal_pid(pid)
        misses = 0
        while self._want_running:
            await asyncio.sleep(self.adopt_poll_seconds)
            if pid and not _pid_alive(pid):
                misses = self.adopt_max_misses
            else:
                misses = 0 if await self._responds() else misses + 1
            if misses < self.adopt_max_misses:
                continue
            if pid and _pid_alive(pid):          # завис: убираем, иначе новый экземпляр уйдёт на запасной порт рядом с ним
                _kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
            self.adopted, self.pid = False, None
            self.restarts += 1
            self.events.add(self.name, "warn", "worker.exit",
                            f"{self.title}: подхваченный процесс перестал отвечать — центр запускает бота заново")
            return

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
            try:
                running = await self.find_running()
            except Exception as e:  # noqa: BLE001 — поиск не должен мешать обычному запуску
                log.debug("%s: find_running: %s", self.name, e)
                running = None
            if running is not None:
                await self._watch_adopted(running)
                first = False
                continue
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


def release_name(root: Path | None) -> str:
    """Имя релиза для панели: папка releases/<имя> на сервере или «dev» для запуска из исходников."""
    if root is None:
        return ""
    p = Path(root)
    return p.name if p.parent.name == "releases" else "dev"


def release_of(pid: int | None) -> Path | None:
    """Из какой папки релиза запущен процесс (Linux: рабочая папка бота лежит внутри релиза)."""
    if not pid or sys.platform == "win32":
        return None
    try:
        cwd = Path(os.readlink(f"/proc/{int(pid)}/cwd"))
    except OSError:
        return None
    for p in (cwd, *cwd.parents):
        if p.parent.name == "releases":
            return p
    return None


def pid_by_port(port: int) -> int:
    """Linux: pid процесса, который слушает этот TCP-порт (для ботов, не сообщающих pid в /api/ping)."""
    if sys.platform != "linux":
        return 0
    inodes = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(table, encoding="ascii") as f:
                next(f, None)
                for row in f:
                    parts = row.split()
                    if len(parts) > 9 and parts[3] == "0A" and int(parts[1].rsplit(":", 1)[1], 16) == int(port):
                        inodes.add(parts[9])
        except OSError:
            continue
    if not inodes:
        return 0
    targets = {f"socket:[{i}]" for i in inodes}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            for fd in os.listdir(f"/proc/{d}/fd"):
                if os.readlink(f"/proc/{d}/fd/{fd}") in targets:
                    return int(d)
        except OSError:
            continue
    return 0


def _signal_pid(pid: int | None) -> int:
    """pid, которым можно проверять и останавливать процесс сигналами; 0 — нельзя.
    На Windows os.kill(pid, 0) завершает процесс, поэтому там только API бота (а бота центра
    и так завершает Job Object вместе с центром)."""
    return 0 if sys.platform == "win32" else int(pid or 0)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _kill(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass
