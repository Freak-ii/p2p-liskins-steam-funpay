"""Значок центра в трее (портативная сборка для Windows): центр работает в фоне без окна консоли.

Меню: открыть панель, открыть папку данных, выход (мягко останавливает ботов). Подсказка значка
показывает состояние ботов. Нужны пакеты pystray и Pillow; без них центр работает как обычно, без значка.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import threading
import webbrowser

log = logging.getLogger("center.tray")

STATE_SHORT = {"running": "работает", "starting": "запуск", "backoff": "перезапуск", "crashed": "упал",
               "not_configured": "нужна настройка", "missing_deps": "нет зависимостей", "stopped": "остановлен",
               "disabled": "выключен"}


def make_icon_image(size: int = 64, color: str = "#2563eb"):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 2, size - 3, size - 3), radius=size // 5, fill=color)
    # две стрелки «купил → продал»: символ перепродажи
    m, w = size // 2, max(2, size // 12)
    d.line((size * .22, m - size * .12, size * .78, m - size * .12), fill="white", width=w)
    d.polygon([(size * .78, m - size * .24), (size * .9, m - size * .12), (size * .78, m)], fill="white")
    d.line((size * .22, m + size * .14, size * .78, m + size * .14), fill="white", width=w)
    d.polygon([(size * .22, m + size * .02), (size * .1, m + size * .14), (size * .22, m + size * .26)], fill="white")
    return img


def _open_folder(path) -> None:
    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606
    else:
        subprocess.Popen(["xdg-open" if sys.platform != "darwin" else "open", str(path)])


def run_with_tray(make_coro) -> None:
    """make_coro(on_ready) → корутина центра. Центр крутится в отдельном потоке, значок — в главном."""
    try:
        import pystray
    except ImportError:
        log.warning("pystray не установлен — центр работает без значка в трее")
        asyncio.run(make_coro(None))
        return

    ctx: dict = {}
    ready = threading.Event()

    def on_ready(center, url, loop):
        ctx.update(center=center, url=url, loop=loop)
        ready.set()

    def worker():
        try:
            asyncio.run(make_coro(on_ready))
        except Exception:  # noqa: BLE001
            log.exception("центр завершился с ошибкой")
        finally:
            ready.set()
            icon.stop()

    def open_panel(icon_, item=None):
        if ctx.get("url"):
            webbrowser.open(ctx["url"])

    def open_data(icon_, item):
        if ctx.get("center"):
            _open_folder(ctx["center"].home)

    def quit_(icon_, item):
        icon_.title = "Центр: останавливаю ботов…"
        if ctx.get("loop"):
            ctx["loop"].call_soon_threadsafe(ctx["center"].quit_event.set)
        else:
            icon_.stop()

    icon = pystray.Icon("resale-center", make_icon_image(), "Центр перепродажи: запуск…", menu=pystray.Menu(
        pystray.MenuItem("Открыть панель", open_panel, default=True),
        pystray.MenuItem("Папка данных", open_data),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Выход (остановить ботов)", quit_),
    ))

    def status_loop(icon_):
        icon_.visible = True
        ready.wait()
        was_bad = None
        while ctx.get("center") and not ctx["center"].quit_event.is_set():
            try:
                bots = list(ctx["center"].workers.values())
                bad = any(w.state in ("crashed", "missing_deps", "not_configured") for w in bots)
                icon_.title = ("Центр перепродажи\n" + "\n".join(
                    f"{w.title.split(' ·')[0]}: {STATE_SHORT.get(w.state, w.state)}" for w in bots))[:120]
                if bad != was_bad:                       # жёлтый значок — боту нужна помощь
                    icon_.icon = make_icon_image(color="#d97706" if bad else "#2563eb")
                    was_bad = bad
            except Exception:  # noqa: BLE001
                pass
            threading.Event().wait(5)

    threading.Thread(target=worker, name="center", daemon=False).start()
    icon.run(setup=status_loop)
