"""Запуск StarsBot как приложения: веб-интерфейс + управление ботом + автооткрытие браузера."""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import sys
import urllib.request
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

from starsbot.app.supervisor import MemoryLogHandler, Supervisor

log = logging.getLogger("app")
APP_NAME = "StarsBot"

DEMO_OVERRIDES = {
    "general": {"demo": True, "dry_run": False, "database": ":memory:", "log_file": ""},
    "funpay": {"lots": [{"stars": n, "offer_id": i + 1} for i, n in enumerate((50, 75, 100, 150, 250, 350, 500))],
               "game_id": 1},
    "risk": {"large_order_stars": 1500, "manual_review_score": 40},
    "rtba": {"enabled": True, "competitor_min_amount": 2, "min_margin_pct": 8},
    "notifications": {"telegram": {"enabled": False}},
    "premium": {"enabled": True, "pilot_orders": 0, "price_mode": "rtba", "target_position": 2,
                "quote_username": "my_shop_account",
                "lots": [{"months": 3, "offer_id": 301, "position": 0}, {"months": 6, "offer_id": 302, "position": 0},
                         {"months": 12, "offer_id": 303, "position": 1}]},
    "gifts": {"enabled": True, "pilot_orders": 0, "pause_between_gifts": 0,
              "lots": [{"stars": n, "offer_id": 100 + i, "position": 3 if n == 13 else 0}
                       for i, n in enumerate((13, 21, 43, 85, 50, 100, 150, 250, 350, 500))]},
    "treasury": {"enabled": True, "hub": {"enabled": True}, "bybit": {"enabled": True}, "daily_report_hour": 25},
}


def app_home(explicit: str | None = None) -> Path:
    """Папка с настройками и базой: рядом с программой (портативно) или указанная явно."""
    if explicit:
        return Path(explicit).expanduser().resolve()
    if os.environ.get("STARSBOT_HOME"):
        return Path(os.environ["STARSBOT_HOME"]).expanduser().resolve()
    if getattr(sys, "frozen", False):                      # PyInstaller: рядом с StarsBot.exe
        return Path(sys.executable).resolve().parent / "StarsBot-data"
    return Path.cwd()


def setup_logging(home: Path, level: str = "INFO", log_file: str | None = None) -> MemoryLogHandler:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)-8s %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    if sys.stdout is not None:                              # в оконной сборке консоли нет
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)
    path = Path(log_file) if log_file else home / "data" / "starsbot.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = RotatingFileHandler(path, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    mem = MemoryLogHandler()
    root.addHandler(mem)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    return mem


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def _is_starsbot(host: str, port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/ping", timeout=1.5) as r:
            return b"starsbot" in r.read()
    except Exception:  # noqa: BLE001
        return False


async def run_app(home: Path, *, host: str, port: int, open_browser: bool, demo: bool, autostart: bool | None,
                  mem: MemoryLogHandler) -> None:
    from starsbot.web.server import start_web
    from starsbot.ozon.fake import DEMO_PRODUCTS
    from starsbot.ggsel.fake import DEMO_PREMIUM_PRODUCTS, DEMO_PRODUCTS as GG_PRODUCTS
    from starsbot.avito.fake import DEMO_PRODUCTS as AVITO_DEMO
    shop = os.environ.get("STARSBOT_PROFILE") == "giftshop"
    demo_over = {**DEMO_OVERRIDES, "ozon": {"enabled": True, "products": DEMO_PRODUCTS},
                 "ggsel": {"enabled": True, "products": GG_PRODUCTS, "premium_products": DEMO_PREMIUM_PRODUCTS},
                 "avito": {"enabled": True, "products": AVITO_DEMO},
                 "wb": {"enabled": True, "products": [{"stars": n, "nm_id": f"WB{n}", "price": p} for n, p in
                                                      ((100, 189), (250, 459), (500, 899))]}}
    if shop:
        demo_over = {k: v for k, v in DEMO_OVERRIDES.items() if k in ("general", "notifications")}
        demo_over["gifts"] = {"pause_between_gifts": 0}
        demo_over["giftshop"] = {"nft": {"rub_per_ton": 80.0}}      # курс TON в демо (≈ $0.95 × 84 ₽)
    sup = Supervisor(home, demo=demo, overrides=demo_over if demo else None)
    token = sup.ensure_token()
    if demo and shop:
        from starsbot.giftshop.demo import run_shop_demo
        sup.engine.cfg.deal.message_rate_limit_seconds = 0
        await sup.engine.lot_manager.create_missing()
        await run_shop_demo(sup.engine, sup.engine.fp)
        log.info("Демо TG Gift: лоты выставлены, 2 заказа подарков в CRM")
    elif demo:
        from starsbot.demo import run_scenario
        sup.engine.cfg.deal.message_rate_limit_seconds = 0
        await run_scenario(sup.engine, sup.engine.fp, sup.engine.wallet, verbose=False)
        from starsbot.ozon.fake import run_ozon_demo
        await run_ozon_demo(sup.engine, sup.engine.wallet)
        from starsbot.ggsel.fake import run_ggsel_demo
        await run_ggsel_demo(sup.engine, sup.engine.wallet)
        from starsbot.demo import run_gift_demo
        sup.engine.cfg.gifts.pause_between_gifts = 0
        await run_gift_demo(sup.engine, sup.engine.fp, sup.engine.wallet)
        from starsbot.demo import run_premium_demo
        await run_premium_demo(sup.engine, sup.engine.fp, sup.engine.wallet)
        from starsbot.avito.fake import run_avito_demo
        await run_avito_demo(sup.engine, sup.engine.wallet)
        from starsbot.wb.channel import run_wb_demo
        await run_wb_demo(sup.engine, sup.engine.wallet)
        from starsbot.treasury.fake import run_treasury_demo
        await run_treasury_demo(sup)
        log.info("Демо-сценарий выполнен: заказы FunPay, Ozon, GGSEL, Авито и Wildberries в CRM")
    runner = await start_web(sup, host, port, mem)
    shown_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
    url = f"http://{shown_host}:{port}/login?token={token}"
    log.info("Интерфейс запущен: http://%s:%s/", shown_host, port)   # токен в журнал не пишем
    if sys.stdout is not None:
        name = "TG Gift" if shop else APP_NAME
        print(f"\n  {name} открыт: {url}\n  Данные: {home}\n  Закройте это окно или нажмите Ctrl+C для выхода.\n",
              flush=True)
    if open_browser:
        webbrowser.open(url)
    want_autostart = sup.cfg.general.autostart if autostart is None else autostart
    if want_autostart and not demo:
        if sup.configured:
            log.info(await sup.start())
        else:
            log.warning("Автозапуск пропущен: настройки не заполнены — откройте мастер настройки")

    stop = asyncio.Event()
    sup.quit_event = stop
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):         # Windows
            pass
    try:
        await stop.wait()
    finally:
        log.info("Завершение работы…")
        await sup.shutdown()
        await runner.cleanup()


