import asyncio
import json
import sqlite3
import time
import zipfile

from aiohttp.test_utils import TestClient, TestServer

from center.__main__ import main
from center.app import Center
from center.backup import PENDING, apply_pending_restore, make_backup, restore_backup
from center.web import make_app


def _db(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY, name TEXT)")
    con.executemany("INSERT INTO items(name) VALUES (?)", [(r,) for r in rows])
    con.commit()
    return con


def _home(tmp_path):
    home = tmp_path / "home"
    (home / "logs").mkdir(parents=True)
    (home / "logs" / "center.log").write_text("журнал", encoding="utf-8")
    (home / "center.yaml").write_text("center: {port: 8800}\n", encoding="utf-8")
    (home / "secret.key").write_text("KEY", encoding="utf-8")
    (home / "secrets.dat").write_bytes(b"encrypted")
    (home / "lisbot").mkdir()
    (home / "lisbot" / "config.json").write_text("{}", encoding="utf-8")
    (home / "lisbot" / "last-port.txt").write_text("8802", encoding="utf-8")
    return home


def _count(path):
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    finally:
        con.close()


def test_backup_live_wal_db_and_skips(tmp_path):
    home = _home(tmp_path)
    con = _db(home / "lisbot" / "data" / "bot.sqlite", ["a", "b", "c"])     # бот держит базу открытой, данные в -wal
    assert (home / "lisbot" / "data" / "bot.sqlite-wal").exists()
    info = make_backup(home, home / "backups")
    con.close()
    names = {f["path"] for f in info["files"]}
    assert names == {"center.yaml", "secrets.dat", "lisbot/config.json", "lisbot/data/bot.sqlite"}
    assert not info["problems"]
    db = next(f for f in info["files"] if f.get("sqlite"))
    assert db["integrity"] == "ok"
    out = tmp_path / "out"
    with zipfile.ZipFile(home / "backups" / info["name"]) as z:
        assert "secret.key" not in z.namelist() and "manifest.json" in z.namelist()
        z.extractall(out)
    assert _count(out / "lisbot" / "data" / "bot.sqlite") == 3                 # записи из -wal попали в копию


def test_restore_replaces_db_and_keeps_previous(tmp_path):
    home = _home(tmp_path)
    _db(home / "lisbot" / "data" / "bot.sqlite", ["a", "b"]).close()
    info = make_backup(home, home / "backups", now=time.time() - 60)
    con = _db(home / "lisbot" / "data" / "bot.sqlite", ["испорчено"] * 5)      # после копии данные изменились
    (home / "center.yaml").write_text("center: {port: 9999}\n", encoding="utf-8")
    con.close()
    res = restore_backup(home / "backups" / info["name"], home)
    assert _count(home / "lisbot" / "data" / "bot.sqlite") == 2
    assert "8800" in (home / "center.yaml").read_text(encoding="utf-8")
    assert (home / "secret.key").read_text(encoding="utf-8") == "KEY"                          # ключ не трогаем
    assert not (home / "lisbot" / "data" / "bot.sqlite-wal").exists()
    before = home / "backups" / res["before"]
    with zipfile.ZipFile(before) as z:
        assert "9999" in z.read("center.yaml").decode()


def test_restore_rejects_foreign_zip(tmp_path):
    home = _home(tmp_path)
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("manifest.json", json.dumps({"app": "center"}))
        z.writestr("../evil.txt", "x")
    try:
        restore_backup(bad, home)
        assert False, "путь вне папки данных должен отклоняться"
    except ValueError:
        pass
    assert not (tmp_path / "evil.txt").exists()


def test_pending_restore_and_cli(tmp_path, capsys):
    home = _home(tmp_path)
    _db(home / "lisbot" / "data" / "bot.sqlite", ["a"]).close()
    info = make_backup(home, home / "backups")
    _db(home / "lisbot" / "data" / "bot.sqlite", ["b", "c"]).close()
    (home / PENDING).write_text(json.dumps({"path": str(home / "backups" / info["name"])}))
    res = apply_pending_restore(home)
    assert res["name"] == info["name"] and not (home / PENDING).exists()
    assert _count(home / "lisbot" / "data" / "bot.sqlite") == 1
    assert apply_pending_restore(home) is None
    _db(home / "lisbot" / "data" / "bot.sqlite", ["d"]).close()
    main(["--home", str(home), "--port", "18999", "--restore", str(home / "backups" / info["name"])])
    assert "Восстановлено файлов" in capsys.readouterr().out
    assert _count(home / "lisbot" / "data" / "bot.sqlite") == 1


def test_schedule_prune_and_api(tmp_path):
    async def go():
        c = Center(tmp_path)
        c.cfg.save({"backup": {"keep": 2, "time": "00:00"}})
        assert c.backup.due()
        for i in range(3):
            c.backup.clock = lambda i=i: time.time() + i * 3600
            r = await c.backup.run()
            assert r["ok"], r
        assert len([b for b in c.backup.list() if b["label"] == ""]) == 2       # старые удаляются
        c.backup.clock = time.time
        token = c.cfg.ensure_token()
        async with TestClient(TestServer(make_app(c))) as cl:
            await cl.get(f"/login?token={token}", allow_redirects=False)
            st = await (await cl.get("/api/backup")).json()
            assert st["keep"] == 2 and st["last"]["ok"] and len(st["backups"]) == 2
            r = await cl.post("/api/backup/run")
            assert (await r.json())["ok"]
            name = (await (await cl.get("/api/backup")).json())["backups"][0]["name"]
            assert "-manual" in name
            d = await cl.get(f"/api/backup/download/{name}")
            assert d.status == 200 and (await d.read())[:2] == b"PK"
            assert (await cl.get("/api/backup/download/..%2Fcenter.yaml")).status == 404
            r = await cl.post("/api/backup/settings", json={"enabled": True, "time": "25:99", "keep": 3})
            assert r.status == 400
            r = await cl.post("/api/backup/settings", json={"enabled": True, "time": "03:30", "keep": 5,
                                                            "dir": str(tmp_path / "cloud")})
            assert (await r.json())["ok"] and c.backup.dir == tmp_path / "cloud"
            r = await cl.post("/api/backup/restore", json={"name": name})
            assert r.status == 400                                              # без подтверждения
            c.cfg.save({"backup": {"dir": ""}})
            r = await cl.post("/api/backup/restore", json={"name": name, "confirm": "ВОССТАНОВИТЬ"})
            assert (await r.json())["ok"] and (tmp_path / PENDING).exists()
            await asyncio.sleep(1.2)
            assert c.quit_event.is_set()                                        # центр останавливается сам
        kinds = [e["kind"] for e in c.events.recent(50)]
        assert "backup.done" in kinds and "backup.restore_scheduled" in kinds
        c.events.close()
    asyncio.run(go())


def test_backup_failure_is_reported(tmp_path):
    async def go():
        c = Center(tmp_path)
        blocker = tmp_path / "file"
        blocker.write_text("x", encoding="utf-8")
        c.cfg.save({"backup": {"dir": str(blocker / "sub")}})                   # папку создать нельзя
        r = await c.backup.run()
        assert not r["ok"]
        ev = c.events.recent(5)[0]
        assert ev["kind"] == "backup.failed" and ev["level"] == "warn"
        c.events.close()
    asyncio.run(go())
