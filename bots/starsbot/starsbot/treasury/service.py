"""Казначейство: один взгляд на все деньги ботов и автоматическое пополнение кошелька StarsBot.

Цепочка денег (схема из документа «Замыкание денежных кругов»):

    вы: P2P-покупка USDT раз в 1–2 недели  →  Bybit (запас USDT)
      → покупка TON (лимитный IOC-ордер, не дороже ask + max_slippage_pct)
      → перевод TON со счёта UNIFIED на FUND → вывод TON на хаб (или сразу на кошелёк бота)
      → хаб пополняет рабочий кошелёк StarsBot, когда в нём меньше чем на refill_below_hours
      → Fragment → звёзды покупателям и на аккаунт подарков

Правила безопасности:
  * за один tick выполняется не больше одного шага; пока шаг «в пути» — новых нет;
  * шаг, исход которого неизвестен (сеть оборвалась после отправки), не повторяется — ждёт вашей проверки;
  * дневные лимиты на покупку USDT и вывод TON; вывод — только на адреса кошелька бота и хаба;
  * в тестовом режиме (dry-run) ничего не двигается: страница показывает, что было бы сделано.

Маршрутизатор вывода не выводит деньги сам — он подсказывает, КОГДА и ОТКУДА выводить: крупными суммами, не чаще
одного вывода на карту за cooldown_hours и не больше max_per_day в сутки, начиная с самого дешёвого источника.
"""
from __future__ import annotations

import logging
import math
import time
from datetime import datetime

from starsbot.core.models import TonMessage
from starsbot.treasury.bybit import BybitError, round_down
from starsbot.ton.wallet import TonError, to_nano

log = logging.getLogger("treasury")

HOUR, DAY = 3600, 86400
IN_FLIGHT = ("sent", "unknown", "proposed")

DEFAULT_SOURCES = [
    {"key": "funpay", "name": "FunPay", "fee_pct": 6.0, "fixed_rub": 0, "min_rub": 1000, "margin_pct": 1.0,
     "method": "USDT TRC20 (6%) или карта"},
    {"key": "ggsel", "name": "GGSEL", "fee_pct": 0.0, "fixed_rub": 0, "min_rub": 1000, "margin_pct": 3.0,
     "method": "уточните в кабинете"},
    {"key": "ozon", "name": "Ozon", "fee_pct": 0.0, "fixed_rub": 0, "min_rub": 0, "margin_pct": 3.0,
     "method": "расчётный счёт"},
    {"key": "wb", "name": "Wildberries", "fee_pct": 0.0, "fixed_rub": 0, "min_rub": 0, "margin_pct": 3.0,
     "method": "расчётный счёт"},
    {"key": "avito", "name": "Avito", "fee_pct": 0.0, "fixed_rub": 0, "min_rub": 0, "margin_pct": 3.0,
     "method": "уточните в кабинете"},
]


def _r(x: float, step: int) -> int:
    return int(math.floor(max(0.0, x) / step) * step)


def _up(x: float, step: int) -> int:
    return int(math.ceil(max(0.0, x) / step) * step)


def rub(x: float | None) -> str:
    return "—" if x is None else f"{x:,.0f}".replace(",", " ")


