"""Запуск центра перепродажи: python -m center [--home ПАПКА] [--port 8800] [--demo] [--no-browser]."""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import socket
import sys
import threading
import urllib.request
import webbrowser
from logging.handlers import RotatingFileHandler

from center import __version__, winutil
from center.config import CenterConfig, default_home

log = logging.getLogger("center")


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def _is_center(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1.5) as r:
            return b'"center"' in r.read()
    except Exception:  # noqa: BLE001
        return False


def setup_logging(home) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)-16s %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)
    if sys.stdout is not None:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)
    (home / "logs").mkdir(parents=True, exist_ok=True)
    fh = RotatingFileHandler(home / "logs" / "center.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)


async def run(home, host: str, port: int, *, demo: bool, open_browser: bool, on_ready=None) -> None:
    from center.app import Center
    from center.web import start_web
    center = Center(home, demo=demo)
    token = center.cfg.ensure_token()
    runner = await start_web(center, host, port)
    shown = "127.0.0.1" if host in ("0.0.0.0", "") else host
    url = f"http://{shown}:{port}/login?token={token}"
    log.info("Центр перепродажи %s. Панель центра: http://%s:%s/", __version__, shown, port)
    if sys.stdout is not None:
        print(f"\n  Центр открыт: {url}\n  Данные: {home}\n  Ctrl+C — выход.\n", flush=True)
    if open_browser:
        webbrowser.open(url)
    await center.start()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, center.quit_event.set)
        except (NotImplementedError, RuntimeError):   # Windows
            pass
    done = threading.Event()
    # Windows: закрытие окна консоли крестиком — успеть мягко остановить ботов (Windows ждёт ~5 с),
    # остальное доделает Job Object: процессы ботов не переживут центр
    def console_closed():
        center.fast_exit = True
        loop.call_soon_threadsafe(center.quit_event.set)
        done.wait(4.5)
    winutil.on_console_close(console_closed)
    if on_ready:
        on_ready(center, url, loop)
    try:
        await center.quit_event.wait()
    finally:
        log.info("Завершение работы центра…")
        await center.shutdown(stop_timeout=3 if getattr(center, "fast_exit", False) else 15)
        await runner.cleanup()
        done.set()


def _utf8_output() -> None:
    """Windows: при выводе в файл или канал Python пишет в cp1252, и русский текст роняет центр. Пишем в UTF-8."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def main(argv: list[str] | None = None) -> None:
    _utf8_output()
    ap = argparse.ArgumentParser(prog="center", description="Центр перепродажи: LisBot (LIS-SKINS → Steam) + MannBot (FunPay)")
    ap.add_argument("--home", help="папка данных центра (по умолчанию ./center-data или $CENTER_HOME)")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--demo", action="store_true", help="MannBot в демо-режиме с эмуляцией FunPay и Steam")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--tray", action="store_true", help="значок в трее (портативная сборка для Windows)")
    args = ap.parse_args(argv)

    from pathlib import Path
    home = Path(args.home).expanduser().resolve() if args.home else default_home()
    if args.demo:
        home = home / "demo"
    home.mkdir(parents=True, exist_ok=True)
    cfg = CenterConfig(home)
    host = args.host or cfg["center"]["host"]
    port = args.port or int(cfg["center"]["port"])
    if not _port_free(host, port):
        if _is_center(port):
            print(f"Центр уже запущен на порту {port}")
            if not args.no_browser:
                webbrowser.open(f"http://127.0.0.1:{port}/login?token={cfg.ensure_token()}")
            return
        sys.exit(f"Порт {port} занят другой программой: укажите другой через --port")
    setup_logging(home)
    if args.tray:
        from center.tray import run_with_tray
        run_with_tray(lambda on_ready: run(home, host, port, demo=args.demo, open_browser=not args.no_browser,
                                           on_ready=on_ready))
        return
    try:
        asyncio.run(run(home, host, port, demo=args.demo, open_browser=not args.no_browser))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
