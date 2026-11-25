"""Орбита: боты переживают перезапуск центра, обновление ботов по одному с откатом."""
import asyncio
import os
import sys
from pathlib import Path

import pytest

from center import rollout as rollout_mod
from center.events import EventStore
from center.rollout import Rollout
from center.workers import RUNNING, STOPPED, Worker, release_name

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="серверный режим — только Linux")


class LoopWorker(Worker):
    name = "fake"
    title = "Fake"
    running_pid: int | None = None

    def command(self):
        return [sys.executable, "-u", "-c",
                "import time,sys\nprint('hello', flush=True)\nwhile True:\n    print('tick', flush=True); time.sleep(0.2)"]

    async def find_running(self):
        pid = type(self).running_pid
        if pid and _alive(pid):
            return pid
        return None

    async def health(self):
        pid = self.pid
        return bool(pid and _alive(pid))


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


async def wait_for(pred, timeout=10):
    for _ in range(int(timeout / 0.05)):
        if pred():
            return True
        await asyncio.sleep(0.05)
    return False


def test_bot_survives_center_handover_and_is_adopted(tmp_path, monkeypatch):
    monkeypatch.setenv("CENTER_SERVER", "1")

    async def go():
        ev = EventStore(tmp_path / "c.sqlite3")
        w = LoopWorker(tmp_path / "fake", ev)
        w.home.mkdir()
        assert w.detached
        await w.start()
        assert await wait_for(lambda: w.state == RUNNING)
        assert await wait_for(lambda: any(ln["text"] == "hello" for ln in w.logs()))
        pid = w.pid
        # центр уходит на новую версию: процесс бота не трогаем
        w.leave_running = True
        await w.stop("обновление центра")
        assert _alive(pid)
        LoopWorker.running_pid = pid
        # «новый центр»: подхватывает тот же процесс и дочитывает его журнал
        w2 = LoopWorker(tmp_path / "fake", ev)
        w2.adopt_poll_seconds = 0.2
        await w2.start("подхват")
        assert await wait_for(lambda: w2.adopted and w2.state == RUNNING)
        assert w2.pid == pid
        n = len(w2.logs())
        assert await wait_for(lambda: len(w2.logs()) > n + 2)       # вывод бота идёт в файл, центр его читает
        await w2.stop("тест")
        assert await wait_for(lambda: not _alive(pid))
        log = (tmp_path / "logs" / "fake.log").read_text(encoding="utf-8")
        assert "hello" in log and "tick" in log
        ev.close()
    try:
        asyncio.run(go())
    finally:
        LoopWorker.running_pid = None


def test_detached_log_rotation_truncates_in_place(tmp_path, monkeypatch):
    monkeypatch.setenv("CENTER_SERVER", "1")

    async def go():
        ev = EventStore(tmp_path / "c.sqlite3")
        w = LoopWorker(tmp_path / "fake", ev)
        w.home.mkdir()
        w.log_max_bytes = 200
        await w.start()
        assert await wait_for(lambda: (tmp_path / "logs" / "fake.log.1").exists())
        n = len(w.logs())
        assert await wait_for(lambda: len(w.logs()) > n + 2)       # после обрезки чтение продолжается
        await w.stop()
        ev.close()
    asyncio.run(go())


class FakeBot:
    """Воркер для проверки порядка обновления: бот на «плохом» релизе падает сразу после запуска."""

    def __init__(self, name, root, bad=()):
        self.name, self.title, self.code_root, self.bad = name, name, Path(root), {Path(b).resolve() for b in bad}
        self.state, self.restarts, self._want_running, self.detached = RUNNING, 0, True, True
        self.pid, self.history = 1, []

    @property
    def alive(self):
        return self.state == RUNNING

    def check_deps(self):
        return None

    async def restart(self, reason=""):
        self.history.append(release_name(self.code_root))
        if Path(self.code_root).resolve() in self.bad:
            self.state = "crashed"
        else:
            self.state = RUNNING


class FakeCenter:
    def __init__(self, home, workers):
        self.home, self.workers = home, workers
        self.events = EventStore(home / "c.sqlite3")
        self.quit_event = asyncio.Event()


def _releases(tmp_path):
    old, new = tmp_path / "releases" / "r1", tmp_path / "releases" / "r2"
    old.mkdir(parents=True), new.mkdir(parents=True)
    return old, new


