"""Орбита — единый пульт товарных позиций всех ботов центра («Пульт Орбиты», страница /orbit).

Позиция — это товар целиком: звёзды, Premium, подарки, ключи Mann Co., скины, услуги магазина. Один
переключатель снимает позицию с орбиты сразу везде, где она продаётся: лоты FunPay, товары GGSEL и Ozon,
меню и мини-приложение Telegram-бота, посты и описания в Telegram-канале, черновики агента канала,
закупка LisBot. Можно закрыть и отдельный канал позиции (например, звёзды только на GGSEL).

Как решение доходит до ботов (на ходу, без перезапуска):
  1. Центр собирает из каталога POSITIONS, какие ключи закрыты у каждого бота, и атомарно пишет
     <папка данных центра>/shared/orbit.json с новым номером версии.
  2. Боты получают путь к файлу в CENTER_ORBIT (center/orbit_gate.py, копия в каждом боте), перечитывают его
     по времени изменения и сразу снимают лоты/товары; пока позиция закрыта, их сверка остатков держит её
     скрытой, а заказы и запросы магазина по ней не принимаются.
  3. Каждый бот пишет подтверждение shared/orbit-ack/<бот>.json: что сделано (ok), что нужно руками
     (manual: Авито и WB без API остатков) и ошибки. Пульт показывает это по каждой ячейке «позиция × канал».
Остановленный бот применит решение при следующем запуске. Уже оплаченные заказы закрытая позиция не
отменяет — их боты выдают как обычно.

Круги (center/loops.py) пересчитываются при каждом изменении: закрытая позиция выпадает из кругов, а канал,
через который не осталось ни одного круга, паркуется (orbit.auto_park) — например, закупка скинов под Steam,
когда ключи Mann сняты и Steam-кошельку больше некуда отдавать деньги.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path

from aiohttp import web

from center.loops import NODES, TRANSFERS, Edge, evaluate
from center.orbit_gate import ENV_BOT, ENV_PATH

log = logging.getLogger("center.orbit")

SYSTEM_NAME = "Орбита"

CHANNELS: dict[str, str] = {
    "funpay": "FunPay", "ggsel": "GGSEL", "ozon": "Ozon", "avito": "Авито", "wb": "Wildberries",
    "shop": "Telegram-бот (магазин)", "tgchannel": "Telegram-канал и описания", "steam": "Steam (закупка LisBot)",
    "market": "Market.CSGO (арбитраж LisBot)",
}
BOT_TITLES = {"starsbot": "StarsBot", "tggift": "TG Gift", "mannbot": "MannBot", "shopbot": "ShopBot",
              "lisbot": "LisBot", "agents": "Агент канала"}


def _ch(bind, src="", dst="", margin=0.0, note="") -> dict:
    return {"bind": list(bind), "from": src, "to": dst, "margin": margin, "note": note}


# Каталог позиций. bind — что закрыть у какого бота: (бот, ключ). Ключи понимает сам бот (см. orbit_gate).
# from/to/margin — ребро круга для канала продажи (у tgchannel ребра нет: это реклама и описания).
# margin — оценка маржи канала в % к себестоимости; правится в пульте (orbit.margins в center.yaml).
POSITIONS: list[dict] = [
    {"id": "stars", "title": "Звёзды Telegram", "emoji": "⭐", "group": "Telegram",
     "keywords": r"звёзд|звезд|stars",
     "channels": {
         "funpay": _ch([("starsbot", "stars")], "ton", "fp_rub", 2.4, "3-е место, себестоимость TON по живой закупке"),
         "ggsel": _ch([("starsbot", "ggsel:stars")], "ton", "ggsel_rub", 3.0),
         "ozon": _ch([("starsbot", "ozon:stars")], "ton", "card_rub", 3.0),
         "avito": _ch([("starsbot", "avito:stars")], "ton", "card_rub", 3.0, "объявления снимаются вручную"),
         "wb": _ch([("starsbot", "wb:stars")], "ton", "card_rub", 3.0, "коды снимаются в кабинете WB вручную"),
         "shop": _ch([("shopbot", "buy.stars"), ("starsbot", "supply:stars")], "ton", "ton", 5.0),
         "tgchannel": _ch([("shopbot", "post.stars"), ("agents", "stars")]),
     }},
    {"id": "stars_gift", "title": "Звёзды подарком", "emoji": "🧸", "group": "Telegram",
     "keywords": r"подарком",
     "channels": {
         "funpay": _ch([("tggift", "topup"), ("starsbot", "gifts")], "ton", "fp_rub", 3.0),
     }},
    {"id": "premium", "title": "Telegram Premium", "emoji": "💎", "group": "Telegram",
     "keywords": r"premium|премиум",
     "channels": {
         "funpay": _ch([("starsbot", "premium")], "ton", "fp_rub", 1.0),
         "ggsel": _ch([("starsbot", "ggsel:premium")], "ton", "ggsel_rub", 2.0),
         "shop": _ch([("shopbot", "buy.premium"), ("starsbot", "supply:premium")], "ton", "ton", 4.0),
         "tgchannel": _ch([("shopbot", "post.premium"), ("agents", "premium")]),
     }},
    {"id": "tg_gifts", "title": "Подарки Telegram", "emoji": "🎁", "group": "Telegram",
     "keywords": r"подар|gift",
     "channels": {
         "funpay": _ch([("tggift", "gifts")], "ton", "fp_rub", 4.0),
         "shop": _ch([("shopbot", "buy.gift"), ("tggift", "supply:gift"), ("starsbot", "supply:gift")],
                     "ton", "ton", 5.0),
         "tgchannel": _ch([("shopbot", "post.gift"), ("agents", "tg_gifts")]),
     }},
    {"id": "nft_gifts", "title": "NFT-подарки", "emoji": "🐸", "group": "Telegram",
     "keywords": r"\bnft\b",
     "channels": {
         "funpay": _ch([("tggift", "nft")], "ton", "fp_rub", 5.0),
         "tgchannel": _ch([("agents", "nft_gifts")]),
     }},
    {"id": "mann_keys", "title": "Ключи Mann Co.", "emoji": "🔑", "group": "Steam",
     "keywords": r"mann|манн|ключ\w* (для )?(пополнения )?steam",
     "channels": {
         "funpay": _ch([("mannbot", "keys")], "steam", "fp_rub", -22.0, "круг Lis → Steam → ключи теряет ~22%"),
         "shop": _ch([("shopbot", "buy.steam_keys"), ("mannbot", "supply:steam_keys")], "steam", "ton", -18.0),
         "tgchannel": _ch([("shopbot", "post.steam_keys"), ("agents", "mann_keys")]),
     }},
    {"id": "skins_steam", "title": "Скины под Steam (LisBot → ТП Steam)", "emoji": "🎯", "group": "Steam",
     "keywords": "",
     "channels": {
         "steam": _ch([("lisbot", "buy.steam")], "lis_usd", "steam", 3.0, "деньги остаются в Steam: выход только ключами"),
     }},
    {"id": "skins_arb", "title": "Арбитраж скинов Lis → Market.CSGO", "emoji": "🔁", "group": "Steam",
     "keywords": "",
     "channels": {
         "market": _ch([("lisbot", "buy.arb")], "lis_usd", "market_rub", 3.0, "цель ≥ +3% после комиссий"),
     }},
    {"id": "steam_topup", "title": "Пополнение Steam", "emoji": "🎮", "group": "Услуги магазина",
     "keywords": r"пополнен\w* steam|steam top",
     "channels": {
         "shop": _ch([("shopbot", "service.steam_topup")], "fp_rub", "ton", 6.0,
                     "закупка с баланса FunPay — круг без вывода 3%"),
         "tgchannel": _ch([("agents", "steam_topup")]),
     }},
    {"id": "vpn", "title": "VPN", "emoji": "🛡", "group": "Услуги магазина", "keywords": r"\bvpn\b|впн",
     "channels": {"shop": _ch([("shopbot", "service.vpn")], "card_rub", "ton", 40.0, "свой сервер"),
                  "tgchannel": _ch([("agents", "vpn")])}},
    {"id": "appstore", "title": "Карты App Store", "emoji": "🍏", "group": "Услуги магазина",
     "keywords": r"app ?store|itunes",
     "channels": {"shop": _ch([("shopbot", "service.appstore")], "usdt", "ton", 15.0),
                  "tgchannel": _ch([("agents", "appstore")])}},
    {"id": "spotify", "title": "Spotify", "emoji": "🎧", "group": "Услуги магазина", "keywords": r"spotify",
     "channels": {"shop": _ch([("shopbot", "service.spotify")], "fp_rub", "ton", 5.0),
                  "tgchannel": _ch([("agents", "spotify")])}},
    {"id": "ai_subs", "title": "Подписки Claude / ChatGPT", "emoji": "🤖", "group": "Услуги магазина",
     "keywords": r"chatgpt|claude",
     "channels": {"shop": _ch([("shopbot", "service.ai")], "fp_rub", "ton", 5.0),
                  "tgchannel": _ch([("agents", "ai_subs")])}},
    {"id": "yandex_plus", "title": "Яндекс Плюс", "emoji": "🟡", "group": "Услуги магазина",
     "keywords": r"яндекс ?плюс|yandex",
     "channels": {"shop": _ch([("shopbot", "service.yandex_plus")], "card_rub", "ton", 10.0),
                  "tgchannel": _ch([("agents", "yandex_plus")])}},
]
POS = {p["id"]: p for p in POSITIONS}
SALE = "sale"

SCHEMA = """
CREATE TABLE IF NOT EXISTS orbit_positions (
    id TEXT PRIMARY KEY, open INTEGER NOT NULL DEFAULT 1, channels_off TEXT NOT NULL DEFAULT '[]',
    reason TEXT DEFAULT '', changed_at REAL, by TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS orbit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, position TEXT, channel TEXT, action TEXT NOT NULL,
    reason TEXT DEFAULT '', by TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS orbit_kv (k TEXT PRIMARY KEY, v TEXT);
"""


class OrbitError(ValueError):
    pass


class Orbit:
    def __init__(self, center, clock=time.time):
        self.center = center
        self.events = center.events
        self.db = self.events.db
        self._lock = self.events._lock
        self.clock = clock
        with self._lock:
            self.db.executescript(SCHEMA)
            self.db.commit()
        name = "orbit_demo.json" if getattr(center, "demo", False) else "orbit.json"
        self.path = Path(center.home) / "shared" / name
        self._task: asyncio.Task | None = None
        self.last: dict = {}
        self._live: dict[str, tuple[float, float | None]] = {}

    # ------------------------------------------------------------------ настройки
    @property
    def cfg(self) -> dict:
        try:
            return self.center.cfg["orbit"]
        except KeyError:
            return {}

    def bot_env(self, bot: str) -> dict[str, str]:
        return {ENV_PATH: str(self.path), ENV_BOT: bot}

    def _kv(self, k: str, default=None):
        with self._lock:
            r = self.db.execute("SELECT v FROM orbit_kv WHERE k=?", (k,)).fetchone()
        return json.loads(r["v"]) if r else default

    def _kv_set(self, k: str, v) -> None:
        with self._lock:
            self.db.execute("INSERT INTO orbit_kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                            (k, json.dumps(v, ensure_ascii=False)))
            self.db.commit()

    def _rows(self) -> dict[str, dict]:
        with self._lock:
            rows = self.db.execute("SELECT * FROM orbit_positions").fetchall()
        return {r["id"]: {"open": bool(r["open"]), "channels_off": json.loads(r["channels_off"] or "[]"),
                          "reason": r["reason"] or "", "changed_at": r["changed_at"], "by": r["by"] or ""}
                for r in rows}

    def margin(self, pid: str, ch: str) -> tuple[float, str]:
        key = f"{pid}@{ch}"
        over = (self.cfg.get("margins") or {}).get(key)
        if over is not None:
            return float(over), "настройка"
        live = self._live_margin(pid, ch)
        if live is not None:
            return live, "факт"
        return float(POS[pid]["channels"][ch]["margin"]), "оценка"

    def _live_margin(self, pid: str, ch: str) -> float | None:
        """Маржа по факту из раздела экономики, если он умеет её считать (раздел «Экономика», center/economy.py)."""
        econ = getattr(self.center, "economy", None)
        fn = getattr(econ, "position_margin", None)
        if fn is None:
            return None
        key = f"{pid}@{ch}"
        hit = self._live.get(key)
        if hit is not None and self.clock() - hit[0] < 300:     # базы ботов читаем не чаще раза в 5 минут
            return hit[1]
        v = self._live_read(fn, pid, ch)
        self._live[key] = (self.clock(), v)
        return v

    @staticmethod
    def _live_read(fn, pid: str, ch: str) -> float | None:
        try:
            # нужна маржа канала ДО вывода денег: вывод (3% на карту и т.п.) — отдельное звено круга
            v = fn(pid, ch, include_withdraw=False)
            return None if v is None else float(v)
        except Exception:  # noqa: BLE001 — экономика без такого режима или с ошибкой не должна ронять пульт
            return None

    def transfers(self) -> list[dict]:
        over = self.cfg.get("transfers") or {}
        out = []
        for t in TRANSFERS:
            o = over.get(t["id"]) or {}
            out.append({**t, "enabled": bool(o.get("enabled", t["enabled"])), "pct": float(o.get("pct", t["pct"])),
                        "changed": bool(o)})
        return out

    # ------------------------------------------------------------------ состояние позиций
    def manual_state(self) -> dict[str, dict]:
        rows = self._rows()
        out = {}
        for p in POSITIONS:
            r = rows.get(p["id"]) or {"open": True, "channels_off": [], "reason": "", "changed_at": None, "by": ""}
            out[p["id"]] = r
        return out

    def edges(self, state: dict | None = None) -> list[Edge]:
        """Рёбра кругов: открытые каналы открытых позиций и включённые переводы."""
        state = state or self.manual_state()
        out = []
        for t in self.transfers():
            if t["enabled"]:
                out.append(Edge(t["id"], t["from"], t["to"], "transfer", t["pct"], t["title"]))
        for p in POSITIONS:
            st = state[p["id"]]
            if not st["open"]:
                continue
            for ch, c in p["channels"].items():
                if not c["from"] or ch in st["channels_off"]:
                    continue
                pct, src = self.margin(p["id"], ch)
                out.append(Edge(f"{p['id']}@{ch}", c["from"], c["to"], SALE, pct,
                                f"{p['title']} · {CHANNELS[ch]}", position=p["id"], channel=ch, source=src))
        return out

    def compute(self) -> dict:
        """Полное решение: состояние каждой ячейки «позиция × канал», круги и что закрыть у ботов."""
        state = self.manual_state()
        loops = evaluate(self.edges(state), float(self.cfg.get("min_loop_pct", 0.0) or 0.0))
        auto_park = bool(self.cfg.get("auto_park", True))
        no_circle = set(loops["no_circle"])
        positions, closed, reasons = [], {}, {}

        def close(bot: str, key: str, why: str) -> None:
            closed.setdefault(bot, set()).add(key)
            reasons.setdefault(bot, {}).setdefault(key, why)

        for p in POSITIONS:
            st = state[p["id"]]
            cells, sale_open = [], 0
            for ch, c in p["channels"].items():
                edge = f"{p['id']}@{ch}"
                if not st["open"]:
                    cell, why = "closed", f"{p['title']}: снята с орбиты" + (f" — {st['reason']}" if st["reason"] else "")
                elif ch in st["channels_off"]:
                    cell, why = "closed", f"{p['title']}: канал {CHANNELS[ch]} закрыт в Пульте Орбиты"
                elif c["from"] and edge in no_circle and auto_park:
                    cell, why = "parked", (f"{p['title']} · {CHANNELS[ch]}: нет круга — деньги отсюда не вернутся "
                                           "в оборот (Орбита припарковала канал)")
                else:
                    cell, why = "open", ""
                if c["from"] and cell == "open":
                    sale_open += 1
                cells.append({"channel": ch, "title": CHANNELS[ch], "state": cell, "why": why, "note": c["note"],
                              "sale": bool(c["from"]), "bind": [{"bot": b, "key": k} for b, k in c["bind"]],
                              "best": loops["best"].get(edge), "losing": edge in loops["losing"],
                              **({"margin": self.margin(p["id"], ch)[0], "margin_source": self.margin(p["id"], ch)[1]}
                                 if c["from"] else {})})
            # реклама и описания (tgchannel) живут, пока у позиции есть хоть один открытый канал продажи
            if st["open"] and not sale_open:
                for cell in cells:
                    if not cell["sale"] and cell["state"] == "open":
                        cell.update(state="parked", why=f"{p['title']}: нигде не продаётся — описания и посты убраны")
            for cell in cells:
                if cell["state"] != "open":
                    for b in cell["bind"]:
                        close(b["bot"], b["key"], cell["why"])
            best = [c["best"]["pct"] for c in cells if c.get("best") and c["state"] == "open"]
            pstate = "closed" if not st["open"] else ("open" if sale_open else "parked")
            positions.append({"id": p["id"], "title": p["title"], "emoji": p["emoji"], "group": p["group"],
                              "state": pstate, "open": st["open"], "reason": st["reason"], "by": st["by"],
                              "changed_at": st["changed_at"], "channels": cells,
                              "best_pct": max(best) if best else None,
                              "losing": bool(best) and max(best) < float(self.cfg.get("min_loop_pct", 0.0) or 0.0)})
        # ключ, который нужен другой открытой ячейке (поставщик магазина один на две позиции), не закрываем
        needed: dict[str, set] = {}
        for p in positions:
            for c in p["channels"]:
                if c["state"] == "open":
                    for b in c["bind"]:
                        needed.setdefault(b["bot"], set()).add(b["key"])
        closed = {b: sorted(k for k in keys if k not in needed.get(b, set())) for b, keys in closed.items()}
        closed = {b: keys for b, keys in closed.items() if keys}
        reasons = {b: {k: v for k, v in reasons[b].items() if k in closed.get(b, [])} for b in closed}
        return {"positions": positions, "closed": closed, "reasons": reasons, "loops": loops}

    # ------------------------------------------------------------------ файл решения для ботов
    def publish(self) -> dict:
        """Пересчитать решение и, если оно изменилось, записать новую версию orbit.json."""
        d = self.compute()
        body = {"positions": {p["id"]: {"title": p["title"], "state": p["state"], "open": p["state"] == "open"}
                              for p in d["positions"]},
                "closed": d["closed"], "reasons": d["reasons"]}
        sig = json.dumps(body, ensure_ascii=False, sort_keys=True)
        version = int(self._kv("version", 0) or 0)
        if sig != self._kv("signature") or not self.path.exists():
            version += 1
            self._kv_set("version", version)
            self._kv_set("signature", sig)
            self._kv_set("published_at", self.clock())
            self._write({"system": "orbit", "name": SYSTEM_NAME, "version": version, "updated_at": self.clock(), **body})
        self.last = {**d, "version": version}
        return self.last

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def acks(self) -> dict[str, dict]:
        out = {}
        d = self.path.parent / "orbit-ack"
        for f in d.glob("*.json") if d.is_dir() else []:
            try:
                a = json.loads(f.read_text(encoding="utf-8"))
                out[str(a.get("bot") or f.stem)] = a
            except (OSError, ValueError):
                continue
        return out

    # ------------------------------------------------------------------ действия пульта
    def _log(self, pid: str, ch: str, action: str, reason: str, by: str) -> None:
        with self._lock:
            self.db.execute("INSERT INTO orbit_log(ts,position,channel,action,reason,by) VALUES(?,?,?,?,?,?)",
                            (self.clock(), pid, ch, action, reason, by))
            self.db.commit()

    def _save_row(self, pid: str, row: dict) -> None:
        with self._lock:
            self.db.execute("INSERT INTO orbit_positions(id,open,channels_off,reason,changed_at,by) VALUES(?,?,?,?,?,?) "
                            "ON CONFLICT(id) DO UPDATE SET open=excluded.open, channels_off=excluded.channels_off, "
                            "reason=excluded.reason, changed_at=excluded.changed_at, by=excluded.by",
                            (pid, int(row["open"]), json.dumps(sorted(set(row["channels_off"]))), row["reason"],
                             self.clock(), row["by"]))
            self.db.commit()

    def set_position(self, pid: str, open_: bool, *, reason: str = "", by: str = "panel",
                     channel: str = "") -> str:
        p = POS.get(pid)
        if p is None:
            raise OrbitError(f"нет позиции {pid!r}; есть: {', '.join(POS)}")
        if channel and channel not in p["channels"]:
            raise OrbitError(f"у позиции {p['title']} нет канала {channel!r}; есть: {', '.join(p['channels'])}")
        row = self.manual_state()[pid]
        row = {**row, "by": by}
        if channel:
            off = set(row["channels_off"])
            (off.discard if open_ else off.add)(channel)
            row["channels_off"] = sorted(off)
            if open_ and not row["open"]:
                row["open"] = True           # открыли канал у снятой позиции — позиция возвращается только этим каналом
                row["channels_off"] = sorted(set(p["channels"]) - {channel})
            what = f"{p['title']} · {CHANNELS[channel]}"
        else:
            row["open"], row["reason"] = bool(open_), reason if not open_ else ""
            if open_:
                row["channels_off"] = []
            what = p["title"]
        self._save_row(pid, row)
        self._log(pid, channel, "open" if open_ else "close", reason, by)
        d = self.publish()
        text = f"{'↩️ Вернул на орбиту' if open_ else '⛔ Снял с орбиты'}: {what}" + (f" ({reason})" if reason else "")
        self.events.add("center", "info", "orbit", text, {"position": pid, "channel": channel, "version": d["version"]})
        return text + f" · версия решения {d['version']}"

    def set_margin(self, key: str, pct) -> None:
        pid, _, ch = key.partition("@")
        if pid not in POS or ch not in POS[pid]["channels"] or not POS[pid]["channels"][ch]["from"]:
            raise OrbitError(f"нет канала продажи {key!r}")
        m = dict(self.cfg.get("margins") or {})
        if pct in (None, ""):
            m.pop(key, None)
        else:
            m[key] = float(pct)
        # save() только дополняет настройки, а сброс к оценке удаляет ключ — поэтому раздел заменяется целиком
        self.center.cfg.user.setdefault("orbit", {})["margins"] = m
        self.center.cfg.save({})
        self.publish()

    def set_transfer(self, tid: str, *, enabled=None, pct=None) -> None:
        if tid not in {t["id"] for t in TRANSFERS}:
            raise OrbitError(f"нет перевода {tid!r}")
        cur = dict((self.cfg.get("transfers") or {}).get(tid) or {})
        if enabled is not None:
            cur["enabled"] = bool(enabled)
        if pct not in (None, ""):
            cur["pct"] = float(pct)
        self.center.cfg.save({"orbit": {"transfers": {tid: cur}}})
        self.publish()

    def history(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self.db.execute("SELECT * FROM orbit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ что видно в пульте
    def view(self) -> dict:
        d = self.publish()
        acks = self.acks()
        version = d["version"]
        workers = getattr(self.center, "workers", {})
        for p in d["positions"]:
            for c in p["channels"]:
                for b in c["bind"]:
                    b["title"] = BOT_TITLES.get(b["bot"], b["bot"])
                    b["status"], b["text"] = self._bind_status(b["bot"], b["key"], c["state"], acks, version, workers)
        circles = d["loops"]["circles"]
        return {"name": SYSTEM_NAME, "version": version, "path": str(self.path),
                "positions": d["positions"], "closed": d["closed"],
                "circles": circles[:60], "circles_total": len(circles),
                "stranded": d["loops"]["stranded"], "transfers": self.transfers(),
                "nodes": NODES, "channels": CHANNELS, "auto_park": bool(self.cfg.get("auto_park", True)),
                "min_loop_pct": float(self.cfg.get("min_loop_pct", 0.0) or 0.0),
                "acks": {k: {kk: v.get(kk) for kk in ("version", "ts")} for k, v in acks.items()},
                "history": self.history(30), "advice": self.advice(d)}

    def _bind_status(self, bot: str, key: str, cell: str, acks: dict, version: int, workers: dict) -> tuple[str, str]:
        if cell == "open":
            return "open", "продаётся"
        if bot == "agents":
            return "ok", "черновики по позиции отклоняются"
        a = acks.get(bot)
        w = workers.get(bot)
        running = w is not None and getattr(w, "state", "") == "running"
        if a and int(a.get("version") or 0) >= version:
            res = str((a.get("applied") or {}).get(key, "ok"))
            if res.startswith("manual"):
                return "manual", res.partition(":")[2].strip() or "нужно снять вручную"
            if res.startswith("error"):
                return "error", res.partition(":")[2].strip() or "ошибка"
            if res.startswith("skip"):
                return "ok", res.partition(":")[2].strip() or "у бота это не настроено"
            return "ok", "снято у бота"
        if not running:
            return "pending", "бот не запущен — снимет при запуске"
        return "pending", "бот применяет решение…"

    def advice(self, d: dict) -> list[str]:
        out = []
        for p in d["positions"]:
            if p["state"] == "open" and p["losing"]:
                out.append(f"{p['emoji']} {p['title']}: лучший круг {p['best_pct']:+.1f}% — в минусе. Снять с орбиты?")
            if p["state"] == "parked":
                out.append(f"{p['emoji']} {p['title']}: нет ни одного круга — Орбита держит её закрытой, пока круг "
                           "не появится (откройте позицию, которая забирает деньги из этого места, или перевод).")
        for s in d["loops"]["stranded"]:
            out.append(f"⚠️ {s['title']}: деньги сюда приходят, но выйти им некуда — проверьте открытые позиции и переводы.")
        return out

    # ------------------------------------------------------------------ агент канала
    def closed_keywords(self) -> list[tuple[str, re.Pattern]]:
        """Позиции, о которых агент канала не пишет: (название, регулярное выражение)."""
        d = self.last or self.publish()
        keys = set((d.get("closed") or {}).get("agents") or [])
        return [(POS[k]["title"], re.compile(POS[k]["keywords"], re.I)) for k in keys
                if k in POS and POS[k]["keywords"]]

    # ------------------------------------------------------------------ команды
    async def command(self, args: list[str], by: str = "remote") -> str:
        """orbit status | orbit close <позиция> [канал] [причина…] | orbit open <позиция> [канал]"""
        if not args or args[0] in ("status", "статус"):
            v = self.view()
            rows = [f"{SYSTEM_NAME}, версия решения {v['version']}"]
            for p in v["positions"]:
                off = [c["channel"] for c in p["channels"] if c["state"] != "open"]
                best = f" · лучший круг {p['best_pct']:+.1f}%" if p["best_pct"] is not None else ""
                rows.append(f"{p['id']}: {p['state']}{best}" + (f" · закрыто: {', '.join(off)}" if off and p["state"] == "open" else ""))
            return "\n".join(rows + v["advice"])
        act = args[0]
        if act not in ("close", "open") or len(args) < 2:
            return "orbit status | orbit close <позиция> [канал] [причина] | orbit open <позиция> [канал]"
        pid, rest = args[1], args[2:]
        channel = rest[0] if rest and pid in POS and rest[0] in POS[pid]["channels"] else ""
        reason = " ".join(rest[1:] if channel else rest)
        try:
            return self.set_position(pid, act == "open", reason=reason, by=by, channel=channel)
        except OrbitError as e:
            return str(e)

    # ------------------------------------------------------------------ жизненный цикл
    async def start(self) -> None:
        try:
            self.publish()
        except Exception:  # noqa: BLE001 — пульт не должен мешать запуску центра
            log.exception("orbit publish")
        self._task = asyncio.create_task(self._loop(), name="orbit")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        while True:                 # маржа «по факту» и ручная правка center.yaml меняют круги без кнопки
            await asyncio.sleep(60)
            try:
                self.publish()
            except Exception:  # noqa: BLE001
                log.exception("orbit publish")


def routes(center) -> list:
    STATIC = Path(__file__).resolve().parent / "static"

    def orb() -> Orbit:
        return center.orbit

    async def page(request: web.Request):
        return web.FileResponse(STATIC / "orbit.html", headers={"Cache-Control": "no-store"})

    async def get_view(request: web.Request):
        return web.json_response(orb().view())

    async def post_position(request: web.Request):
        d = await request.json()
        try:
            text = orb().set_position(str(d.get("id") or ""), bool(d.get("open")), reason=str(d.get("reason") or ""),
                                      channel=str(d.get("channel") or ""), by="panel")
        except OrbitError as e:
            return web.json_response({"ok": False, "message": str(e)}, status=400)
        return web.json_response({"ok": True, "message": text, "view": orb().view()})

    async def post_settings(request: web.Request):
        d = await request.json()
        o = orb()
        try:
            for key, pct in (d.get("margins") or {}).items():
                o.set_margin(key, pct)
            for tid, t in (d.get("transfers") or {}).items():
                o.set_transfer(tid, enabled=t.get("enabled"), pct=t.get("pct"))
            upd = {k: d[k] for k in ("auto_park", "min_loop_pct") if k in d}
            if upd:
                if "auto_park" in upd:
                    upd["auto_park"] = bool(upd["auto_park"])
                if "min_loop_pct" in upd:
                    upd["min_loop_pct"] = float(upd["min_loop_pct"])
                center.cfg.save({"orbit": upd})
                o.publish()
        except (OrbitError, ValueError, TypeError) as e:
            return web.json_response({"ok": False, "message": str(e)}, status=400)
        return web.json_response({"ok": True, "view": o.view()})

    return [web.get("/orbit", page), web.get("/api/orbit", get_view), web.post("/api/orbit/position", post_position),
            web.post("/api/orbit/settings", post_settings)]
