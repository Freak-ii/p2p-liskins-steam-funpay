import asyncio
import sys

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