def test_rollout_updates_running_bots_one_by_one(tmp_path):
    old, new = _releases(tmp_path)
    bots = {n: FakeBot(n, old) for n in ("starsbot", "mannbot", "shopbot")}
    bots["lisbot"] = FakeBot("lisbot", old)
    bots["lisbot"].state = STOPPED                    # остановленный бот не запускается (правило PR #37)
    c = FakeCenter(tmp_path, bots)
    r = Rollout(c, verify_seconds=0.2)
    st = asyncio.run(r.roll(new))
    assert st["state"] == "done"
    assert [b.history for b in (bots["shopbot"], bots["mannbot"], bots["starsbot"])] == [["r2"]] * 3
    assert bots["lisbot"].history == [] and bots["lisbot"].state == STOPPED
    assert bots["lisbot"].code_root == new.resolve()   # ручной запуск поднимет его уже на новом коде
    assert (tmp_path / "server" / "rollout.json").exists()


def test_rollout_rolls_back_failed_bot_and_stops(tmp_path):
    old, new = _releases(tmp_path)
    bots = {"shopbot": FakeBot("shopbot", old), "mannbot": FakeBot("mannbot", old, bad=[new]),
            "starsbot": FakeBot("starsbot", old)}
    c = FakeCenter(tmp_path, bots)
    r = Rollout(c, verify_seconds=0.2, healthy_timeout=1)
    st = asyncio.run(r.roll(new))
    assert st["state"] == "failed"
    assert bots["shopbot"].history == ["r2"]
    assert bots["mannbot"].history == ["r2", "r1"] and bots["mannbot"].state == RUNNING
    assert bots["starsbot"].history == []             # дальше обновление не пошло
    kinds = [e["kind"] for e in c.events.recent()]
    assert "server.rollout_failed" in kinds


def test_handover_only_in_server_mode(tmp_path):
    old, _ = _releases(tmp_path)
    b = FakeBot("starsbot", old)
    b.detached = False
    c = FakeCenter(tmp_path, {"starsbot": b})
    with pytest.raises(RuntimeError):
        Rollout(c).request_handover()


def test_pending_rollout_runs_after_new_center_starts(tmp_path, monkeypatch):
    old, new = _releases(tmp_path)
    monkeypatch.setattr(rollout_mod, "ROOT", new)
    bots = {"starsbot": FakeBot("starsbot", old)}
    c = FakeCenter(tmp_path, bots)
    (tmp_path / "server").mkdir()
    (tmp_path / "server" / "rollout-pending.json").write_text('{"reason": "deploy"}', encoding="utf-8")

    async def go():
        r = Rollout(c, verify_seconds=0.1)
        await r.start()
        await r.task
        return r
    r = asyncio.run(go())
    assert bots["starsbot"].history == ["r2"] and r.status["state"] == "done"
    assert not (tmp_path / "server" / "rollout-pending.json").exists()


def test_migration_bundle_roundtrip(tmp_path, monkeypatch):
    """Переезд с ПК: данные и секреты едут одним файлом под паролем, на сервере секреты — ключом сервера."""
    import yaml
    from center import migrate
    from center.vault import SecretStore
    pc = tmp_path / "pc"
    (pc / "starsbot" / "data").mkdir(parents=True)
    (pc / "logs").mkdir()
    (pc / "logs" / "center.log").write_text("x", encoding="utf-8")
    (pc / "center.yaml").write_text(yaml.safe_dump({"center": {"port": 1}, "workers": {
        "starsbot": {"python": "C:\\runtime\\python.exe", "autostart": False}}}), encoding="utf-8")
    (pc / "starsbot" / "config.yaml").write_text("wallet: {seed: 'a b c'}\n", encoding="utf-8")
    import sqlite3
    con = sqlite3.connect(pc / "starsbot" / "data" / "starsbot.sqlite3")
    con.execute("create table t(x)"), con.execute("insert into t values (42)"), con.commit(), con.close()
    blob = migrate.seal(migrate.build_payload(pc, {"mannbot.funpay.golden_key": "gk"}), "pass-1234567")
    migrate.disable_pc_bots(pc)
    assert yaml.safe_load((pc / "center.yaml").read_text())["workers"]["mannbot"]["enabled"] is False
    assert (pc / "MOVED_TO_SERVER.txt").exists()

    with pytest.raises(ValueError):
        migrate.unseal(blob, "wrong-password")
    monkeypatch.setenv("CENTER_SECRET_KEY", "server-key")
    srv = tmp_path / "srv"
    res = migrate.import_bundle(blob, "pass-1234567", srv)
    assert res["secrets"] == 1
    assert not (srv / "logs").exists()
    cfg = yaml.safe_load((srv / "center.yaml").read_text())
    assert "python" not in cfg["workers"]["starsbot"] and "enabled" not in cfg["workers"]["starsbot"]
    assert sqlite3.connect(srv / "starsbot" / "data" / "starsbot.sqlite3").execute("select x from t").fetchone() == (42,)
    assert SecretStore(srv, backend="fernet").get("mannbot.funpay.golden_key") == "gk"
    with pytest.raises(RuntimeError):
        migrate.import_bundle(blob, "pass-1234567", srv)          # поверх данных — только с --force