def main(argv: list[str] | None = None) -> None:
    import argparse
    ap = argparse.ArgumentParser(prog=APP_NAME, description="StarsBot — автопродажа Telegram Stars на FunPay через Fragment")
    ap.add_argument("--home", help="папка с настройками и базой (по умолчанию — рядом с программой)")
    ap.add_argument("--host", default=None, help="адрес интерфейса (по умолчанию 127.0.0.1)")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--no-browser", action="store_true", help="не открывать браузер")
    ap.add_argument("--headless", action="store_true", help="сервер/VPS: без браузера, бот стартует сам")
    ap.add_argument("--demo", action="store_true", help="демо с эмуляцией FunPay, Fragment и TON-кошелька")
    ap.add_argument("--profile", choices=("stars", "giftshop"), default=None,
                    help="giftshop — бот TG Gift (подарки Telegram в разделе FunPay «Подарки»)")
    args = ap.parse_args(argv)
    if args.profile:
        os.environ["STARSBOT_PROFILE"] = args.profile

    home = app_home(args.home)
    if args.demo:
        home = home / "demo"
    home.mkdir(parents=True, exist_ok=True)
    from starsbot.config import load_config
    cfg = load_config(home / "config.yaml" if (home / "config.yaml").exists() else None)
    host = args.host or os.environ.get("STARSBOT_HOST") or cfg.dashboard.host or "127.0.0.1"
    port = args.port or int(os.environ.get("STARSBOT_PORT") or cfg.dashboard.port or 8083)
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "") else host

    if not _port_free(host, port):
        if _is_starsbot(probe_host, port):
            # Уже запущен — просто открываем окно существующего экземпляра
            from starsbot.config import load_config as lc
            token = lc(home / "config.yaml").dashboard.token if (home / "config.yaml").exists() else ""
            if not (args.no_browser or args.headless):
                webbrowser.open(f"http://{probe_host}:{port}/login?token={token}")
            print(f"{APP_NAME} уже запущен на порту {port}")
            return
        for p in range(port + 1, port + 50):
            if _port_free(host, p):
                port = p
                break

    mem = setup_logging(home, cfg.general.log_level, None)
    try:
        asyncio.run(run_app(home, host=host, port=port, open_browser=not (args.no_browser or args.headless),
                            demo=args.demo, autostart=True if args.headless else None, mem=mem))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
