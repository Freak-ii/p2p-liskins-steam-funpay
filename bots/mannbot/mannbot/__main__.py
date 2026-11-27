"""Точка входа: python -m mannbot [--demo] [--headless] [--home DIR] [--port N] | --console-demo."""
from __future__ import annotations

import asyncio
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from mannbot.analytics.analytics import Analytics
from mannbot.config import load_config
from mannbot.core.engine import Engine
from mannbot.crm.db import Database
from mannbot.notify.notifier import Notifier


def setup_logging(cfg) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)-8s %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(getattr(logging, str(cfg.general.log_level).upper(), logging.INFO))
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    if cfg.general.log_file:
        Path(cfg.general.log_file).parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(cfg.general.log_file, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


def build(cfg):
    db = Database(cfg.general.database)
    notifier = Notifier(cfg.notifications, db)
    if cfg.general.demo:
        from mannbot.demo import FakeFunPay, FakeSteam
        fp, steam = FakeFunPay(), FakeSteam(keys=60)
    else:
        from mannbot.funpay.client import FunPayClient
        from mannbot.steam.client import SteamClient
        fp = FunPayClient(cfg.funpay.golden_key, cfg.funpay.user_agent, dry_run=cfg.general.dry_run)
        steam = SteamClient(cfg.steam, dry_run=cfg.general.dry_run)
    if not (cfg.get("funpay") or {}).get("enabled", True):    # «Только поставщик»: FunPay только чтобы выключить лот
        from mannbot.funpay.off import FunPayOff
        real = fp
        fp = FunPayOff((lambda: real) if (cfg.funpay.golden_key or cfg.general.demo) else None)
    engine = Engine(cfg, db, fp, steam, notifier)
    analytics = Analytics(cfg, db, notifier, engine)
    engine.analytics = analytics
    return db, notifier, fp, steam, engine, analytics


async def run_demo(cfg, web: bool) -> None:
    from mannbot.demo import run_scenario
    db, notifier, fp, steam, engine, analytics = build(cfg)
    await run_scenario(engine, fp, steam)
    print("\n" + "═" * 60 + "\n" + analytics.text_report())
    print("\nАлерты продавцу:")
    for level, text in notifier.sent:
        print(f"  [{level}] {text.splitlines()[0][:140]}")


def main() -> None:
    """По умолчанию — приложение (веб-интерфейс + управление ботом).
    --console-demo — консольный прогон демо-сценария (старое поведение --demo без интерфейса)."""
    if "--console-demo" in sys.argv:
        overrides = {"general": {"demo": True, "dry_run": False, "database": ":memory:", "log_file": "",
                                 "log_level": "WARNING"},
                     "funpay": {"lots": [{"offer_id": 1, "node_id": 1808, "game_id": 435, "keys_per_unit": 1}]},
                     "risk": {"manual_review_score": 40},
                     "rtba": {"enabled": True, "cost_per_key": 150, "competitor_min_amount": 2,
                              "ceiling_price": 260, "undercut_abs": 0.5},
                     "notifications": {"telegram": {"enabled": False}}}
        cfg = load_config(None, overrides)
        setup_logging(cfg)
        asyncio.run(run_demo(cfg, web=False))
        return
    from mannbot.app.launcher import main as app_main
    app_main([a for a in sys.argv[1:]])


if __name__ == "__main__":
    main()
