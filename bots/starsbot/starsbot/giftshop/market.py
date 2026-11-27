"""Автоподбор NFT-лотов TG Gift для раздела «Подарки» (funpay.com/lots/3064/).

Раз в несколько часов бот смотрит раздел: какие коллекционные подарки там продают (продажа, не аренда),
сколько продавцов с отзывами, почём самый дешёвый. Для популярных коллекций берёт котировку обоих путей
(Fragment и рынок Telegram) и считает, сколько останется после комиссий, если встать чуть ниже лидера.
Рейтинг = маржа × ликвидность (продавцов и предложений) × сезон. Выставляются лучшие N, слабые выключаются,
место всей страницы бот не занимает. Ультра-дорогие коллекции (дороже max_cost_rub) сами не берутся — только
вручную из панели (pinned или «Добавить NFT»). Ручные решения (manual) автоподбор не трогает.
"""
from __future__ import annotations

import logging
import math
import statistics
import time
from datetime import datetime

from starsbot.funpay.client import FunPayError
from starsbot.giftshop.collections import Collection, find_collection, season_active
from starsbot.giftshop.service import lot_kind

log = logging.getLogger("giftshop")

DEFAULTS = {
    "enabled": True,
    "max_lots": 8,               # сколько NFT-лотов держать сразу (страницу целиком не занимаем)
    "max_cost_rub": 5000.0,      # дороже — только вручную
    "min_cost_rub": 150.0,       # совсем дешёвые не берём: газ и риск съедают маржу
    "min_margin_pct": 8.0,       # ожидаемая маржа после комиссий при цене чуть ниже лидера
    "min_sellers": 2,            # коллекцию продают хотя бы N продавцов с отзывами — значит, есть спрос
    "min_reviews": 10,           # продавец считается, если у него не меньше N отзывов
    "max_quotes": 25,            # сколько коллекций котировать за проход (Fragment не дёргаем без меры)
    "rescan_hours": 6,
    "season_boost": 1.6,         # множитель рейтинга для сезонных коллекций в их сезон
    "keep_bonus": 1.2,           # гистерезис: уже выставленным коллекциям — бонус, чтобы лоты не мигали
    "undercut_pct": 1.0,         # насколько встаём ниже лидера при оценке маржи
    "pinned": [],                # коллекции (slug), которые держать всегда (дорогие — тоже)
    "banned": [],                # никогда не брать
}


