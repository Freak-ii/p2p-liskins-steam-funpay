"""Сторож цикла событий: если цикл занят дольше порога (панель в это время не отвечает), пишет в журнал,
сколько длилась заминка и какой код её вызвал. Так «панель зависла» на живом ПК превращается в строку
журнала с точным местом в коде, без отладчика."""
from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
import traceback

log = logging.getLogger("watchdog")


class LoopWatchdog:
    def __init__(self, loop: asyncio.AbstractEventLoop, threshold: float = 1.5, interval: float = 0.25):
        self.loop = loop
        self.threshold = threshold
        self.interval = interval
        self.last_beat = time.monotonic()
        self.loop_thread = threading.get_ident()
        self.stalls = 0
        self.worst = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    async def _beat(self) -> None:
        while not self._stop.is_set():
            self.last_beat = time.monotonic()
            await asyncio.sleep(self.interval)

    def _watch(self) -> None:
        reported = False
        stall_start = 0.0
        stack = ""
        while not self._stop.wait(self.interval):
            lag = time.monotonic() - self.last_beat
            if lag > self.threshold:
                if not reported:
                    reported, stall_start = True, self.last_beat
                    frame = sys._current_frames().get(self.loop_thread)
                    stack = "".join(traceback.format_stack(frame, limit=12)) if frame else ""
            elif reported:
                took = time.monotonic() - stall_start
                self.stalls += 1
                self.worst = max(self.worst, took)
                log.warning("цикл событий был занят %.1f с — панель в это время не отвечала. Где:\n%s", took, stack)
                reported = False

    def start(self) -> "LoopWatchdog":
        self.loop_thread = threading.get_ident()
        self.loop.create_task(self._beat())
        self._thread = threading.Thread(target=self._watch, name="loop-watchdog", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict:
        return {"stalls": self.stalls, "worst_s": round(self.worst, 1)}
