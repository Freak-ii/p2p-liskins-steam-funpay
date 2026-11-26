"""Мини-приложение (окно магазина, как у BotFather): где взять https-адрес и как поставить кнопку «Магазин».

Два способа, по приоритету:
  1. miniapp.url — постоянный адрес (GitHub Pages публичного репозитория со страницей web/miniapp/index.html).
  2. miniapp.tunnel: cloudflared — запуск с ПК. ShopBot поднимает отдельный крошечный сервер на 127.0.0.1:<port>
     (по умолчанию 8807), который отдаёт ТОЛЬКО страницу витрины, и запускает бесплатный туннель Cloudflare
     (`cloudflared tunnel --url …`). Адрес вида https://xxx.trycloudflare.com меняется при каждом запуске, поэтому
     бот сам обновляет кнопку меню (setChatMenuButton). Панель ShopBot (порт 8806) через туннель не видна.
cloudflared.exe ищется в miniapp.cloudflared, рядом с приложением (tools\\cloudflared.exe) и в PATH.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import urlencode

from aiohttp import web

log = logging.getLogger("miniapp")
PAGE = Path(__file__).parent / "web" / "miniapp" / "index.html"
TUNNEL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def page_query(cfg, bot_username: str, hidden: set[str] | frozenset = frozenset()) -> str:
    q = {"bot": bot_username}
    if hidden:                                # товары, снятые в Пульте Орбиты: плиток в мини-приложении нет
        q["hide"] = ",".join(sorted(hidden))
    name = str(cfg.get("general.shop_name") or "")
    if name:
        q["name"] = name
    ch = str(cfg.get("channel.chat_id") or "")
    if ch.startswith("@"):
        q["channel"] = ch[1:]
    return urlencode(q)


def find_cloudflared(cfg) -> str | None:
    given = str(cfg.get("miniapp.cloudflared") or "").strip()
    cands = [given] if given else []
    exe = "cloudflared.exe" if os.name == "nt" else "cloudflared"
    root = Path(sys.executable).resolve().parent
    cands += [str(root.parent / "tools" / exe), str(root / "tools" / exe), str(Path.cwd() / "tools" / exe)]
    for c in cands:
        if c and Path(c).is_file():
            return c
    return shutil.which("cloudflared")


def static_app() -> web.Application:
    async def page(request):
        return web.FileResponse(PAGE, headers={"Cache-Control": "no-cache"})

    app = web.Application()
    app.add_routes([web.get("/", page), web.get("/index.html", page)])
    return app


class MiniApp:
    def __init__(self, shop, bot=None):
        self.shop = shop
        self.cfg = shop.cfg
        self.bot = bot
        self.url = ""                 # действующий адрес (с параметрами страницы)
        self.mode = "off"             # off | url | tunnel | waiting
        self.detail = ""
        self._runner: web.AppRunner | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None

    def status(self) -> dict:
        return {"mode": self.mode, "url": self.url, "detail": self.detail}

    def _full(self, base: str) -> str:
        base = base.rstrip("/") + "/"
        user = self.bot.bot_username if self.bot is not None else "your_shop_bot"
        orbit = getattr(self.shop, "orbit", None)
        return f"{base}?{page_query(self.cfg, user, orbit.menu_hidden() if orbit is not None else frozenset())}"

    async def refresh(self) -> None:
        """Адрес мини-приложения с новым списком скрытых товаров (кнопка меню бота)."""
        if self.url:
            await self._apply(self.url.split("?", 1)[0], self.mode)

    async def _apply(self, base: str, mode: str) -> None:
        self.url, self.mode = self._full(base), mode
        self.shop.db.kv_set("miniapp", self.status())
        log.info("мини-приложение: %s (%s)", self.url, mode)
        if self.bot is not None:
            await self.bot.setup_menu_button(self.url)

    async def start(self) -> None:
        fixed = str(self.cfg.get("miniapp.url") or "").strip()
        if fixed.startswith("https://"):
            await self._apply(fixed, "url")
            return
        if str(self.cfg.get("miniapp.tunnel") or "").lower() != "cloudflared" or self.shop.demo:
            self.detail = "адрес не задан, туннель выключен"
            return
        port = int(self.cfg.get("miniapp.port") or 8807)
        self._runner = web.AppRunner(static_app())
        await self._runner.setup()
        try:
            await web.TCPSite(self._runner, "127.0.0.1", port).start()
        except OSError:
            self.detail = f"порт {port} занят"
            log.warning("мини-приложение: %s", self.detail)
            return
        self._task = asyncio.create_task(self._tunnel_loop(port))

    async def _tunnel_loop(self, port: int) -> None:
        delay = 10
        while True:
            exe = find_cloudflared(self.cfg)
            if not exe:
                self.mode, self.detail = "waiting", "нет cloudflared.exe (положите в tools\\ или укажите путь)"
                await asyncio.sleep(300)
                continue
            self.mode, self.detail = "waiting", "запускаю туннель Cloudflare"
            try:
                self._proc = await asyncio.create_subprocess_exec(
                    exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                    **({"creationflags": 0x08000000} if os.name == "nt" else {}))   # без окна консоли
                assert self._proc.stdout is not None
                async for raw in self._proc.stdout:
                    m = TUNNEL_RE.search(raw.decode("utf-8", "replace"))
                    if m and not self.url.startswith(m.group(0)):
                        await self._apply(m.group(0), "tunnel")
                        self.detail, delay = "", 10
                await self._proc.wait()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("туннель: %s", e)
            self.mode, self.detail = "waiting", f"туннель остановился, перезапуск через {delay} с"
            await asyncio.sleep(delay)
            delay = min(delay * 2, 600)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
        if self._proc is not None and self._proc.returncode is None:
            self._proc.terminate()
        if self._runner is not None:
            await self._runner.cleanup()
