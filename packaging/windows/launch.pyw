"""Лаунчер Windows-версии Центра. Лежит в <программа>\\app рядом с center\\ и bots\\.

Ярлык установщика запускает его через runtime\\pythonw.exe (без окна консоли), «Центр.bat» — через
python.exe с параметром --console (без трея). Остальные параметры передаются центру как есть
(--demo, --port, --home, --no-browser…); --no-tray — без значка в трее. Сама логика — в пакете desktop.
"""
from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

APP = Path(__file__).resolve().parent
BASE = APP.parent
RUNTIME = BASE / "runtime"
NODE = BASE / "node"


def default_home() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "StarsCenter" / "center-data"


def message_box(text: str) -> None:
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, "Центр", 0x10)
    except Exception:  # noqa: BLE001 — не Windows или нет user32
        print(text, file=sys.stderr)


def main() -> None:
    console = "--console" in sys.argv[1:]
    argv = [a for a in sys.argv[1:] if a != "--console"]
    os.chdir(APP)
    sys.path.insert(0, str(APP))
    os.environ.setdefault("CENTER_HOME", str(default_home()))
    if NODE.is_dir():                                      # Node.js из сборки — для LisBot
        os.environ["PATH"] = str(NODE) + os.pathsep + os.environ.get("PATH", "")
    # Боты запускаются через sys.executable. Под pythonw.exe у детей не было бы нормального stdout,
    # поэтому подменяем на python.exe: центр всё равно запускает их с CREATE_NO_WINDOW, окон не будет.
    console_py = RUNTIME / "python.exe"
    if sys.platform == "win32" and console_py.exists():
        sys.executable = str(console_py)
    if sys.stdout is None:                                 # pythonw: argparse/print не должны падать
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
        sys.stderr = sys.stdout
        windowed = True
    else:
        windowed = False
        # вывод в файл/канал на Windows идёт в кодировке ANSI (cp1251/cp1252) — кириллица роняла бы print
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass

    try:
        from desktop.app import Desktop
        code = Desktop(argv, app_dir=APP, console=console).run()
    except Exception:  # noqa: BLE001
        if windowed:
            home = Path(os.environ["CENTER_HOME"])
            crash = home / "logs" / "launcher-crash.log"
            crash.parent.mkdir(parents=True, exist_ok=True)
            crash.write_text(traceback.format_exc(), encoding="utf-8")
            message_box(f"Центр не запустился. Подробности: {crash}")
        raise
    sys.exit(code)


if __name__ == "__main__":
    main()
