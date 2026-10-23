"""Лаунчер Windows-версии Центра. Лежит в <программа>\\app рядом с center\\ и bots\\.

Ярлык установщика запускает его через runtime\\pythonw.exe (без окна консоли), «Центр.bat» — через
python.exe с параметром --console. Остальные параметры передаются центру как есть (--demo, --port, --home…).
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


def disable_missing_lisbot(argv: list[str]) -> None:
    """Сборка без LisBot: выключаем его в center.yaml, если пользователь сам не решил иначе."""
    from center.config import CenterConfig
    home = Path(os.environ["CENTER_HOME"])
    if "--home" in argv[:-1]:
        home = Path(argv[argv.index("--home") + 1]).expanduser().resolve()
    if "--demo" in argv:
        home = home / "demo"
    cfg = CenterConfig(home)
    if "lisbot" not in (cfg.user.get("workers") or {}):
        cfg.save({"workers": {"lisbot": {"enabled": False}}})


def main() -> None:
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

    if not (APP / "bots" / "lisbot").is_dir():
        disable_missing_lisbot(argv)

    from center.__main__ import main as center_main
    try:
        center_main(argv)
    except SystemExit as e:
        if windowed and e.code not in (None, 0):
            message_box(str(e.code))
        raise
    except Exception:  # noqa: BLE001
        if windowed:
            home = Path(os.environ["CENTER_HOME"])
            crash = home / "logs" / "launcher-crash.log"
            crash.parent.mkdir(parents=True, exist_ok=True)
            crash.write_text(traceback.format_exc(), encoding="utf-8")
            message_box(f"Центр не запустился. Подробности: {crash}")
        raise


if __name__ == "__main__":
    main()
