"""Десктоп-оболочка Центра для Windows: центр работает в фоновом потоке, в главном — значок в трее.

* Первый запуск: сразу открывается мастер настройки StarsBot (ключи FunPay, кошелёк TON, Fragment).
  Дальше — обычная панель центра.
* Трей: открыть панель / StarsBot, папка данных и журналы, автозапуск, обновления, выход.
* Повторный запуск при уже работающем центре просто открывает панель.
* Без Windows, с --console или --no-tray значка нет: центр работает до Ctrl+C или кнопки «Выключить».
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from desktop import autostart, updater

log = logging.getLogger("desktop")
FIRST_RUN_MARK = ".desktop-first-run-done"
SETUP_BOT = "starsbot"                                    # чей мастер настройки открыть при первом запуске


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


class CenterClient:
    """Минимальный клиент API центра (тот же токен, что и для входа в панель)."""

    def __init__(self, port: int, token: str):
        self.base = f"http://127.0.0.1:{port}"
        self.token = token

    def _req(self, path: str, method: str = "GET", timeout: float = 3):
        req = urllib.request.Request(self.base + path, method=method,
                                     headers={"Authorization": f"Bearer {self.token}"})
        return urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout)

    def alive(self) -> bool:
        try:
            with urllib.request.urlopen(self.base + "/api/ping", timeout=1.5) as r:
                return b'"center"' in r.read()
        except Exception:  # noqa: BLE001
            return False

    def bot_states(self) -> dict[str, str]:
        with self._req("/api/overview") as r:
            return {b.get("name"): b.get("state") for b in json.load(r).get("bots", [])}

    def bot_panel_url(self, name: str) -> str | None:
        """Ссылка на панель бота уже со входом (центр отдаёт её редиректом /open/<бот>)."""
        try:
            self._req(f"/open/{name}").close()
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location", "")
            if e.code in (301, 302, 303, 307) and loc.startswith("http"):
                return loc
        return None

    def login_url(self) -> str:
        return f"{self.base}/login?token={self.token}"

    def quit(self) -> None:
        try:
            self._req("/api/quit", "POST", timeout=5).close()
        except Exception:  # noqa: BLE001 — центр мог уже закрыться
            pass


class Desktop:
    def __init__(self, argv: list[str], *, app_dir: Path, console: bool):
        ap = argparse.ArgumentParser(add_help=False)
        ap.add_argument("--home")
        ap.add_argument("--port", type=int)
        ap.add_argument("--demo", action="store_true")
        ap.add_argument("--no-browser", action="store_true")
        ap.add_argument("--no-tray", action="store_true")
        self.args, _ = ap.parse_known_args(argv)
        self.center_argv = [a for a in argv if a not in ("--no-tray",)]
        if "--no-browser" not in self.center_argv:
            self.center_argv.append("--no-browser")     # браузер открывает оболочка, а не центр
        self.app_dir = app_dir
        self.base_dir = app_dir.parent
        self.console = console

        os.environ["CENTER_DESKTOP_UPDATES"] = "1"           # центр не показывает свою полоску обновлений: их ставит оболочка
        from center.config import CenterConfig
        root_home = Path(self.args.home).expanduser().resolve() if self.args.home else Path(os.environ["CENTER_HOME"])
        self.root_home = root_home
        self.home = root_home / "demo" if self.args.demo else root_home
        self.home.mkdir(parents=True, exist_ok=True)
        cfg = CenterConfig(self.home, demo=bool(self.args.demo))
        if not (app_dir / "bots" / "lisbot").is_dir() and "lisbot" not in (cfg.user.get("workers") or {}):
            cfg.save({"workers": {"lisbot": {"enabled": False}}})   # сборка без LisBot
        self.port = int(self.args.port or cfg["center"]["port"])
        self.client = CenterClient(self.port, cfg.ensure_token())
        self.bots = sorted(p.name for p in (app_dir / "bots").iterdir() if p.is_dir()) if (app_dir / "bots").is_dir() else []

        self.version = "0"
        try:
            self.version = json.loads((self.base_dir / "build-info.json").read_text(encoding="utf-8"))["version"]
        except (OSError, ValueError, KeyError):
            pass
        self.updates = updater.Settings(root_home.parent / "updates.json")
        self.pending_update: updater.Update | None = None
        self.tray = None
        self.thread: threading.Thread | None = None
        self.exit_code = 0

    # ---------------------------------------------------------------- центр
    def _run_center(self) -> None:
        from center.__main__ import main as center_main
        try:
            center_main(self.center_argv)
        except SystemExit as e:
            self.exit_code = e.code if isinstance(e.code, int) else (1 if e.code else 0)
            if e.code not in (None, 0):
                self.error(str(e.code))
        except Exception:  # noqa: BLE001
            self.exit_code = 1
            log.exception("центр упал")
            self.error("Центр завершился с ошибкой. Подробности в журнале logs\\center.log")

    def start_center(self) -> None:
        self.thread = threading.Thread(target=self._run_center, name="center", daemon=True)
        self.thread.start()

    def center_running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def wait_alive(self, timeout: float = 60) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline and self.center_running():
            if self.client.alive():
                return True
            time.sleep(0.5)
        return False

    # ---------------------------------------------------------------- браузер
    def open_panel(self) -> None:
        webbrowser.open(self.client.login_url())

    def open_bot(self, name: str) -> None:
        url = self.client.bot_panel_url(name)
        if url:
            webbrowser.open(url)
        else:
            self.info("Бот ещё не запущен", "Откройте панель центра и запустите бота.")
            self.open_panel()

    def first_run_pending(self) -> bool:
        return not self.args.demo and SETUP_BOT in self.bots and not (self.home / FIRST_RUN_MARK).exists()

    def open_initial(self) -> None:
        """Первый запуск — мастер настройки StarsBot, дальше — панель центра."""
        if not self.wait_alive():
            return
        if self.args.no_browser:
            return
        if self.first_run_pending():
            deadline = time.time() + 60
            while time.time() < deadline and self.center_running():
                try:
                    if self.client.bot_states().get(SETUP_BOT) == "running":
                        url = self.client.bot_panel_url(SETUP_BOT)
                        if url:
                            webbrowser.open(url)          # без настроек StarsBot сам ведёт на /setup
                            (self.home / FIRST_RUN_MARK).write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
                            self.info("Добро пожаловать!", "Открыт мастер настройки StarsBot. Центр работает в трее.")
                            return
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(1)
        self.open_panel()

    # ---------------------------------------------------------------- уведомления
    def info(self, title: str, text: str, on_click=None) -> None:
        if self.tray:
            self.tray.notify(title, text, on_click=on_click)
        else:
            print(f"{title}: {text}", flush=True)

    def error(self, text: str) -> None:
        if self.tray or (sys.platform == "win32" and not self.console):
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(None, text, "Центр", 0x10)
                return
            except Exception:  # noqa: BLE001
                pass
        print(text, file=sys.stderr, flush=True)

    # ---------------------------------------------------------------- обновления
    def check_updates(self, manual: bool = False) -> None:
        if not self.updates.enabled:
            if manual:
                self.info("Обновления не настроены",
                          f"Добавьте токен GitHub (только чтение) в {self.updates.path}. Подробнее — в «Прочти меня».")
                if not self.updates.path.exists():
                    self.updates.save(enabled=True)
            return
        try:
            upd = updater.check(self.updates, self.version)
        except Exception as e:  # noqa: BLE001
            log.warning("проверка обновлений не удалась: %s", e)
            if manual:
                self.info("Не удалось проверить обновления", str(e)[:200])
            return
        if upd:
            self.pending_update = upd
            self.info(f"Доступна версия {upd.version}", "Нажмите, чтобы установить (центр перезапустится).",
                      on_click=lambda: self.install_update())
        elif manual:
            self.info("Обновлений нет", f"Установлена последняя версия {self.version}.")

    def install_update(self) -> None:
        upd = self.pending_update
        if not upd:
            return

        def work():
            try:
                self.info("Обновление", f"Скачиваю версию {upd.version}…")
                setup = updater.download(self.updates, upd, Path(tempfile.gettempdir()) / "StarsCenter-update")
            except Exception as e:  # noqa: BLE001
                self.info("Обновление не скачалось", str(e)[:200])
                return
            self.client.quit()
            if self.thread:
                self.thread.join(60)
            subprocess.Popen(updater.installer_command(setup), close_fds=True,
                             creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
            if self.tray:
                self.tray.stop()
        threading.Thread(target=work, name="update", daemon=True).start()

    def _update_loop(self) -> None:
        if self.args.demo:
            return
        time.sleep(30)
        while self.center_running():
            if self.updates.due():
                self.check_updates()
            time.sleep(3600)

    # ---------------------------------------------------------------- трей
    CMD_PANEL, CMD_STARS, CMD_MANN, CMD_LIS, CMD_DATA, CMD_LOGS = 1, 2, 3, 4, 5, 6
    CMD_AUTOSTART, CMD_UPDATE, CMD_INSTALL, CMD_QUIT = 7, 8, 9, 10

    def autostart_cmd(self) -> str:
        pyw = self.base_dir / "runtime" / "pythonw.exe"
        return autostart.command(str(pyw), str(self.app_dir / "launch.pyw"))

    def menu(self) -> list:
        items = [(self.CMD_PANEL, "Открыть панель центра", {"default"})]
        names = {"starsbot": (self.CMD_STARS, "StarsBot · Telegram Stars"), "mannbot": (self.CMD_MANN, "MannBot · FunPay"),
                 "lisbot": (self.CMD_LIS, "LisBot · LIS-SKINS")}
        for b in sorted(self.bots, key=lambda n: list(names).index(n) if n in names else 99):
            if b in names:
                items.append((names[b][0], f"Открыть {names[b][1]}", set()))
        items += [None, (self.CMD_DATA, "Папка данных", set()), (self.CMD_LOGS, "Журналы", set()), None,
                  (self.CMD_AUTOSTART, "Запускать при входе в Windows", {"checked"} if autostart.enabled() else set())]
        if self.pending_update:
            items.append((self.CMD_INSTALL, f"Установить обновление {self.pending_update.version}", set()))
        items += [(self.CMD_UPDATE, f"Проверить обновления (версия {self.version})", set()), None,
                  (self.CMD_QUIT, "Выключить центр и ботов", set())]
        return items

    def on_command(self, cmd: int) -> None:
        bot = {self.CMD_STARS: "starsbot", self.CMD_MANN: "mannbot", self.CMD_LIS: "lisbot"}.get(cmd)
        if cmd == self.CMD_PANEL:
            self.open_panel()
        elif bot:
            threading.Thread(target=self.open_bot, args=(bot,), daemon=True).start()
        elif cmd == self.CMD_DATA:
            os.startfile(self.home)  # type: ignore[attr-defined]  # noqa: S606
        elif cmd == self.CMD_LOGS:
            (self.home / "logs").mkdir(parents=True, exist_ok=True)
            os.startfile(self.home / "logs")  # type: ignore[attr-defined]  # noqa: S606
        elif cmd == self.CMD_AUTOSTART:
            on = not autostart.enabled()
            autostart.set_enabled(on, self.autostart_cmd())
            self.info("Автозапуск " + ("включён" if on else "выключен"),
                      "Центр будет запускаться при входе в Windows." if on else "Центр не будет запускаться сам.")
        elif cmd == self.CMD_UPDATE:
            threading.Thread(target=self.check_updates, args=(True,), daemon=True).start()
        elif cmd == self.CMD_INSTALL:
            self.install_update()
        elif cmd == self.CMD_QUIT:
            threading.Thread(target=self.client.quit, daemon=True).start()

    # ---------------------------------------------------------------- запуск
    def run(self) -> int:
        if self.client.alive():                                  # центр уже работает — просто показать его
            if not self.args.no_browser:
                self.open_panel()
            return 0
        self.start_center()
        threading.Thread(target=self.open_initial, name="browser", daemon=True).start()
        threading.Thread(target=self._update_loop, name="updates", daemon=True).start()

        use_tray = sys.platform == "win32" and not self.console and not self.args.no_tray
        if use_tray:
            try:
                from desktop.tray import Tray
                self.tray = Tray(str(self.app_dir / "center.ico"), "Центр · StarsBot" + (" (демо)" if self.args.demo else ""),
                                 self.menu, self.on_command, self.open_panel, poll=self.center_running)
                self.tray.run()                                   # до выхода центра или «Выключить»
            except Exception:  # noqa: BLE001 — без трея центр всё равно должен работать
                log.exception("значок в трее не запустился")
                self.tray = None
        try:
            while self.center_running():
                self.thread.join(0.5)
        except KeyboardInterrupt:
            self.client.quit()
            self.thread.join(60)
        return self.exit_code