class Treasury:
    def __init__(self, cfg, engine, *, bybit=None, hub=None):
        self.cfg = cfg
        self.engine = engine
        self.db = engine.db
        self.bybit = bybit
        self.hub = hub
        self.snap: dict = self.db.kv_get("treasury_snapshot") or {}
        self._chain: dict | None = None
        self._instrument = None

    # ------------------------------------------------------------------ настройки
    @property
    def t(self) -> dict:
        return self.cfg.get("treasury") or {}

    def sub(self, name: str) -> dict:
        return self.t.get(name) or {}

    @property
    def enabled(self) -> bool:
        return bool(self.t.get("enabled"))

    @property
    def demo(self) -> bool:
        return bool(self.cfg.general.get("demo"))

    @property
    def dry(self) -> bool:
        return bool(self.cfg.general.get("dry_run")) and not self.demo

    @property
    def bybit_on(self) -> bool:
        return bool(self.bybit is not None and self.sub("bybit").get("enabled") and getattr(self.bybit, "configured",
                                                                                           True))

    @property
    def hub_on(self) -> bool:
        return bool(self.hub is not None and self.sub("hub").get("enabled"))

    def sources(self, include_off: bool = False) -> list[dict]:
        cfg = {s.get("key"): s for s in (self.sub("router").get("sources") or [])}
        out = []
        for d in DEFAULT_SOURCES:
            s = {**d, **(cfg.get(d["key"]) or {})}
            out.append(s)
        for k, s in cfg.items():
            if k not in {d["key"] for d in DEFAULT_SOURCES}:
                out.append({"name": k, "fee_pct": 0.0, "fixed_rub": 0, "min_rub": 0, "margin_pct": 3.0, **s})
        return [s for s in out if include_off or s.get("enabled", True)]

    def usdt_rub(self) -> float:
        """Курс USDT в рублях: средний по вашим записанным P2P-покупкам за 30 дней, иначе из настроек."""
        rows = [p for p in (self.db.kv_get("treasury_p2p") or []) if p.get("ts", 0) >= time.time() - 30 * DAY]
        r, u = sum(float(p["rub"]) for p in rows), sum(float(p["usdt"]) for p in rows)
        if u > 0:
            return round(r / u, 3)
        return float(self.t.get("usdt_rub") or 86.8)

    def ton_rub(self) -> float | None:
        if self.engine.econ is not None:
            v = self.engine.econ.effective_rate()[0]
            if v:
                return float(v)
        return float(self.engine.rates.value) if self.engine.rates.value else None

    # ------------------------------------------------------------------ расход
    def burn(self) -> dict:
        """Расход TON на Fragment (покупатели + пополнение аккаунта подарков) за burn_days дней."""
        days = float(self.t.get("burn_days") or 7)
        now = time.time()
        row = self.db.one("SELECT SUM(ton_amount) t, MIN(delivered_at) f, COUNT(*) n FROM orders WHERE ton_amount>0 "
                          "AND delivered_at>=? AND COALESCE(delivery,'fragment')='fragment'", (now - days * DAY,))
        rate = self.ton_rub()
        if row and row["n"] and int(row["n"]) >= 5 and now - float(row["f"]) >= 12 * HOUR:
            span_h = max(24.0, (now - float(row["f"])) / HOUR)
            ton_h = float(row["t"]) / span_h
            src = f"по заказам за {span_h / 24:.1f} дн."
        else:
            fallback = float(self.t.get("fallback_burn_rub_day") or 60000)
            ton_h = fallback / (rate or 100.0) / 24
            src = f"оценка {rub(fallback)} ₽/сутки (мало статистики)"
        return {"ton_h": round(ton_h, 4), "ton_day": round(ton_h * 24, 2),
                "rub_day": round(ton_h * 24 * rate) if rate else None, "source": src}

    # ------------------------------------------------------------------ снимок балансов
    async def refresh(self) -> dict:
        e = self.engine
        s: dict = {"ts": time.time(), "errors": []}
        rate = self.ton_rub()
        s["ton_rub"], s["usdt_rub"] = rate, self.usdt_rub()
        try:
            s["working"] = {"ton": float(await e.wallet.balance_ton()),
                            "address": getattr(e.wallet, "friendly_address", "")}
        except Exception as ex:  # noqa: BLE001
            s["working"] = {"ton": float((e.wallet_info or {}).get("balance") or 0),
                            "address": getattr(e.wallet, "friendly_address", "")}
            s["errors"].append(f"кошелёк бота: {ex}")
        s["hub"] = {"enabled": self.hub_on, "ton": 0.0, "address": getattr(self.hub, "friendly_address", "")}
        if self.hub_on:
            try:
                s["hub"]["ton"] = float(await self.hub.balance_ton())
            except Exception as ex:  # noqa: BLE001
                s["errors"].append(f"хаб: {ex}")
        s["bybit"] = {"enabled": self.bybit_on}
        if self.bybit_on:
            try:
                b = await self.bybit.balances(("USDT", "TON"))
                tk = await self.bybit.ticker(self.sub("bybit").get("symbol") or "TONUSDT")
                s["bybit"].update(usdt_unified=b.get("UNIFIED", "USDT"), usdt_fund=b.get("FUND", "USDT"),
                                  ton_unified=b.get("UNIFIED", "TON"), ton_fund=b.get("FUND", "TON"),
                                  usdt=b.total("USDT"), ton=b.total("TON"), bid=tk.bid, ask=tk.ask, last=tk.last)
                s["ton_usdt"] = tk.last
            except BybitError as ex:
                s["errors"].append(f"Bybit: {ex}")
        s["gift_stars"] = getattr(getattr(e, "gifts", None), "balance", None)
        await self._refresh_sources()
        s["sources"] = [self._source_view(src) for src in self.sources()]
        s["burn"] = b = self.burn()
        ton_h = b["ton_h"] or 1e-9
        usdt_as_ton = 0.0
        if s["bybit"].get("usdt") is not None and s["bybit"].get("last"):
            usdt_as_ton = s["bybit"]["usdt"] / s["bybit"]["last"]
        parts = {"working": s["working"]["ton"], "hub": s["hub"]["ton"],
                 "bybit": float(s["bybit"].get("ton") or 0) + usdt_as_ton}
        s["runway_h"] = {k: round(v / ton_h, 1) for k, v in parts.items()}
        s["runway_h"]["total"] = round(sum(parts.values()) / ton_h, 1)
        s["reserve_rub"] = round(sum(parts.values()) * rate) if rate else None
        s["plan"] = self.snap.get("plan") or ""
        self.snap = s
        self.db.kv_set("treasury_snapshot", s)
        return s

    async def _refresh_sources(self) -> None:
        """FunPay — со страницы баланса раз в 30 минут; площадки с методом balance_rub() — у канала; остальные —
        как вы ввели в панели."""
        e = self.engine
        ap = getattr(e, "autopilot", None)
        if ap is not None and time.time() - float(ap.state.get("funpay_balance_at") or 0) > 30 * 60 and \
                not self.demo:
            try:
                info = await e.fp.get_balance()
                ap._save_state(funpay_balance=info.available_rub, funpay_balance_at=time.time(),
                               withdraw_methods=info.methods[:30])
            except Exception as ex:  # noqa: BLE001
                log.debug("баланс FunPay: %s", ex)
        for key, ch in (getattr(e, "channels", {}) or {}).items():
            fn = getattr(ch, "balance_rub", None)
            if fn is None:
                continue
            try:
                v = await fn()
                if v is not None:
                    self.db.kv_set(f"treasury_src:{key}", {"rub": float(v), "ts": time.time(), "auto": True})
            except Exception as ex:  # noqa: BLE001
                log.debug("баланс %s: %s", key, ex)

    def _source_view(self, src: dict) -> dict:
        key = src["key"]
        if key == "funpay":
            ap = getattr(self.engine, "autopilot", None)
            st = ap.state if ap is not None else {}
            bal, ts = st.get("funpay_balance"), st.get("funpay_balance_at")
        else:
            kv = self.db.kv_get(f"treasury_src:{key}") or {}
            bal, ts = kv.get("rub"), kv.get("ts")
        return {**src, "balance": float(bal) if bal is not None else None, "at": ts}

    def set_source_balance(self, key: str, value: float) -> str:
        self.db.kv_set(f"treasury_src:{key}", {"rub": float(value), "ts": time.time(), "auto": False})
        return f"Баланс {key}: {rub(value)} ₽"

    def record_p2p(self, rub_amount: float, usdt: float) -> str:
        if rub_amount <= 0 or usdt <= 0:
            return "Укажите рубли и USDT"
        rows = (self.db.kv_get("treasury_p2p") or [])[-49:]
        rows.append({"ts": time.time(), "rub": float(rub_amount), "usdt": float(usdt)})
        self.db.kv_set("treasury_p2p", rows)
        self.db.exec("UPDATE treasury_advice SET state='done', updated_at=? WHERE kind='p2p' AND state='new'",
                     (time.time(),))
        return f"Записано: {rub(rub_amount)} ₽ → {usdt:g} USDT ({rub_amount / usdt:.2f} ₽/USDT)"

    async def _notify_once(self, level: str, code: str, text: str, hours: float = 6.0, **kw) -> None:
        """Повторяющиеся предупреждения (нет USDT, лимит, неизвестный шаг) — не чаще раза в hours часов."""
        sent = self.db.kv_get("treasury_notified") or {}
        if time.time() - float(sent.get(code) or 0) < hours * HOUR:
            return
        sent[code] = time.time()
        self.db.kv_set("treasury_notified", sent)
        await self.engine.notifier.notify(level, code, text, throttle_key=code, **kw)

    # ------------------------------------------------------------------ главный цикл
    async def tick(self) -> None:
        if not self.enabled:
            return
        await self.refresh()
        self._expire()
        await self.advance()
        if not self.engine.paused:
            await self.plan()
        await self.advise()
        await self.daily_report()

    def _expire(self) -> None:
        now = time.time()
        self.db.exec("UPDATE treasury_moves SET state='cancelled', detail='не подтверждено за 3 ч', updated_at=? "
                     "WHERE state='proposed' AND ts<?", (now, now - 3 * HOUR))
        self.db.exec("UPDATE treasury_advice SET state='expired', updated_at=? WHERE state='new' AND ts<?",
                     (now, now - DAY))

    # ------------------------------------------------------------------ шаги (moves)
    def moves(self, limit: int = 40) -> list:
        return self.db.q("SELECT * FROM treasury_moves ORDER BY id DESC LIMIT ?", (limit,))

    def _new_move(self, kind: str, amount: float, unit: str, src: str, dst: str, state: str, detail: str = "",
                  price: float = 0.0) -> int:
        now = time.time()
        cur = self.db.exec("INSERT INTO treasury_moves(ts,kind,amount,unit,src,dst,state,ext_id,price,rub,detail,"
                           "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                           (now, kind, float(amount), unit, src, dst, state, "", float(price), 0.0, detail, now))
        return int(cur.lastrowid)

    def _upd(self, mid: int, **kw) -> None:
        kw["updated_at"] = time.time()
        sets = ", ".join(f"{k}=?" for k in kw)
        self.db.exec(f"UPDATE treasury_moves SET {sets} WHERE id=?", (*kw.values(), mid))

    def in_flight(self) -> list:
        return self.db.q(f"SELECT * FROM treasury_moves WHERE state IN ({','.join('?' * len(IN_FLIGHT))}) ORDER BY id",
                         IN_FLIGHT)

    async def advance(self) -> None:
        for m in self.in_flight():
            try:
                if m["state"] == "sent":
                    await self._poll(m)
                elif m["state"] == "unknown" and m["kind"] == "hub":
                    await self._poll(m, unknown=True)
            except BybitError as ex:
                log.warning("treasury: проверка шага #%s: %s", m["id"], ex)
            except Exception as ex:  # noqa: BLE001
                log.warning("treasury: проверка шага #%s: %s", m["id"], ex)
        unknown = [m for m in self.in_flight() if m["state"] == "unknown"]
        if unknown:
            await self._notify_once(
                "CRITICAL", "treasury_unknown", f"Казначейство: исход шага #{unknown[0]['id']} ({unknown[0]['kind']}, "
                f"{unknown[0]['amount']:g} {unknown[0]['unit']}) неизвестен. Автоматика остановлена до проверки: "
                "посмотрите историю на бирже/в кошельке и отметьте результат на странице «Казначейство».", hours=2)

    async def _poll(self, m, unknown: bool = False) -> None:
        kind, mid = m["kind"], int(m["id"])
        if kind == "buy":
            st = await self.bybit.order(m["ext_id"], self.sub("bybit").get("symbol") or "TONUSDT")
            if st.final:
                net = max(0.0, st.qty - (st.fee if st.fee < st.qty else 0))
                cost_rub = st.qty * st.avg_price * self.usdt_rub()
                self._pool_add(net, cost_rub)
                self._upd(mid, state="done" if st.qty > 0 else "failed", amount=net, price=st.avg_price,
                          rub=round(cost_rub, 2), detail=f"{st.status}: {st.qty:g} TON по {st.avg_price:g} USDT")
        elif kind == "withdraw":
            st = await self.bybit.withdraw_state(m["ext_id"])
            if st.done:
                got = float(m["amount"]) - float(st.fee or 0)
                self._pool_fee(float(st.fee or 0))
                if m["dst"] == "working":
                    self._land(got, f"Bybit → кошелёк бота, вывод #{m['ext_id']}")
                self._upd(mid, state="done", detail=f"дошло {got:g} TON, комиссия {st.fee:g}, tx {st.tx_id[:16]}")
            elif st.failed:
                self._upd(mid, state="failed", detail=f"Bybit: {st.status}")
                await self.engine.notifier.notify("WARNING", "treasury_failed", f"Вывод TON с Bybit #{mid} отклонён: "
                                                  f"{st.status}", throttle_key=f"treasury_failed:{mid}")
        elif kind == "hub":
            dest = self.snap.get("working", {}).get("address") or self.engine.wallet.friendly_address
            tx = await self.hub.find_outgoing(dest, to_nano(float(m["amount"])), float(m["ts"]))
            if tx:
                fee = float(tx.get("fee") or 0) / 1e9
                self._pool_fee(fee)
                self._land(float(m["amount"]), "хаб → кошелёк бота")
                self._upd(mid, state="done", ext_id=str(tx.get("hash") or "")[:80],
                          detail=f"подтверждено в сети, комиссия {fee:.4f} TON")
            elif not unknown and time.time() - float(m["ts"]) > 15 * 60:
                self._upd(mid, state="unknown", detail="перевод не найден в сети за 15 мин")
            elif unknown and time.time() - float(m["ts"]) > 6 * HOUR:
                pass        # ждём ручной отметки

    # ------------------------------------------------------------------ себестоимость TON «в пути»
    def _pool(self) -> dict:
        return self.db.kv_get("treasury_pool") or {"ton": 0.0, "rub": 0.0}

    def _pool_add(self, ton: float, rub_cost: float) -> None:
        p = self._pool()
        self.db.kv_set("treasury_pool", {"ton": p["ton"] + ton, "rub": p["rub"] + rub_cost})

    def _pool_fee(self, ton_fee: float) -> None:
        p = self._pool()            # комиссия сети/вывода: TON меньше, рубли те же — оставшиеся TON чуть дороже
        self.db.kv_set("treasury_pool", {"ton": max(0.0, p["ton"] - ton_fee), "rub": p["rub"]})

    def _land(self, ton: float, note: str) -> None:
        """TON пришли на рабочий кошелёк: записываем пополнение в журнал «Деньги» по себестоимости, чтобы сверка
        баланса и курс TON для цен оставались честными."""
        p = self._pool()
        if p["ton"] > 1e-9:
            take = min(ton, p["ton"])
            rub_part = p["rub"] * take / p["ton"]
            self.db.kv_set("treasury_pool", {"ton": p["ton"] - take, "rub": p["rub"] - rub_part})
            rub_part += (ton - take) * (self.ton_rub() or 0)
        else:
            rub_part = ton * (self.ton_rub() or 0)
        if self.engine.econ is not None:
            self.engine.econ.add_cashflow("topup", round(rub_part, 2), round(ton, 6), f"казначейство: {note}")

    # ------------------------------------------------------------------ план пополнения
    async def plan(self) -> str:
        if self.in_flight():
            return ""
        s = self.snap
        b = s.get("burn") or self.burn()
        ton_h = float(b["ton_h"] or 0)
        if ton_h <= 0:
            return ""
        w = self.sub("working")
        work = float(s.get("working", {}).get("ton") or 0)
        low, target = ton_h * float(w.get("refill_below_hours") or 8), ton_h * float(w.get("refill_to_hours") or 24)
        text = ""
        if work < low:
            need = target - work
            hub_ton = float(s.get("hub", {}).get("ton") or 0)
            if self.hub_on and hub_ton - 0.2 >= min(need, 1.0):
                amt = round(min(need, hub_ton - 0.2), 2)
                text = await self._do("hub", amt, "TON", "hub", "working",
                                      f"кошелёк бота {work:.1f} TON < {low:.1f} (запас {w.get('refill_below_hours')} ч)")
            elif self.bybit_on and not self.hub_on:
                text = await self._bybit_route("working", need)
            elif self.bybit_on and self.hub_on:
                hcfg = self.sub("hub")
                text = await self._bybit_route("hub", need + ton_h * float(hcfg.get("keep_hours") or 48) - hub_ton)
            else:
                text = f"кошелёку бота нужно {need:.1f} TON — источник не подключён"
        elif self.hub_on and self.bybit_on:
            hcfg = self.sub("hub")
            hub_ton = float(s.get("hub", {}).get("ton") or 0)
            if hub_ton < ton_h * float(hcfg.get("refill_below_hours") or 12):
                text = await self._bybit_route("hub", ton_h * float(hcfg.get("keep_hours") or 48) - hub_ton)
        if text:
            self.snap["plan"] = text
            self.db.kv_set("treasury_snapshot", self.snap)
        return text

    def _today(self, kind: str, col: str = "amount") -> float:
        return float(self.db.scalar(f"SELECT SUM({col}) FROM treasury_moves WHERE kind=? AND ts>=? AND state IN "
                                    "('sent','done','unknown')", (kind, time.time() - DAY), 0) or 0)

    def _addr(self, dst: str) -> str:
        if dst == "hub":
            return str(getattr(self.hub, "friendly_address", "") or "")
        return str(getattr(self.engine.wallet, "friendly_address", "") or "")

    async def _bybit_route(self, dst: str, need_ton: float) -> str:
        """Следующий шаг цепочки Bybit → dst, чтобы туда пришло need_ton TON."""
        bc = self.sub("bybit")
        s = self.snap.get("bybit") or {}
        if "usdt" not in s:
            return "Bybit: нет данных о балансе"
        if self._chain is None:
            self._chain = await self.bybit.coin_chain("TON", bc.get("withdraw_chain") or "TON")
        fee, chain_min = float(self._chain["withdraw_fee"]), float(self._chain["withdraw_min"])
        if not self._chain.get("can_withdraw", True):
            return "Bybit: вывод TON сейчас приостановлен биржей"
        minw = max(float(bc.get("min_withdraw_ton") or 20), chain_min)
        want = max(need_ton, minw) + fee
        left = float(bc.get("max_withdraw_ton_day") or 3000) - self._today("withdraw")
        if left < minw + fee:
            await self._notify_once("WARNING", "treasury_limit", f"Казначейство: дневной лимит вывода TON с "
                                              f"Bybit исчерпан ({bc.get('max_withdraw_ton_day')} TON). Поднимите "
                                              "лимит в настройках, если оборот вырос.")
            return "дневной лимит вывода исчерпан"
        want = min(want, left)
        fund, uni = float(s.get("ton_fund") or 0), float(s.get("ton_unified") or 0)
        have = fund + uni
        if have >= want * 0.98:
            return await self._send_ton(dst, want, fund, uni, minw + fee)
        # TON не хватает — покупаем на USDT
        qty_need = want - have
        ask = float(s.get("ask") or s.get("last") or 0)
        if ask <= 0:
            return "Bybit: нет цены TON"
        slip = float(bc.get("max_slippage_pct") or 0.5) / 100
        limit = ask * (1 + slip)
        usdt_need = qty_need * limit * 1.002
        uni_usdt, fund_usdt = float(s.get("usdt_unified") or 0), float(s.get("usdt_fund") or 0)
        keep = float(bc.get("keep_usdt") or 0)
        if uni_usdt - keep < usdt_need and fund_usdt >= 1:
            amt = float(round_down(min(fund_usdt, usdt_need - max(0.0, uni_usdt - keep) + 1), "0.01"))
            return await self._do("xfer", amt, "USDT", "FUND", "UNIFIED", "USDT на торговый счёт для покупки TON")
        spent_today = self._today("buy", "amount*price")
        spend = min(usdt_need, uni_usdt - keep, float(bc.get("max_buy_usdt_day") or 5000) - spent_today)
        if self._instrument is None:
            self._instrument = await self.bybit.instrument(bc.get("symbol") or "TONUSDT")
        ins = self._instrument
        qty = float(round_down(spend / limit, ins.base_precision)) if spend > 0 else 0.0
        if qty > 0 and qty >= ins.min_order_qty and qty * ask >= max(ins.min_order_amt, 1):
            return await self._do("buy", qty, "TON", "USDT", "TON", f"покупка до {limit:.4f} USDT/TON",
                                  price=float(round_down(limit, ins.tick_size)))
        # купить больше нечем — отправляем то, что есть, если это хотя бы одна партия
        await self._notify_once("WARNING", "treasury_no_usdt", f"Казначейство: на Bybit мало USDT "
                                          f"({uni_usdt + fund_usdt:.2f}) — докупить TON нечем. Купите USDT на P2P "
                                          "(сумму подскажет советник).")
        if have >= minw + fee:
            return await self._send_ton(dst, have, fund, uni, minw + fee)
        return "мало USDT на Bybit"

    async def _send_ton(self, dst: str, want: float, fund: float, uni: float, min_batch: float) -> str:
        """TON на Bybit есть: сначала перевести с торгового счёта на счёт финансирования, потом вывести."""
        if fund >= min_batch and fund >= want * 0.98:
            amt = float(round_down(min(fund, want), "0.01"))
            return await self._do("withdraw", amt, "TON", "bybit", dst, f"вывод на {dst}")
        if uni >= 0.01:
            amt = float(round_down(uni, "0.01"))
            return await self._do("xfer", amt, "TON", "UNIFIED", "FUND", "TON на счёт финансирования для вывода")
        amt = float(round_down(fund, "0.01"))
        return await self._do("withdraw", amt, "TON", "bybit", dst, f"вывод на {dst}")

    async def _do(self, kind: str, amount: float, unit: str, src: str, dst: str, why: str, price: float = 0.0) -> str:
        label = {"hub": "Хаб → кошелёк бота", "withdraw": f"Вывод TON с Bybit → {dst}", "xfer": f"Bybit {src} → {dst}",
                 "buy": "Покупка TON на Bybit"}[kind]
        text = f"{label}: {amount:g} {unit} ({why})"
        if self.dry:
            return "[тестовый режим] " + text
        if not self.t.get("auto", True):
            mid = self._new_move(kind, amount, unit, src, dst, "proposed", why, price)
            await self.engine.notifier.notify("WARNING", "treasury_proposed", f"🏦 {text}. Выполнить?",
                                              throttle_key=f"treasury_proposed:{mid}",
                                              buttons=[("✔ Выполнить", f"treasury_ok {mid}"),
                                                       ("✖ Отмена", f"treasury_no {mid}")])
            return f"ждёт подтверждения: {text}"
        mid = self._new_move(kind, amount, unit, src, dst, "sent", why, price)
        return await self._execute(mid)

    async def _execute(self, mid: int) -> str:
        m = self.db.one("SELECT * FROM treasury_moves WHERE id=?", (mid,))
        kind, amount = m["kind"], float(m["amount"])
        bc = self.sub("bybit")
        try:
            if kind == "buy":
                oid = await self.bybit.buy_ton(round_down(amount, (self._instrument.base_precision if self._instrument
                                                                   else "0.01")),
                                               format(float(m["price"]), "f"), bc.get("symbol") or "TONUSDT",
                                               link_id=f"tr{mid}")
                self._upd(mid, ext_id=oid)
                await self._poll(self.db.one("SELECT * FROM treasury_moves WHERE id=?", (mid,)))
            elif kind == "xfer":
                tid = await self.bybit.transfer(m["unit"], round_down(amount, "0.01"), m["src"], m["dst"])
                self._upd(mid, ext_id=tid, state="done")
            elif kind == "withdraw":
                addr = self._addr(m["dst"])
                if not addr or addr == "—":
                    raise BybitError("адрес получателя не известен", "address")
                wid = await self.bybit.withdraw("TON", bc.get("withdraw_chain") or "TON", addr,
                                                round_down(amount, "0.01"))
                self._upd(mid, ext_id=wid, detail=f"{m['detail']} · {addr[:10]}…")
            elif kind == "hub":
                dest = self._addr("working")
                sent = await self.hub.transfer([TonMessage(address=dest, amount=to_nano(amount))],
                                               int(time.time()) + 300)
                self._upd(mid, ext_id=sent.msg_hash[:80])
        except BybitError as ex:
            self._upd(mid, state="unknown" if ex.sent else "failed", detail=str(ex)[:300])
            if not ex.sent:
                await self.engine.notifier.notify("WARNING", "treasury_failed", f"Казначейство: шаг #{mid} ({kind}) "
                                                  f"не выполнен: {ex}", throttle_key=f"treasury_failed:{kind}")
            return f"ошибка: {ex}"
        except TonError as ex:
            self._upd(mid, state="failed" if ex.code == "insufficient_funds" else "unknown", detail=str(ex)[:300])
            return f"ошибка: {ex}"
        except Exception as ex:  # noqa: BLE001 — сеть оборвалась после подписи: не повторяем
            self._upd(mid, state="unknown", detail=str(ex)[:300])
            return f"ошибка: {ex}"
        log.info("treasury #%s %s %.4f %s → %s", mid, kind, amount, m["unit"], m["dst"])
        return f"#{mid} {kind}: {amount:g} {m['unit']}"

    async def approve(self, mid: int) -> str:
        m = self.db.one("SELECT * FROM treasury_moves WHERE id=?", (mid,))
        if m is None or m["state"] != "proposed":
            return "шаг не найден или уже обработан"
        if self.dry:
            return "тестовый режим: деньги не двигаются"
        self._upd(mid, state="sent")
        return await self._execute(mid)

    def reject(self, mid: int) -> str:
        self.db.exec("UPDATE treasury_moves SET state='cancelled', updated_at=? WHERE id=? AND state='proposed'",
                     (time.time(), mid))
        return f"шаг #{mid} отменён"

    def resolve(self, mid: int, ok: bool) -> str:
        """Ручная отметка шага с неизвестным исходом после проверки на бирже/в кошельке."""
        m = self.db.one("SELECT * FROM treasury_moves WHERE id=?", (mid,))
        if m is None or m["state"] not in ("unknown", "sent"):
            return "шаг не найден или уже закрыт"
        if ok and m["kind"] in ("hub", "withdraw") and m["dst"] == "working":
            self._land(float(m["amount"]), f"ручная отметка шага #{mid}")
        self._upd(mid, state="done" if ok else "failed", detail=(m["detail"] or "") + " · отмечено вручную")
        return f"шаг #{mid} отмечен {'выполненным' if ok else 'несостоявшимся'}"

    # ------------------------------------------------------------------ маршрутизатор: когда и откуда выводить
    def advices(self, limit: int = 30) -> list:
        return self.db.q("SELECT * FROM treasury_advice ORDER BY id DESC LIMIT ?", (limit,))

    def _advice(self, kind: str, source: str, amount: float, text: str) -> int:
        now = time.time()
        cur = self.db.exec("INSERT INTO treasury_advice(ts,kind,source,amount_rub,text,state,updated_at) "
                           "VALUES(?,?,?,?,?,?,?)", (now, kind, source, float(amount), text, "new", now))
        return int(cur.lastrowid)

    def _last_advice(self, kind: str) -> float:
        return float(self.db.scalar("SELECT MAX(ts) FROM treasury_advice WHERE kind=? AND state IN ('new','done')",
                                    (kind,), 0) or 0)

    def withdraw_plan(self, need_rub: float = 0.0) -> list[dict]:
        """Откуда выводить: самые дешёвые источники с балансом не меньше крупной партии. Если need_rub > 0 — набираем
        эту сумму для покупки USDT только из источников, где вывод дешевле маржи бота."""
        r = self.sub("router")
        batch = float(r.get("min_batch_rub") or 20000)
        card_max = float(r.get("max_batch_rub") or 150000)
        out = []
        left = need_rub
        for src in sorted(self.snap.get("sources") or [], key=lambda x: (float(x.get("fee_pct") or 0),
                                                                          -(x.get("balance") or 0))):
            bal = float(src.get("balance") or 0) - float(r.get("keep_rub") or 0)
            if bal < max(batch, float(src.get("min_rub") or 0)):
                continue
            fee_pct = float(src.get("fee_pct") or 0)
            cheap = fee_pct <= float(src.get("margin_pct") or 3)
            if need_rub > 0:
                if not cheap or left <= 0:
                    continue
                amount = min(bal, card_max, max(left, batch))
                left -= amount
            else:
                amount = min(bal, card_max)
            amount = _r(amount, 1000)
            if amount < batch:
                continue
            cost = amount * fee_pct / 100 + float(src.get("fixed_rub") or 0)
            out.append({"key": src["key"], "name": src["name"], "amount": amount, "fee_pct": fee_pct,
                        "cost": round(cost), "cheap": cheap, "method": src.get("method") or ""})
        return out

    def card_ok(self) -> tuple[bool, str]:
        r = self.sub("router")
        last = self._last_advice("withdraw")
        cd = float(r.get("cooldown_hours") or 24)
        if last and time.time() - last < cd * HOUR:
            return False, f"следующий вывод на карту не раньше {datetime.fromtimestamp(last + cd * HOUR):%d.%m %H:%M}"
        n = int(self.db.scalar("SELECT COUNT(*) FROM treasury_advice WHERE kind='withdraw' AND state IN ('new','done') "
                               "AND ts>=?", (time.time() - DAY,), 0) or 0)
        if n >= int(r.get("max_per_day") or 1):
            return False, "дневной лимит выводов на карту"
        return True, ""

    async def advise(self) -> list[str]:
        s = self.snap
        r = self.sub("router")
        if not r.get("enabled", True) or not s:
            return []
        sent: list[str] = []
        b = s.get("burn") or {}
        rub_day = float(b.get("rub_day") or 0)
        days = float((s.get("runway_h") or {}).get("total") or 0) / 24
        p2p = self.sub("p2p")
        card_ok, card_why = self.card_ok()
        # 1) запаса мало — купить USDT на P2P одной крупной покупкой
        trigger, target = float(p2p.get("trigger_days") or 1.5), float(p2p.get("target_days") or 3)
        if rub_day > 0 and days < trigger and time.time() - self._last_advice("p2p") > \
                float(p2p.get("cooldown_hours") or 20) * HOUR:
            amount = max(float(p2p.get("min_buy_rub") or 25000), _up((target - days) * rub_day, 5000))
            usdt = amount / self.usdt_rub()
            lines = [f"🏦 Запаса денег на {days:.1f} дн. (цель {target:g} дн., расход ≈ {rub(rub_day)} ₽/сутки).",
                     f"Купите USDT на P2P ≈ на {rub(amount)} ₽ (≈ {usdt:,.0f} USDT по {self.usdt_rub():g} ₽) и "
                     "переведите на Bybit (счёт финансирования) — дальше бот всё сделает сам."]
            plan = self.withdraw_plan(amount) if card_ok else []
            if plan:
                for p in plan:
                    lines.append(f"Рубли: выведите {rub(p['amount'])} ₽ с {p['name']} ({p['method']}, комиссия "
                                 f"{p['fee_pct']:g}% ≈ {rub(p['cost'])} ₽).")
            else:
                lines.append("Дешёвого источника рублей сейчас нет" + (f" ({card_why})" if card_why else "") +
                             " — используйте деньги на карте.")
            aid = self._advice("p2p", "", amount, "\n".join(lines))
            for p in plan:
                self._advice("withdraw", p["key"], p["amount"], f"для P2P: {p['name']} {rub(p['amount'])} ₽")
            await self.engine.notifier.notify("WARNING", "treasury_p2p", "\n".join(lines),
                                              throttle_key=f"treasury_advice:{aid}",
                                              buttons=[("✔ Купил", f"treasury_done {aid}"),
                                                       ("Позже", f"treasury_skip {aid}")])
            sent.append("\n".join(lines))
            card_ok = False
        # 2) накопилось на площадке — один крупный вывод, не чаще cooldown_hours
        if card_ok:
            plan = self.withdraw_plan()
            if plan:
                p = plan[0]
                text = (f"💸 Пора вывести {rub(p['amount'])} ₽ с {p['name']} ({p['method']}). Комиссия {p['fee_pct']:g}% "
                        f"≈ {rub(p['cost'])} ₽.")
                if not p["cheap"]:
                    text += " Это дороже маржи — выводите только прибыль, на пополнение лучше P2P."
                text += f" Следующий вывод — не раньше чем через {float(r.get('cooldown_hours') or 24):g} ч."
                aid = self._advice("withdraw", p["key"], p["amount"], text)
                await self.engine.notifier.notify("INFO", "treasury_withdraw", text,
                                                  throttle_key=f"treasury_advice:{aid}",
                                                  buttons=[("✔ Вывел", f"treasury_done {aid}"),
                                                           ("Пропустить", f"treasury_skip {aid}")])
                sent.append(text)
        return sent

    def advice_done(self, aid: int) -> str:
        a = self.db.one("SELECT * FROM treasury_advice WHERE id=?", (aid,))
        if a is None:
            return "совет не найден"
        self.db.exec("UPDATE treasury_advice SET state='done', updated_at=? WHERE id=?", (time.time(), aid))
        if a["kind"] == "withdraw" and a["source"]:
            if a["source"] == "funpay":
                if self.engine.econ is not None:
                    self.engine.econ.add_cashflow("withdraw_funpay", float(a["amount_rub"]), 0.0,
                                                  "вывод по совету казначейства")
            else:
                kv = self.db.kv_get(f"treasury_src:{a['source']}") or {}
                if kv.get("rub") is not None and not kv.get("auto"):
                    self.set_source_balance(a["source"], max(0.0, float(kv["rub"]) - float(a["amount_rub"])))
        if a["kind"] == "p2p":
            return ("Отлично. Запишите фактическую покупку (₽ и USDT) на странице «Казначейство» — по ней бот считает "
                    "себестоимость TON")
        return "Отмечено"

    def advice_skip(self, aid: int) -> str:
        self.db.exec("UPDATE treasury_advice SET state='skipped', updated_at=? WHERE id=?", (time.time(), aid))
        return "Хорошо, напомню позже"

    # ------------------------------------------------------------------ отчёт
    def summary_text(self) -> str:
        s = self.snap or {}
        rh = s.get("runway_h") or {}
        b = s.get("burn") or {}
        lines = ["🏦 Казначейство",
                 f"Кошелёк бота: {s.get('working', {}).get('ton', 0):.1f} TON (≈ {rh.get('working', 0):.0f} ч)"]
        if s.get("hub", {}).get("enabled"):
            lines.append(f"Хаб: {s['hub']['ton']:.1f} TON (≈ {rh.get('hub', 0):.0f} ч)")
        if s.get("bybit", {}).get("enabled") and "usdt" in s.get("bybit", {}):
            lines.append(f"Bybit: {s['bybit']['usdt']:.0f} USDT + {s['bybit']['ton']:.1f} TON (≈ {rh.get('bybit', 0):.0f} ч)")
        lines.append(f"Всего запас ≈ {rh.get('total', 0) / 24:.1f} дн. · расход ≈ {rub(b.get('rub_day'))} ₽/сутки "
                     f"({b.get('source', '')})")
        for src in s.get("sources") or []:
            if src.get("balance"):
                lines.append(f"{src['name']}: {rub(src['balance'])} ₽")
        done = self.db.q("SELECT kind, SUM(amount) a, unit FROM treasury_moves WHERE state='done' AND ts>=? "
                         "GROUP BY kind, unit", (time.time() - DAY,))
        if done:
            lines.append("За сутки: " + ", ".join(f"{d['kind']} {d['a']:.1f} {d['unit']}" for d in done))
        if s.get("errors"):
            lines.append("Проблемы: " + "; ".join(s["errors"]))
        return "\n".join(lines)

    async def daily_report(self) -> None:
        hour = int(self.t.get("daily_report_hour") if self.t.get("daily_report_hour") is not None else 10)
        now = datetime.now()
        key = now.strftime("%Y-%m-%d")
        if now.hour < hour or self.db.kv_get("treasury_report_day") == key:
            return
        self.db.kv_set("treasury_report_day", key)
        await self.engine.notifier.notify("INFO", "treasury_daily", self.summary_text(), throttle_key="treasury_daily")

    def status(self) -> dict:
        return {"enabled": self.enabled, "auto": bool(self.t.get("auto", True)), "dry": self.dry,
                "bybit": self.bybit_on, "hub": self.hub_on, "snap": self.snap, "pool": self._pool(),
                "card": self.card_ok(), "usdt_rub": self.usdt_rub(),
                "p2p": list(reversed((self.db.kv_get("treasury_p2p") or [])[-10:]))}