class Assortment:
    def __init__(self, svc):
        self.svc = svc
        self.engine = svc.engine
        self.db = svc.db
        self.db.exec("CREATE TABLE IF NOT EXISTS nft_market (slug TEXT PRIMARY KEY, title TEXT, sellers INTEGER, "
                     "offers INTEGER, min_price REAL, median_price REAL, cost_rub REAL, route TEXT, net_rub REAL, "
                     "margin_pct REAL, score REAL, season INTEGER, chosen INTEGER, why TEXT, ts REAL)")

    @property
    def c(self) -> dict:
        return {**DEFAULTS, **(self.svc.scfg.get("assortment") or {})}

    @property
    def enabled(self) -> bool:
        return bool(self.c.get("enabled", True)) and self.svc.nft is not None and self.svc.nft.enabled

    def due(self) -> bool:
        last = float(self.db.kv_get("assortment_scan_ts") or 0)
        return time.time() - last >= float(self.c["rescan_hours"]) * 3600

    # ------------------------------------------------------------------ рынок раздела
    def group(self, public) -> dict[str, dict]:
        from starsbot.giftshop.engine import is_rent
        own = {str(l.get("offer_id")) for l in self.svc.all_lots if l.get("offer_id")}
        me = (self.engine.fp.username or "").lower()
        min_rev = int(self.c["min_reviews"])
        known = self.svc.nft.known_collections()
        out: dict[str, dict] = {}
        for c in public:
            if c.offer_id in own or (me and c.seller.lower() == me) or is_rent(c):
                continue
            coll = find_collection(c.description, known)
            if coll is None:
                continue
            g = out.setdefault(coll.slug, {"coll": coll, "prices": [], "sellers": set(), "offers": 0})
            g["offers"] += 1
            if (c.reviews or 0) >= min_rev:
                g["sellers"].add(c.seller.lower())
                g["prices"].append(float(c.price))
        return out

    def _markup(self) -> float:
        # раздел «Подарки» (3064) дороже «Звёзд»: FunPay добавляет 15.07% против 14.05% (расчёт lots/calc, 24.11.2026)
        v = self.svc.scfg.get("buyer_markup_pct")
        return 1 + float(15.07 if v is None else v) / 100

    def _keep(self) -> float:
        econ = self.engine.econ
        if econ is None:
            return 1.0
        f, w = econ.fee_pcts("funpay")
        return (1 - f / 100) * (1 - w / 100)

    # ------------------------------------------------------------------ проход
    async def scan(self, force: bool = False) -> str:
        if not self.enabled:
            return "автоподбор NFT выключен"
        if not force and not self.due():
            return "ещё рано"
        node = int(self.engine.cfg.funpay.node_id)
        try:
            public = await self.engine.fp.get_public_lots(node)
        except FunPayError as e:
            return f"раздел не прочитан: {e}"
        c = self.c
        groups = self.group(public)
        pinned = {str(x) for x in c.get("pinned") or []}
        banned = {str(x) for x in c.get("banned") or []}
        active = {l["collection"] for l in self.svc.nft_lots if l.get("sell", True)}
        now = datetime.now()
        # котируем самые ходовые (и уже выставленные/закреплённые) — по числу продавцов
        order = sorted(groups.values(), key=lambda g: (-(g["coll"].slug in pinned | active), -len(g["sellers"]),
                                                         -g["offers"]))
        rows = []
        quoted = 0
        markup, keep = self._markup(), self._keep()
        for g in order:
            coll: Collection = g["coll"]
            slug = coll.slug
            sellers, prices = len(g["sellers"]), sorted(g["prices"])
            row = {"slug": slug, "title": coll.title, "sellers": sellers, "offers": g["offers"],
                   "min_price": prices[0] if prices else None,
                   "median_price": statistics.median(prices) if prices else None,
                   "season": int(season_active(coll.season, now.month, now.day)), "cost_rub": None, "route": None,
                   "net_rub": None, "margin_pct": None, "score": 0.0, "why": ""}
            rows.append(row)
            if slug in banned:
                row["why"] = "в запрете"
                continue
            if not prices:
                row["why"] = "нет продавцов с отзывами"
                continue
            if sellers < int(c["min_sellers"]) and slug not in pinned:
                row["why"] = f"продавцов {sellers} < {c['min_sellers']}"
                continue
            if quoted >= int(c["max_quotes"]) and slug not in pinned | active:
                row["why"] = "не котировалась (лимит прохода)"
                continue
            quoted += 1
            try:
                q = await self.svc.nft.quote(coll, fresh=True)
            except Exception as e:  # noqa: BLE001
                row["why"] = f"котировка: {e}"
                continue
            cost = q.get("cost_rub")
            if not cost:
                row["why"] = "; ".join(q.get("notes") or []) or "не купить ни на Fragment, ни на рынке Telegram"
                continue
            row["cost_rub"], row["route"] = float(cost), q.get("best")
            lot_price = prices[0] * (1 - float(c["undercut_pct"]) / 100) / markup
            net = lot_price * keep
            row["net_rub"] = round(net, 2)
            row["margin_pct"] = round((net - cost) / cost * 100, 1)
            if cost > float(c["max_cost_rub"]) and slug not in pinned:
                row["why"] = f"дорогая ({cost:.0f} ₽) — только вручную"
                continue
            if cost < float(c["min_cost_rub"]) and slug not in pinned:
                row["why"] = f"слишком дешёвая ({cost:.0f} ₽)"
                continue
            if row["margin_pct"] < float(c["min_margin_pct"]) and slug not in pinned:
                row["why"] = f"маржа {row['margin_pct']}% < {c['min_margin_pct']}%"
                continue
            liquidity = math.log1p(sellers + g["offers"] / 2)
            score = max(0.1, row["margin_pct"]) * liquidity
            if row["season"]:
                score *= float(c["season_boost"])
            if slug in active:
                score *= float(c["keep_bonus"])
            row["score"] = round(score, 2)
        candidates = sorted([r for r in rows if r["score"] > 0], key=lambda r: -r["score"])
        manual = {l["collection"]: l for l in self.svc.nft_lots if l.get("manual")}
        slots = max(0, int(c["max_lots"]) - len([s for s in manual if manual[s].get("sell", True)]))
        chosen = [r["slug"] for r in candidates if r["slug"] in pinned]      # закреплённые — всегда
        rest = [r["slug"] for r in candidates if r["slug"] not in pinned and r["slug"] not in manual]
        chosen += rest[:max(0, slots - len(chosen))]
        for r in rows:
            if r["slug"] in chosen:
                r["why"] = "выбрана" + (" (закреплена)" if r["slug"] in pinned else "")
        changes = self.apply(chosen, groups)
        if any(x.startswith("+") for x in changes) and (self.svc.scfg.get("auto_create_lots", True)):
            try:
                report = await self.engine.lot_manager.create_missing(kinds=["shop"])
                changes += [r for r in report if "выставлен" in r]
            except Exception as e:  # noqa: BLE001
                log.warning("выставление NFT-лотов: %s", e)
        self.db.exec("DELETE FROM nft_market")
        for r in rows:
            self.db.exec("INSERT INTO nft_market(slug,title,sellers,offers,min_price,median_price,cost_rub,route,"
                         "net_rub,margin_pct,score,season,chosen,why,ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (r["slug"], r["title"], r["sellers"], r["offers"], r["min_price"], r["median_price"],
                          r["cost_rub"], r["route"], r["net_rub"], r["margin_pct"], r["score"], r["season"],
                          int(r["slug"] in chosen), r["why"], time.time()))
        self.db.kv_set("assortment_scan_ts", time.time())
        msg = (f"NFT: коллекций в разделе {len(groups)}, котировано {quoted}, выбрано {len(chosen)}"
               + (f"; {'; '.join(changes)}" if changes else ""))
        self.db.add_event("-", "assortment_scan", summary=msg)
        if changes:
            await self.engine.notifier.notify("INFO", "assortment", f"Ассортимент NFT: {'; '.join(changes)}",
                                              throttle_key="assortment")
        return msg

    def apply(self, chosen: list[str], groups: dict[str, dict]) -> list[str]:
        """Включить выбранные (добавить лот, если его нет), выключить автоподобранные, которые выпали."""
        lm = self.engine.lot_manager
        changes = []
        have = {l["collection"]: l for l in self.svc.nft_lots}
        for slug in chosen:
            lot = have.get(slug)
            if lot is None:
                lm.add_nft(groups[slug]["coll"] if slug in groups else self.svc.nft.collection(slug), auto=True)
                changes.append(f"+ {slug}")
            elif not lot.get("sell", True) and not lot.get("manual"):
                lm.set_nft_sell(slug, True)
                changes.append(f"вкл {slug}")
        for slug, lot in have.items():
            if lot.get("manual") or slug in chosen or not lot.get("sell", True):
                continue
            if lot.get("auto"):
                lm.set_nft_sell(slug, False)
                changes.append(f"выкл {slug}")
        return changes

    def table(self) -> list[dict]:
        return self.db.q("SELECT * FROM nft_market ORDER BY chosen DESC, score DESC, sellers DESC LIMIT 60")

    # ------------------------------------------------------------------ команды
    @property
    def _save(self):
        return getattr(self.engine.lot_manager, "save_config", None)

    def _set_list(self, key: str, slug: str, add: bool) -> list[str]:
        cur = [str(x) for x in (self.c.get(key) or [])]
        cur = sorted(set(cur) | {slug}) if add else [x for x in cur if x != slug]
        a = dict(self.svc.scfg.get("assortment") or {})
        a[key] = cur
        if self._save is not None:
            self._save({"giftshop": {"assortment": a}}, rebuild=False)
        self.svc.cfg.giftshop["assortment"] = a
        return cur

    async def command(self, args: list[str]) -> str:
        if not args:
            rows = self.table()[:12]
            if not rows:
                return "ассортимент ещё не подбирался: assort scan"
            return "\n".join(f"{'✅' if r['chosen'] else '·'} {r['title']}: продавцов {r['sellers']}, мин "
                             f"{r['min_price'] or 0:.0f} ₽, себест. {r['cost_rub'] or 0:.0f} ₽ ({r['route'] or '—'}), "
                             f"маржа {r['margin_pct'] if r['margin_pct'] is not None else '—'}% — {r['why']}"
                             for r in rows)
        cmd, rest = args[0].lower(), args[1:]
        if cmd == "scan":
            return await self.scan(force=True)
        if cmd in ("pin", "unpin", "ban", "unban") and rest:
            coll = self.svc.nft.collection(rest[0].lower())
            key = "pinned" if "pin" in cmd else "banned"
            self._set_list(key, coll.slug, not cmd.startswith("un"))
            if cmd == "ban":
                self.engine.lot_manager.set_nft_sell(coll.slug, False)
            return f"{coll.en}: {cmd} — применится при следующем подборе (assort scan)"
        if cmd == "max" and rest and rest[0].isdigit():
            a = dict(self.svc.scfg.get("assortment") or {})
            a["max_lots"] = int(rest[0])
            if self._save is not None:
                self._save({"giftshop": {"assortment": a}}, rebuild=False)
            self.svc.cfg.giftshop["assortment"] = a
            return f"NFT-лотов не больше {a['max_lots']}"
        return "assort [scan | pin <slug> | unpin <slug> | ban <slug> | unban <slug> | max N]"

    async def tick(self) -> None:
        if self.enabled and self.due() and not self.engine.paused and getattr(self.engine, "funpay_on", True):
            try:
                await self.scan()
            except Exception as e:  # noqa: BLE001
                log.warning("автоподбор NFT: %s", e)


def nft_lot_count(svc) -> int:
    return len([l for l in svc.nft_lots if lot_kind(l) == "nft" and l.get("sell", True)])
