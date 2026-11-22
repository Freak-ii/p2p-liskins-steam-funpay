import asyncio
import sys

import pytest

from center.events import EventStore
from center.workers import CRASHED, NOT_CONFIGURED, RUNNING, STOPPED, Worker


class FakeWorker(Worker):
    name = "fake"
    title = "Fake"

    def __init__(self, home, events, code, **kw):
        super().__init__(home, events, **kw)
        self.code = code

    def command(self):
        return [sys.executable, "-c", self.code]

    def classify_exit(self, code, tail):
        if "NEED_SETUP" in tail:
            return NOT_CONFIGURED, "нужна настройка"
        return None


def make(tmp_path, code, **restart):
    ev = EventStore(tmp_path / "c.sqlite3")
    w = FakeWorker(tmp_path / "fake", ev, code,
                   restart={"backoff_start": 0.05, "backoff_max": 0.1, "fast_fail_seconds": 5, "max_fast_fails": 3, **restart})
    w.home.mkdir()
    return w, ev


async def wait_for(pred, timeout=10):
    for _ in range(int(timeout / 0.05)):
        if pred():
            return True
        await asyncio.sleep(0.05)
    return False


def test_runs_collects_output_and_stops(tmp_path):
    async def go():
        w, ev = make(tmp_path, "import time\nprint('hello', flush=True)\ntime.sleep(30)")
        await w.start()
        assert await wait_for(lambda: w.state == RUNNING)
        assert await wait_for(lambda: any(ln["text"] == "hello" for ln in w.logs()))
        await w.stop()
        assert w.state == STOPPED and w.proc.returncode is not None
        kinds = [e["kind"] for e in ev.recent()]
        assert "worker.start" in kinds and "worker.stop" in kinds
        assert " out hello" in (tmp_path / "logs" / "fake.log").read_text(encoding="utf-8")
    asyncio.run(go())


def test_crash_loop_stops_restarting(tmp_path):
    async def go():
        w, ev = make(tmp_path, "import sys; sys.exit(3)")
        await w.start()
        assert await wait_for(lambda: w.state == CRASHED)
        assert w.last_exit == 3 and w.restarts == 2
        assert any(e["kind"] == "worker.crashed" and e["level"] == "critical" for e in ev.recent())
    asyncio.run(go())


def test_not_configured_is_not_a_crash(tmp_path):
    async def go():
        w, ev = make(tmp_path, "print('NEED_SETUP', flush=True); raise SystemExit(1)")
        await w.start()
        assert await wait_for(lambda: w.state == NOT_CONFIGURED)
        await asyncio.sleep(0.3)
        assert w.restarts == 0 and w.state == NOT_CONFIGURED
    asyncio.run(go())


def test_missing_executable_reported(tmp_path):
    async def go():
        w, ev = make(tmp_path, "")
        w.command = lambda: ["/nonexistent/binary-xyz"]
        await w.start()
        assert await wait_for(lambda: w.state == "missing_deps")
        assert "не удалось запустить" in w.detail
    asyncio.run(go())


class AdoptingWorker(FakeWorker):
    """Бот «остался жить» после падения центра: find_running сообщает его pid."""

    adopt_poll_seconds = 0.05
    adopt_max_misses = 2

    def __init__(self, *a, orphan=None, **kw):
        super().__init__(*a, **kw)
        self.orphan = orphan

    async def find_running(self):
        if self.orphan is not None and self.orphan.poll() is None:
            return self.orphan.pid
        return None

    async def health(self):
        if self.adopted:
            return self.orphan.poll() is None
        return await super().health()


def _orphan():
    import subprocess
    import threading
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    threading.Thread(target=p.wait, daemon=True).start()     # как init у настоящего осиротевшего процесса
    return p


@pytest.mark.skipif(sys.platform == "win32", reason="на Windows бот завершается вместе с центром (Job Object)")
def test_adopts_running_bot_instead_of_second_copy(tmp_path):
    async def go():
        orphan = _orphan()
        w, ev = make(tmp_path, "import time\ntime.sleep(30)")
        w.__class__ = AdoptingWorker
        w.orphan = orphan
        await w.start("автозапуск центра")
        assert await wait_for(lambda: w.state == RUNNING)
        assert w.adopted and w.proc is None and w.pid == orphan.pid and w.alive
        assert w.info()["pid"] == orphan.pid
        assert "worker.adopted" in [e["kind"] for e in ev.recent()]
        await w.stop()
        assert await wait_for(lambda: orphan.poll() is not None)  # остановка из центра доходит и до подхваченного
        assert w.state == STOPPED and not w.alive
    asyncio.run(go())


@pytest.mark.skipif(sys.platform == "win32", reason="на Windows бот завершается вместе с центром (Job Object)")
def test_adopted_bot_dies_center_starts_own(tmp_path):
    async def go():
        orphan = _orphan()
        w, ev = make(tmp_path, "import time\nprint('own', flush=True)\ntime.sleep(30)")
        w.__class__ = AdoptingWorker
        w.orphan = orphan
        await w.start()
        assert await wait_for(lambda: w.adopted)
        orphan.kill()
        assert await wait_for(lambda: w.proc is not None and any(ln["text"] == "own" for ln in w.logs()))
        assert not w.adopted and w.restarts == 1
        await w.stop()
    asyncio.run(go())
