"""Платформа: зашифрованные секреты, перенос открытых ключей, мягкая остановка ботов."""
import asyncio
import sys

import pytest
import yaml

from center.bots import LisBotWorker, MannBotWorker, mann_secret_env, write_env_file
from center.events import EventStore
from center.vault import SecretStore, SecretStoreError
from center.workers import Worker
from center import winutil


def test_vault_roundtrip_and_wrong_key(tmp_path, monkeypatch):
    monkeypatch.delenv("CENTER_SECRET_KEY", raising=False)
    v = SecretStore(tmp_path)
    v.update({"lisbot.A": "секрет-1", "mannbot.funpay.golden_key": "gk"})
    raw = (tmp_path / "secrets.dat").read_bytes()
    assert "секрет".encode() not in raw and b"gk" not in raw
    v2 = SecretStore(tmp_path)
    assert v2.get("lisbot.A") == "секрет-1" and v2.prefix("lisbot") == {"A": "секрет-1"}
    v2.set("lisbot.A", None)
    assert SecretStore(tmp_path).prefix("lisbot") == {}
    if not winutil.IS_WINDOWS:
        monkeypatch.setenv("CENTER_SECRET_KEY", "другой ключ")
        with pytest.raises(SecretStoreError):
            SecretStore(tmp_path).get("x")


@pytest.mark.skipif(not winutil.IS_WINDOWS, reason="DPAPI есть только в Windows")
def test_dpapi_roundtrip():
    enc = winutil.dpapi_protect(b"hello")
    assert enc != b"hello" and winutil.dpapi_unprotect(enc) == b"hello"


def test_lisbot_plain_secrets_migrated(tmp_path):
    w = LisBotWorker(tmp_path / "lisbot", EventStore(tmp_path / "c.sqlite3"), port=18802)
    write_env_file(w.secrets_path, {"LIS_API_KEY": "old", "DASHBOARD_TOKEN": "t" * 32})
    assert w.secrets()["LIS_API_KEY"] == "old" and w.token() == "t" * 32
    assert not w.secrets_path.exists()


def test_mannbot_secrets_moved_to_env(tmp_path):
    w = MannBotWorker(tmp_path / "mannbot", EventStore(tmp_path / "c.sqlite3"), port=18801)
    w._cfg_path.write_text(yaml.safe_dump({"funpay": {"golden_key": "GK", "poll_interval": 6},
                                           "notifications": {"telegram": {"bot_token": "TG"}},
                                           "dashboard": {"token": "panel-token-123"}}), encoding="utf-8")
    assert w.migrate_secrets() == 2
    raw = yaml.safe_load(w._cfg_path.read_text(encoding="utf-8"))
    assert raw["funpay"]["golden_key"] == "${CENTER_MB_FUNPAY_GOLDEN_KEY}"
    assert raw["notifications"]["telegram"]["bot_token"] == "${" + mann_secret_env("notifications.telegram", "bot_token") + "}"
    assert raw["funpay"]["poll_interval"] == 6 and w.token() == "panel-token-123"   # остальное не тронуто
    env = w.env()
    assert env["CENTER_MB_FUNPAY_GOLDEN_KEY"] == "GK" and env["CENTER_MB_NOTIFICATIONS_TELEGRAM_BOT_TOKEN"] == "TG"
    assert w.migrate_secrets() == 0                                    # повторно ничего не переносится


class SoftWorker(Worker):
    """Процесс ждёт файл-флаг и выходит сам, напечатав bye — как бот после команды /api/shutdown."""
    name = "soft"
    title = "Soft"

    def command(self):
        flag = str(self.home / "quit.flag")
        code = ("import os,time,signal\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN) if hasattr(signal,'SIGTERM') else None\n"
                f"print('up', flush=True)\nwhile not os.path.exists({flag!r}): time.sleep(0.05)\n"
                "print('bye', flush=True)\n")
        return [sys.executable, "-c", code]

    async def soft_stop(self):
        (self.home / "quit.flag").write_text("1")
        return True


def test_soft_stop_lets_bot_exit_cleanly(tmp_path):
    async def go():
        w = SoftWorker(tmp_path / "soft", EventStore(tmp_path / "c.sqlite3"))
        w.home.mkdir()
        await w.start("test")
        for _ in range(200):
            if any(ln["text"] == "up" for ln in w.lines):
                break
            await asyncio.sleep(0.05)
        await w.stop("test", timeout=10)
        texts = [ln["text"] for ln in w.lines]
        assert "bye" in texts and w.last_exit == 0 and w.soft_stopped
        assert not any("принудительная" in t for t in texts)
    asyncio.run(go())


def test_center_keys_live_in_vault(tmp_path):
    from center.app import Center
    c = Center(tmp_path)
    c.cfg.save({"monitor": {"telegram": {"token": "123:plain"}}, "coordinator": {"ai": {"api_key": "sk-old-key-123"}}})
    assert c.monitor._token() == "123:plain" and c.coordinator.api_key() == "sk-old-key-123"   # старые значения перенесены
    c.coordinator.save_settings({"ai": {"api_key": "sk-new-key-456"}})
    c.monitor.save_settings({"telegram_token": "456:new", "telegram_chat_id": "1"})
    text = (tmp_path / "center.yaml").read_text(encoding="utf-8")
    assert "plain" not in text and "sk-" not in text and "456:new" not in text
    assert c.coordinator.api_key() == "sk-new-key-456" and c.monitor.telegram_target()[0] == "456:new"
    c.events.close()
