"""Панель не должна зависать: поток событий освобождает соединение, база не раздувается, сторож ловит заминки."""
import asyncio
import json
import time
from pathlib import Path

from starsbot.app.watchdog import LoopWatchdog
from starsbot.crm.db import Database

TEMPLATES = Path(__file__).resolve().parents[1] / "starsbot" / "web" / "templates"


def test_snapshots_pruned_and_latest_kept(tmp_path):
    db = Database(tmp_path / "s.sqlite3")
    old = time.time() - 3 * 86400
    for i in range(50):
        for stars in (50, 100):
            db.exec("INSERT INTO competitor_snapshots(ts,stars,data) VALUES(?,?,?)", (old + i, stars, json.dumps([1] * 500)))
    db.exec("INSERT INTO competitor_snapshots(ts,stars,data) VALUES(?,?,?)", (time.time(), 50, "[]"))
    removed = db.prune()
    rows = db.q("SELECT stars, ts FROM competitor_snapshots ORDER BY id")
    assert removed == 99                                   # старые удалены…
    assert sorted(r["stars"] for r in rows) == [50, 100]   # …а последний снимок номинала 100 остался, хоть и старый


def test_memory_db_prune_noop():
    assert Database(":memory:").prune() == 0


def test_watchdog_reports_blocking_call(caplog):
    async def main():
        wd = LoopWatchdog(asyncio.get_running_loop(), threshold=0.3, interval=0.05).start()
        await asyncio.sleep(0.1)
        time.sleep(0.6)                                    # «тяжёлый» синхронный код прямо в цикле
        await asyncio.sleep(0.3)
        wd.stop()
        return wd
    with caplog.at_level("WARNING", logger="watchdog"):
        wd = asyncio.run(main())
    assert wd.stalls == 1 and wd.worst >= 0.5
    assert "test_watchdog_reports_blocking_call" in caplog.text   # в журнале — место в коде


def test_live_stream_released_when_tab_hidden_or_left():
    js = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert "visibilitychange" in js and "pagehide" in js and "es.close()" in js
