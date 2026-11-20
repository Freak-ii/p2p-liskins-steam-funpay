"""Точка входа: python -m starsbot [--demo] [--headless] [--home DIR] [--port N] | --console-demo."""
from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
from pathlib import Path


async def run_console_demo() -> None:
    from starsbot.app.launcher import DEMO_OVERRIDES
    from starsbot.app.supervisor import Supervisor
    from starsbot.demo import run_scenario
    with tempfile.TemporaryDirectory() as home:
        sup = Supervisor(Path(home), demo=True, overrides=DEMO_OVERRIDES)
        await run_scenario(sup.engine, sup.engine.fp, sup.engine.wallet)
        from starsbot.treasury.fake import run_treasury_demo
        steps = await run_treasury_demo(sup)
        if steps:
            print("\nКазначейство (демо):")
            for line in steps:
                print("  • " + line)
            print("  " + sup.engine.treasury.summary_text().replace("\n", "\n  "))
        print("\n" + "═" * 60 + "\n" + sup.analytics.text_report())
        print("\nКалькулятор (цена → чистыми → себестоимость → прибыль):")
        for f in sup.econ.calculator():
            print(f"  {f.stars:>4} ⭐: {f.price:>8.2f} ₽ → {f.net_rub:>8.2f} ₽ − {f.cost_rub or 0:>7.2f} ₽ = "
                  f"{f.profit if f.profit is not None else '—':>7} ₽ ({f.margin_pct}%)")
        print("\nАлерты продавцу:")
        for level, text in sup.notifier.sent:
            print(f"  [{level}] {text.splitlines()[0][:140]}")


def main() -> None:
    """По умолчанию — приложение (веб-интерфейс + управление ботом).
    --console-demo — консольный прогон демо-сценария без интерфейса."""
    if "--console-demo" in sys.argv:
        logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
        asyncio.run(run_console_demo())
        return
    from starsbot.app.launcher import main as app_main
    app_main([a for a in sys.argv[1:]])


if __name__ == "__main__":
    main()
