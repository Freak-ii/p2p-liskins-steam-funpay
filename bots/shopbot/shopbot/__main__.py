"""python -m shopbot --home <папка> --port 8806 [--demo] — панель + Telegram-бот магазина."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from aiohttp import web

from shopbot.channel import Channel
from shopbot.config import Config
from shopbot.db import Database
from shopbot.engine import Shop
from shopbot.tg import ShopTelegram
from shopbot.web.server import make_app

log = logging.getLogger("app")


class App:
    def __init__(self, home: Path, *, demo: bool = False):
        self.home = Path(home) / ("demo" if demo else "")
        self.demo = demo
        self.cfg = Config(self.home, demo=demo)
        self.db = Database(":memory:" if demo else self.home / "data" / "shopbot.sqlite3")
        self.shop = Shop(self.cfg, self.db, demo=demo)
        self.bot = ShopTelegram(self.shop, "" if demo else str(self.cfg.get("telegram.bot_token") or ""),
                                str(self.cfg.get("telegram.api") or "https://api.telegram.org"))
        self.channel = Channel(self.shop)

    async def start(self) -> None:
        if self.demo:
            from shopbot.demo import run_scenario
            await run_scenario(self.shop, self.bot)
        await self.shop.start()


def setup_logging(home: Path) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)-8s %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if sys.stdout is not None:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)
    (home / "data").mkdir(parents=True, exist_ok=True)
    fh = RotatingFileHandler(home / "data" / "shopbot.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


async def amain(args) -> None:
    app = App(Path(args.home), demo=args.demo)
    setup_logging(app.home)
    await app.start()
    runner = web.AppRunner(make_app(app))
    await runner.setup()
    try:
        await web.TCPSite(runner, args.host, args.port).start()
    except OSError:
        print(f"порт {args.port} занят другой программой", flush=True)
        raise SystemExit(3)
    token = app.cfg.get("dashboard.token")
    log.info("ShopBot %s на http://%s:%s/login?token=%s", "ДЕМО" if args.demo else "", args.host, args.port, token)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):     # Windows
            pass
    await stop.wait()
    await app.shop.stop()
    await runner.cleanup()


def main() -> None:
    p = argparse.ArgumentParser("shopbot")
    p.add_argument("--home", default=os.environ.get("SHOPBOT_HOME", "."))
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8806)
    p.add_argument("--demo", action="store_true")
    p.add_argument("--no-browser", action="store_true")
    args = p.parse_args()
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
