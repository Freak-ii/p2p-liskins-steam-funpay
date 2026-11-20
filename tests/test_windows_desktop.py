"""Windows-оболочка (packaging/windows): обновления, сборка списка зависимостей, клиент центра."""
import hashlib
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

WIN = Path(__file__).resolve().parents[1] / "packaging" / "windows"
sys.path.insert(0, str(WIN))

from desktop import updater  # noqa: E402


def load_build():
    spec = importlib.util.spec_from_file_location("win_build", WIN / "build.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("cand,cur,newer", [
    ("1.0.1", "1.0.0", True), ("center-v1.2", "1.1.9", True), ("1.0", "1.0.0", False),
    ("1.0.0", "1.0.0", False), ("0.9.9", "1.0.0", False), ("1.10.0", "1.9.0", True), ("junk", "1.0", False),
    ("1.0.0", "0.0.57", True),
])
def test_is_newer(cand, cur, newer):
    assert updater.is_newer(cand, cur) is newer


def release(tag="center-v1.2.0", **kw):
    return {"tag_name": tag, "draft": False, "prerelease": False, "body": "notes", "assets": [
        {"name": "StarsCenter-1.2.0-win64-portable.zip", "url": "https://api.github.com/a/1", "size": 10},
        {"name": "StarsCenter-1.2.0-setup.exe", "url": "https://api.github.com/a/2", "size": 5},
        {"name": "SHA256SUMS.txt", "url": "https://api.github.com/a/3", "size": 1}], **kw}


def test_pick_update():
    upd = updater.pick_update(release(), "1.1.0")
    assert upd and upd.version == "1.2.0" and upd.asset_name == "StarsCenter-1.2.0-setup.exe"
    assert updater.pick_update(release(), "1.2.0") is None
    assert updater.pick_update(release(prerelease=True), "1.0") is None
    assert updater.pick_update(release(tag="mannbot-v9.0"), "1.0") is None


def test_parse_sums():
    h = "a" * 64
    assert updater.parse_sums(f"{h}  StarsCenter-1.2.0-setup.exe\r\n{'b' * 64} *x.zip\nbad line") == {
        "StarsCenter-1.2.0-setup.exe": h, "x.zip": "b" * 64}


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def test_check_and_download(tmp_path, monkeypatch):
    payload = b"MZ fake installer"
    digest = hashlib.sha256(payload).hexdigest()
    rel = release()
    rel["assets"][1]["size"] = len(payload)
    calls = []

    def fake_open(url, token, accept, timeout=30):
        calls.append((url, token))
        if url.endswith("/releases/latest"):
            return FakeResp(json.dumps(rel).encode())
        if url.endswith("/a/3"):
            return FakeResp(f"{digest}  StarsCenter-1.2.0-setup.exe\n".encode())
        if url.endswith("/a/2"):
            return FakeResp(payload)
        raise AssertionError(url)

    monkeypatch.setattr(updater, "_open", fake_open)
    s = updater.Settings(tmp_path / "updates.json")
    assert not s.enabled                                   # без токена обновления выключены
    s.save(token="t0k")
    assert s.enabled and s.due()
    upd = updater.check(s, "1.0.0")
    assert upd and upd.sha256 == digest and not s.due()
    assert all(tok == "t0k" for _, tok in calls)
    out = updater.download(s, upd, tmp_path / "dl")
    assert out.read_bytes() == payload

    upd.sha256 = "0" * 64                                  # подменённый файл не ставим
    with pytest.raises(ValueError):
        updater.download(s, upd, tmp_path / "dl2")
    assert not list((tmp_path / "dl2").glob("*"))
    upd.sha256 = ""                                        # без SHA256SUMS тоже не ставим
    with pytest.raises(ValueError):
        updater.download(s, upd, tmp_path / "dl3")


def test_installer_command_relaunch(tmp_path):
    cmd = updater.installer_command(tmp_path / "setup.exe")
    assert "/RELAUNCH" in cmd and "/CURRENTUSER" in cmd


def test_requirement_lines_skip_test_deps():
    reqs = load_build().requirement_lines()
    assert any(r.lower().startswith("aiohttp") for r in reqs)
    assert not any(r.lower().startswith("pytest") for r in reqs)
    assert len(reqs) == len(set(reqs))


def test_code_ignore_top_level_only(tmp_path):
    build = load_build()
    bot = tmp_path / "bot"
    (bot / "pkg" / "data").mkdir(parents=True)
    (bot / "data").mkdir()
    (bot / "tests").mkdir()
    (bot / "pkg" / "data" / "x.txt").write_text("x")
    ign = build.code_ignore(bot)
    assert {"data", "tests"} <= ign(str(bot), ["data", "tests", "pkg"])
    assert ign(str(bot / "pkg"), ["data"]) == set()        # вложенная папка data — часть кода


def test_center_client_against_running_center(tmp_path):
    """Клиент оболочки понимает настоящий API центра: ping, состояния ботов, quit."""
    import asyncio
    import threading
    import time

    from desktop.app import CenterClient
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    from center.app import Center
    from center.web import start_web

    ready = threading.Event()
    box = {}

    async def main():
        c = Center(tmp_path, demo=False)
        c.cfg.save({"workers": {n: {"enabled": False} for n in c.workers}})
        box["token"] = c.cfg.ensure_token()
        runner = await start_web(c, "127.0.0.1", 0)
        box["port"] = runner.addresses[0][1]
        await c.start()
        ready.set()
        await c.quit_event.wait()
        await c.shutdown()
        await runner.cleanup()

    t = threading.Thread(target=lambda: asyncio.run(main()), daemon=True)
    t.start()
    assert ready.wait(10)
    cl = CenterClient(box["port"], box["token"])
    assert cl.alive()
    assert set(cl.bot_states().values()) == {"disabled"}
    assert cl.bot_panel_url("mannbot") is None             # бот не запущен — ссылки нет
    assert cl.login_url().endswith(box["token"])
    cl.quit()
    t.join(10)
    assert not t.is_alive()
    time.sleep(0.1)
    assert not cl.alive()
