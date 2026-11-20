import json
import sqlite3

from center.bots import LisBotWorker, MannBotWorker, StarsBotWorker, read_env_file, write_env_file
from center.events import EventStore


def test_env_file_roundtrip(tmp_path):
    p = tmp_path / "s.env"
    write_env_file(p, {"A": "1", "B": "x=y"})
    assert read_env_file(p) == {"A": "1", "B": "x=y"}


def test_lisbot_secrets_and_config(tmp_path):
    ev = EventStore(tmp_path / "c.sqlite3")
    w = LisBotWorker(tmp_path / "lisbot", ev, port=18802)
    w.save_secrets({"LIS_API_KEY": "k", "STEAM_PASSWORD": "pw"})
    tok = w.token()
    assert len(tok) >= 16                                        # токен панели создаётся автоматически
    w.save_secrets({"LIS_API_KEY": "lis-key-2", "STEAM_PASSWORD": ""})  # пустой пароль не затирает сохранённый
    s = w.secrets()
    assert not w.secrets_path.exists() and b"lis-key-2" not in (tmp_path / "secrets.dat").read_bytes()
    assert s["LIS_API_KEY"] == "lis-key-2" and s["STEAM_PASSWORD"] == "pw" and s["DASHBOARD_TOKEN"] == tok
    m = w.masked_secrets()
    assert m["STEAM_PASSWORD"] == "" and m["STEAM_PASSWORD_SET"] is True
    w.config_path.write_text(json.dumps({"strategy": {"minRoi": 0.1}}), encoding="utf-8")
    w.prepare()
    cfg = json.loads(w.config_path.read_text(encoding="utf-8"))
    assert cfg["strategy"]["minRoi"] == 0.1                       # настройки, сохранённые панелью бота, не теряются
    assert cfg["dataDir"] == str(w.data_dir) and cfg["dashboard"]["port"] == w.port
    assert cfg["telegram"]["enabled"] is False
    env = w.env()
    assert env["DASHBOARD_TOKEN"] == tok and env["BOT_CONFIG"] == str(w.config_path)
    assert w.panel_url().endswith(f"/#token={tok}")


def test_lisbot_classify_exit(tmp_path):
    w = LisBotWorker(tmp_path / "lisbot", EventStore(tmp_path / "c.sqlite3"), port=18802)
    st, detail = w.classify_exit(1, "Ошибки конфигурации:\n - LIS_API_KEY не задан\n\nЗапустите")
    assert st == "not_configured" and detail == "LIS_API_KEY не задан"
    assert w.classify_exit(1, "Error [ERR_MODULE_NOT_FOUND]: x")[0] == "missing_deps"
    assert w.classify_exit(1, "boom") is None


def test_alerts_read_only(tmp_path):
    ev = EventStore(tmp_path / "c.sqlite3")
    lis = LisBotWorker(tmp_path / "lisbot", ev, port=18802)
    lis.data_dir.mkdir(parents=True)
    con = sqlite3.connect(lis.data_dir / "bot.sqlite")
    con.execute("CREATE TABLE alerts (id INTEGER PRIMARY KEY, ts INTEGER, level TEXT, code TEXT, message TEXT, data TEXT, acked INTEGER)")
    con.execute("INSERT INTO alerts VALUES (1, 1700000000000, 'CRIT', 'X', 'стоп', NULL, 0)")
    con.commit(); con.close()
    a = lis.alerts()
    assert a == [{"source": "lisbot", "id": 1, "ts": 1700000000.0, "level": "crit", "code": "X", "message": "стоп", "acked": False}]

    mann = MannBotWorker(tmp_path / "mannbot", ev, port=18801)
    (mann.home / "data").mkdir(parents=True)
    con = sqlite3.connect(mann.home / "data" / "mannbot.sqlite3")
    con.execute("CREATE TABLE alerts (id INTEGER PRIMARY KEY, ts REAL, level TEXT, code TEXT, text TEXT, order_id TEXT, acknowledged INTEGER)")
    con.execute("INSERT INTO alerts VALUES (5, 1700000001.5, 'WARNING', 'Y', 'проверьте', NULL, 1)")
    con.commit(); con.close()
    assert mann.alerts()[0]["message"] == "проверьте" and mann.alerts()[0]["acked"] is True
    assert MannBotWorker(tmp_path / "empty", ev, port=1).alerts() == []


def test_mannbot_command(tmp_path):
    w = MannBotWorker(tmp_path / "m", EventStore(tmp_path / "c.sqlite3"), port=18801, demo=True)
    cmd = w.command()
    assert cmd[1:3] == ["-m", "mannbot.app.launcher"] and "--demo" in cmd and "--no-browser" in cmd
    assert str(w.src) in w.env()["PYTHONPATH"]


def test_starsbot_command_and_shared_coordination(tmp_path):
    ev = EventStore(tmp_path / "c.sqlite3")
    stars = StarsBotWorker(tmp_path / "starsbot", ev, port=18803)
    mann = MannBotWorker(tmp_path / "mannbot", ev, port=18801)
    cmd = stars.command()
    assert cmd[1:3] == ["-m", "starsbot.app.launcher"] and "--no-browser" in cmd and "18803" in cmd
    assert str(stars.src) in stars.env()["PYTHONPATH"] and stars.src.name == "starsbot"
    # оба бота одного аккаунта FunPay получают один файл координации
    assert stars.env()["FUNPAY_COORD_DB"] == mann.env()["FUNPAY_COORD_DB"]
    assert stars.env()["FUNPAY_COORD_DB"].endswith("funpay_coord.sqlite3")
    demo = StarsBotWorker(tmp_path / "starsbot", ev, port=1, demo=True)
    assert demo.env()["FUNPAY_COORD_DB"].endswith("funpay_coord_demo.sqlite3") and "--demo" in demo.command()
    s = stars._summary({"state": "running", "summary": {"orders": 3, "stars_sold": 300},
                        "wallet": {"balance": 12.34567, "capacity": 1000}, "fragment": {"session": "ok"}})
    assert s["kpi"]["Звёзд выдано"] == 300 and s["kpi"]["TON на кошельке"] == 12.346 and s["kpi"]["Fragment"] == "активна"


def test_center_remote_cmd_file(tmp_path):
    """Центром можно управлять файлом remote_cmd.txt в папке данных: статус и автозапуск без панели."""
    import asyncio
    from center.app import Center
    c = Center(tmp_path)
    (tmp_path / "remote_cmd.txt").write_text("status\nlisbot autostart_on\nnobot start\nmannbot pause\n", encoding="utf-8")
    assert asyncio.run(c.remote_inbox_once()) == 4
    out = (tmp_path / "remote_cmd.out.txt").read_text(encoding="utf-8")
    assert "starsbot:" in out and "автозапуск с центром включён" in out
    assert "неизвестный бот nobot" in out and "не запущен" in out
    assert c.cfg.worker("lisbot")["autostart"] is True and not (tmp_path / "remote_cmd.txt").exists()
    c.events.close()
