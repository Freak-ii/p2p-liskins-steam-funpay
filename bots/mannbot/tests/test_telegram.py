import asyncio

import pytest

from mannbot.app.supervisor import Supervisor
from mannbot.config import Section
from mannbot.notify.notifier import Notifier


class _Resp:
    def __init__(self, data, status=200):
        self.data, self.status = data, status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, content_type=None):
        return self.data


class _Sess:
    """Поддельный Telegram: отдаёт ответы getUpdates по очереди, запоминает отправленные сообщения."""
    closed = False

    def __init__(self, updates):
        self.updates, self.posts = list(updates), []

    def get(self, url, params=None):
        if not self.updates:
            raise asyncio.CancelledError
        return _Resp(self.updates.pop(0))

    def post(self, url, json=None):
        self.posts.append(json)
        return _Resp({"ok": True})

    async def close(self):
        pass


def _notifier(tmp_path, sess, **tg):
    sup = Supervisor(tmp_path)
    cfg = Section({**sup.cfg.notifications, "telegram": Section({"enabled": True, "commands": True, "bot_token": "1:x",
                                                                   "admin_chat_ids": [5], **tg})})
    n = Notifier(cfg, sup.db)
    n._session = sess
    return n, sup


def test_conflict_is_reported_and_paused(tmp_path, monkeypatch):
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    conflict = {"ok": False, "error_code": 409, "description": "Conflict: terminated by other getUpdates request"}
    sess = _Sess([conflict, conflict, {"ok": True, "result": []}])
    n, _ = _notifier(tmp_path, sess)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(n.poll_commands())
    assert sleeps == [30, 30]                              # раньше цикл крутился без паузы
    warn = [t for lvl, t in n.sent if lvl == "WARNING"]
    assert len(warn) == 1 and "разные Telegram-боты" in warn[0]


def test_commands_answer_when_bot_is_stopped(tmp_path):
    sess = _Sess([{"ok": True, "result": [
        {"update_id": 1, "message": {"chat": {"id": 5}, "text": "/status"}},
        {"update_id": 2, "message": {"chat": {"id": 99}, "text": "/run"}},       # чужой чат игнорируется
    ]}])
    n, sup = _notifier(tmp_path, sess)
    n.command_handler = sup.handle_command
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(n.poll_commands(lambda: n.tg))
    assert len(sess.posts) == 1 and sess.posts[0]["chat_id"] == 5
    assert "остановлен" in sess.posts[0]["text"] and "/run" in sess.posts[0]["text"]


def test_run_command_starts_bot(tmp_path):
    sup = Supervisor(tmp_path)
    started = []

    async def fake_start():
        started.append(1)
        return "Бот запускается…"

    sup.start = fake_start
    assert asyncio.run(sup.handle_command("run", [])) == "Бот запускается…" and started
    assert sup.engine.external_commands                    # движок сам Telegram не опрашивает
