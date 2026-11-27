"""Связи снабжения Орбиты: кто кого снабжает и работает ли каждая связь.

Общий секрет поставщиков. Telegram-магазин (ShopBot) берёт товар у StarsBot (звёзды, Premium), TG Gift (подарки)
и MannBot (ключи TF2) через их /api/supply/*, и каждый запрос несёт X-Supply-Token. Раньше один и тот же ключ
приходилось вписывать в пять мест (suppliers.*.token у магазина и supply.token у каждого поставщика). Теперь
Центр держит один секрет в своём зашифрованном хранилище и сам передаёт его всем ботам переменной
ORBITA_SUPPLY_TOKEN, а магазину ещё и настоящие адреса поставщиков (ORBITA_SUPPLY_URLS) — порт бота мог смениться,
если стандартный был занят. Свой supply.token в config.yaml бота по-прежнему работает (бот без Центра).
Первый раз Центр берёт уже вписанный ключ магазина, чтобы ничего не сломать; если его нет — создаёт новый.

Схема для главной страницы — graph(): узлы-боты, их площадки продаж (только включённые) и связи снабжения со
статусом (проверка /api/supply/ping тем же секретом, кэш на полминуты).
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from pathlib import Path
import aiohttp
import yaml

log = logging.getLogger("center.supply")

ENV_TOKEN = "ORBITA_SUPPLY_TOKEN"
ENV_URLS = "ORBITA_SUPPLY_URLS"
VAULT_KEY = "center.supply_token"
SUPPLIER_BOTS = ("starsbot", "tggift", "mannbot")      # кому Центр передаёт секрет, кроме самого магазина
# товар магазина → бот-поставщик
SHOP_SUPPLY = (("stars", "starsbot", "⭐ звёзды"), ("premium", "starsbot", "💎 Premium"),
               ("gifts", "tggift", "🎁 подарки"), ("steam_keys", "mannbot", "🔑 ключи TF2"))
# площадки продаж каждого бота: (ключ config.yaml с флагом enabled, подпись); funpay включён, если флаг не задан
SELL_CHANNELS = {
    "starsbot": (("funpay", "FunPay · звёзды"), ("ggsel", "GGSEL"), ("ozon", "Ozon"), ("avito", "Авито"),
                 ("wb", "Wildberries")),
    "tggift": (("funpay", "FunPay · подарки"),),
    "mannbot": (("funpay", "FunPay · ключи TF2"),),
    "lisbot": (("steam", "Steam · скины"),),
    "shopbot": (("telegram", "Telegram · покупатели магазина"),),
}
PING_TTL = 30.0


def _read_yaml(path: Path) -> dict:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}


class SupplyLinks:
    def __init__(self, center):
        self.center = center
        self._token: str | None = None
        self.token_source = ""             # vault | adopted | new | memory
        self._pings: dict[str, tuple[float, str, str]] = {}   # бот → (когда, статус, текст)
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ секрет
    def _bot_cfg(self, name: str) -> dict:
        w = self.center.workers.get(name)
        path = getattr(w, "_cfg_path", None)
        return _read_yaml(path) if isinstance(path, Path) else {}

    def _adoptable(self) -> str:
        """Ключ, который уже вписан магазину (так было до общего секрета) — берём его, чтобы не сломать связи."""
        sup = self._bot_cfg("shopbot").get("suppliers") or {}
        for name, _bot, _t in SHOP_SUPPLY:
            tok = str((sup.get(name) or {}).get("token") or "")
            if len(tok) >= 16 and "${" not in tok:
                return tok
        return ""

    @property
    def token(self) -> str:
        if self._token:
            return self._token
        vault = getattr(self.center, "vault", None)
        tok = ""
        try:
            tok = vault.get(VAULT_KEY) if vault is not None else ""
            self.token_source = "vault" if tok else ""
            if not tok:
                tok = self._adoptable()
                self.token_source = "adopted" if tok else "new"
                tok = tok or secrets.token_urlsafe(24)
                if vault is not None:
                    vault.set(VAULT_KEY, tok)
        except Exception as e:  # noqa: BLE001 — хранилище не читается: секрет живёт до перезапуска Центра
            log.warning("общий секрет поставщиков не сохранён: %s", e)
            tok = tok or self._adoptable() or secrets.token_urlsafe(24)
            self.token_source = "memory"
        self._token = tok
        return tok

    def regenerate(self) -> str:
        """Новый общий секрет. Боты получат его при следующем запуске — их нужно перезапустить."""
        self._token = secrets.token_urlsafe(24)
        vault = getattr(self.center, "vault", None)
        if vault is not None:
            try:
                vault.set(VAULT_KEY, self._token)
                self.token_source = "vault"
            except Exception as e:  # noqa: BLE001
                log.warning("общий секрет поставщиков не сохранён: %s", e)
                self.token_source = "memory"
        self._pings.clear()
        return "Создан новый общий секрет поставщиков. Перезапустите магазин и поставщиков, чтобы они его получили"

    def urls(self) -> dict[str, str]:
        out = {}
        for name, bot, _t in SHOP_SUPPLY:
            w = self.center.workers.get(bot)
            if w is not None and getattr(w, "base_url", ""):
                out[name] = w.base_url
        return out

    def bot_env(self, bot: str) -> dict[str, str]:
        if bot not in SUPPLIER_BOTS and bot != "shopbot":
            return {}
        env = {ENV_TOKEN: self.token}
        if bot == "shopbot":
            env[ENV_URLS] = json.dumps(self.urls())
        return env

    # ------------------------------------------------------------------ состояние связей
    def _running(self, name: str) -> bool:
        w = self.center.workers.get(name)
        return w is not None and w.state == "running"

    def _enabled(self, name: str) -> bool:
        return name in self.center.workers and self.center.enabled(name)

    def channel_on(self, bot: str, key: str, cfg: dict | None = None) -> bool:
        if key in ("steam", "telegram"):
            return True
        cfg = self._bot_cfg(bot) if cfg is None else cfg
        node = cfg.get(key) or {}
        return bool(node.get("enabled", key == "funpay"))

    async def _ping(self, bot: str) -> tuple[str, str]:
        hit = self._pings.get(bot)
        if hit and time.time() - hit[0] < PING_TTL:
            return hit[1], hit[2]
        w = self.center.workers[bot]
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5),
                                             headers={"X-Supply-Token": self.token}) as s:
                async with s.get(f"{w.base_url}/api/supply/ping") as r:
                    if r.status == 401:
                        res = ("bad", "поставщик не принял общий секрет — перезапустите его из Центра")
                    elif r.status != 200:
                        res = ("bad", f"поставщик ответил HTTP {r.status}")
                    else:
                        d = await r.json(content_type=None)
                        res = ("ok", "работает") if d.get("ok", True) else ("warn", str(d.get("reason") or "не готов"))
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            res = ("down", f"поставщик не отвечает ({type(e).__name__})")
        self._pings[bot] = (time.time(), *res)
        return res

    async def graph(self) -> dict:
        async with self._lock:
            return await self._graph()

    async def _graph(self) -> dict:
        titles = {n: w.title.split(" · ")[0] for n, w in self.center.workers.items()}
        cfgs = {n: self._bot_cfg(n) for n in self.center.workers if n != "lisbot"}
        nodes, links, unused = [], [], []
        for name, w in self.center.workers.items():
            if not self._enabled(name):
                continue
            nodes.append({"id": name, "title": titles[name], "state": w.state, "running": w.state == "running"})
            for key, label in SELL_CHANNELS.get(name, ()):
                if self.channel_on(name, key, cfgs.get(name, {})):
                    links.append({"from": name, "to": key, "to_title": label, "kind": "sell", **self._sell_status(name)})
                elif key == "funpay":
                    unused.append(f"{titles[name]}: FunPay выключен — только поставщик")
        shop_cfg = (cfgs.get("shopbot") or {}).get("suppliers") or {}
        if self._enabled("shopbot"):
            by_bot: dict[str, list[str]] = {}
            manual = []
            for item, bot, label in SHOP_SUPPLY:
                if str((shop_cfg.get(item) or {}).get("kind") or "http") == "manual":
                    manual.append(label)
                    continue
                by_bot.setdefault(bot, []).append(label)
            for bot, items in by_bot.items():
                link = {"from": bot, "to": "shopbot", "to_title": titles.get("shopbot", "ShopBot"),
                        "kind": "supply", "what": ", ".join(items)}
                link.update(await self._supply_status(bot))
                links.append(link)
            if manual:
                links.append({"from": "manual", "to": "shopbot", "to_title": titles.get("shopbot", "ShopBot"),
                              "kind": "supply", "what": ", ".join(manual), "status": "manual",
                              "text": "выдаёт оператор вручную"})
        if self._enabled("starsbot") and self._enabled("tggift"):
            sup = ((cfgs.get("tggift") or {}).get("giftshop") or {}).get("supply") or {}
            if sup.get("enabled", True):
                ok = self._running("starsbot") and self._running("tggift")
                links.append({"from": "starsbot", "to": "tggift", "to_title": titles.get("tggift", "TG Gift"),
                              "kind": "supply", "what": "⭐ звёзды на аккаунт подарков",
                              "status": "ok" if ok else "idle",
                              "text": "работает" if ok else "запустите StarsBot и TG Gift"})
        return {"nodes": nodes, "links": links, "unused": unused,
                "secret": {"source": self.token_source or ("vault" if self._token else ""),
                           "set": bool(self._token)}}

    def _sell_status(self, bot: str) -> dict:
        return {"status": "ok", "text": "продаёт"} if self._running(bot) else \
            {"status": "idle", "text": "бот не запущен"}

    async def _supply_status(self, bot: str) -> dict:
        if not self._enabled(bot):
            return {"status": "off", "text": "поставщик выключен в center.yaml"}
        if not self._running(bot):
            return {"status": "idle", "text": "поставщик не запущен"}
        st, text = await self._ping(bot)
        if st == "ok" and not self._running("shopbot"):
            return {"status": "idle", "text": "поставщик готов, магазин не запущен"}
        return {"status": st, "text": text}

    def status_line(self) -> str:
        return {"vault": "хранится в Центре", "adopted": "взят из настроек магазина", "new": "создан Центром",
                "memory": "временный (хранилище Центра недоступно)"}.get(self.token_source, "")



def routes(center) -> list:
    from aiohttp import web

    async def get_graph(request: web.Request):
        return web.json_response(await center.supply.graph())

    async def post_regenerate(request: web.Request):
        msg = center.supply.regenerate()
        center.events.add("center", "info", "supply.secret", msg)
        return web.json_response({"ok": True, "message": msg})

    return [web.get("/api/supply/graph", get_graph), web.post("/api/supply/secret", post_regenerate)]
