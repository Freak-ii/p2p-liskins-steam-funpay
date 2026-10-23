"""Аналитика работы бота + движок рекомендаций."""
from __future__ import annotations

import datetime as dt
import json
import statistics
import time
from collections import Counter

from starsbot.core.models import OrderStatus as S
from starsbot.crm.db import Database

DAY = 86400


def _median(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None and x >= 0]
    return statistics.median(xs) if xs else None


def fmt_dur(sec: float | None) -> str:
    if sec is None:
        return "—"
    if sec < 90:
        return f"{sec:.0f} с"
    if sec < 5400:
        return f"{sec / 60:.1f} мин"
    return f"{sec / 3600:.1f} ч"


class Analytics:
    def __init__(self, cfg, db: Database, notifier=None, engine=None):
        self.cfg = cfg
        self.db = db
        self.notifier = notifier
        self.engine = engine

    # ------------------------------------------------------------------ KPI
    def summary(self, days: float = 7) -> dict:
        since = time.time() - days * DAY
        rows = self.db.q("SELECT * FROM orders WHERE created_at>=?", (since,))
        delivered = [r for r in rows if r["delivered_at"]]
        completed = [r for r in rows if r["status"] == S.COMPLETED.value]
        refunded = [r for r in rows if r["status"] == S.REFUNDED.value]
        revenue = sum(r["price"] or 0 for r in rows if r["status"] != S.REFUNDED.value)
        stars_sold = sum(r["stars"] or 0 for r in delivered)
        econ = getattr(self.engine, "econ", None) if self.engine else None
        profit = None
        if econ is not None and delivered:
            flows = [econ.order_flow(r) for r in delivered if r["status"] != S.REFUNDED.value]
            if all(f.profit is not None for f in flows):
                profit = sum(f.profit for f in flows)
        ton_spent = sum(r["ton_amount"] or 0 for r in delivered)
        t_user = _median([r["username_at"] - r["paid_at"] for r in rows if r["username_at"] and r["paid_at"]])
        t_buy = _median([r["sent_at"] - r["username_at"] for r in rows if r["sent_at"] and r["username_at"]])
        t_chain = _median([r["delivered_at"] - r["sent_at"] for r in delivered if r["sent_at"]])
        t_total = _median([r["delivered_at"] - r["paid_at"] for r in delivered if r["paid_at"]])
        t_confirm = _median([r["completed_at"] - r["delivered_at"] for r in completed if r["delivered_at"]])
        n = len(rows)
        w = self.db.kv_get("wallet") or {}
        return {
            "days": days, "orders": n, "delivered": len(delivered), "completed": len(completed),
            "refunded": len(refunded), "revenue": round(revenue, 2), "stars_sold": stars_sold,
            "ton_spent": round(ton_spent, 4),
            "avg_check": round(revenue / n, 2) if n else 0, "profit": round(profit, 2) if profit is not None else None,
            "delivery_rate": round(len(delivered) / n * 100, 1) if n else 0,
            "confirm_rate": round(len(completed) / len(delivered) * 100, 1) if delivered else 0,
            "refund_rate": round(len(refunded) / n * 100, 1) if n else 0,
            "auto_rate": round(sum(1 for r in delivered if not self._had_manual(r["order_id"])) / len(delivered) * 100, 1)
            if delivered else 0,
            "t_username": t_user, "t_purchase": t_buy, "t_chain": t_chain, "t_total": t_total, "t_confirm": t_confirm,
            "active": len(self.db.active_orders()), "balance": w.get("balance"), "capacity": w.get("capacity"),
            "avg_review": self.db.scalar("SELECT AVG(review_stars) FROM orders WHERE review_stars IS NOT NULL "
                                         "AND created_at>=?", (since,), None),
        }

    def _had_manual(self, order_id: str) -> bool:
        return bool(self.db.scalar("SELECT COUNT(*) FROM order_events WHERE order_id=? AND kind IN "
                                   "('admin_approved','admin_mark_delivered','admin_take','admin_set_username')",
                                   (order_id,)))

    def funnel(self, days: float = 7) -> list[tuple[str, int]]:
        since = time.time() - days * DAY
        q = lambda cond: int(self.db.scalar(f"SELECT COUNT(*) FROM orders WHERE created_at>=? AND {cond}", (since,)))
        return [("Оплачено", q("1=1")), ("Username получен", q("username_at IS NOT NULL")),
                ("Оплачено на Fragment", q("sent_at IS NOT NULL")), ("Звёзды выданы", q("delivered_at IS NOT NULL")),
                ("Подтверждено", q("status='completed'"))]

    def failure_reasons(self, days: float = 30) -> list[tuple[str, int]]:
        since = time.time() - days * DAY
        c: Counter = Counter()
        for r in self.db.q("SELECT kind, data FROM order_events WHERE ts>=? AND kind IN ('bad_username','purchase_error',"
                           "'purchase_failed','out_of_funds','price_guard','risk_review','risk_block','tx_expired',"
                           "'tx_mismatch','seqno_used_elsewhere','username_check_deferred')", (since,)):
            d = json.loads(r["data"] or "{}")
            c[f"{r['kind']}:{d.get('fail_reason') or d.get('code') or ''}".rstrip(":")] += 1
        return c.most_common(12)

    def denominations(self, days: float = 30) -> list[dict]:
        since = time.time() - days * DAY
        return [dict(r) for r in self.db.q(
            "SELECT stars_per_unit stars, COUNT(*) orders, SUM(quantity) units, SUM(price) revenue, "
            "SUM(ton_amount) ton FROM orders WHERE created_at>=? AND status<>'refunded' GROUP BY stars_per_unit "
            "ORDER BY stars_per_unit", (since,))]

    def daily_series(self, days: int = 30) -> list[dict]:
        out = []
        today = dt.date.today()
        for i in range(days - 1, -1, -1):
            d = today - dt.timedelta(days=i)
            start = dt.datetime.combine(d, dt.time()).timestamp()
            rows = self.db.q("SELECT price, stars, status, delivered_at, ton_amount FROM orders WHERE created_at>=? AND "
                             "created_at<?", (start, start + DAY))
            out.append({"date": d.isoformat(), "orders": len(rows),
                        "revenue": round(sum(r["price"] or 0 for r in rows if r["status"] != "refunded"), 2),
                        "stars": sum(r["stars"] or 0 for r in rows if r["delivered_at"]),
                        "ton": round(sum(r["ton_amount"] or 0 for r in rows if r["delivered_at"]), 4)})
        return out

    def hourly_heatmap(self, days: int = 30) -> list[int]:
        since = time.time() - days * DAY
        hours = [0] * 24
        for r in self.db.q("SELECT created_at FROM orders WHERE created_at>=?", (since,)):
            hours[dt.datetime.fromtimestamp(r["created_at"]).hour] += 1
        return hours

    def price_series(self, stars: int | None = None, limit: int = 300) -> list[dict]:
        if stars:
            rows = self.db.q("SELECT ts, new_price, best_competitor, applied, position, floor FROM price_history "
                             "WHERE stars=? ORDER BY id DESC LIMIT ?", (stars, limit))
        else:
            rows = self.db.q("SELECT ts, new_price, best_competitor, applied, position, floor FROM price_history "
                             "ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in reversed(rows)]

    def balance_series(self, limit: int = 300) -> list[dict]:
        return [dict(r) for r in reversed(self.db.q("SELECT ts, ton, capacity_stars FROM balance_history "
                                                     "ORDER BY ts DESC LIMIT ?", (limit,)))]

    def top_customers(self, limit: int = 10) -> list[dict]:
        return [dict(r) for r in self.db.q("SELECT * FROM customers ORDER BY total_spent DESC LIMIT ?", (limit,))]

    def sales_velocity(self, days: float = 3) -> float:
        """Звёзд в сутки за последние N дней."""
        since = time.time() - days * DAY
        stars = self.db.scalar("SELECT SUM(stars) FROM orders WHERE delivered_at>=?", (since,), 0)
        return stars / days

    # ------------------------------------------------------------------ рекомендации
    def recommendations(self) -> list[tuple[str, str, str]]:
        """[(code, severity, text)] — набор правил поверх аналитики."""
        recs: list[tuple[str, str, str]] = []
        s = self.summary(7)
        w = self.db.kv_get("wallet") or {}
        cap = w.get("capacity")
        vel = self.sales_velocity(3)
        if cap is not None and vel > 0:
            days_left = max(0, cap) / vel
            if days_left < 2:
                recs.append(("restock", "CRITICAL" if days_left < 0.5 else "WARNING",
                             f"Баланса TON хватит примерно на {days_left:.1f} дн. (продаётся ~{vel:.0f} ⭐/день). "
                             "Пополните кошелёк."))
        if cap is not None and cap <= 0:
            recs.append(("wallet_empty", "CRITICAL", "Баланса кошелька не хватает даже на 50 ⭐ — лоты скрыты."))
        fs = self.db.kv_get("fragment_status") or {}
        if fs.get("session") == "expired":
            recs.append(("fragment_session", "CRITICAL", "Сессия Fragment истекла — обновите cookie в настройках."))
        if fs.get("kyc") == "required":
            recs.append(("fragment_kyc", "CRITICAL", "Fragment требует KYC — пройдите верификацию на fragment.com."))
        if s["orders"] >= 5 and s["t_username"] and s["t_username"] > 20 * 60:
            recs.append(("slow_username", "INFO", f"Покупатели присылают username в среднем через "
                         f"{fmt_dur(s['t_username'])}. Добавьте в описание лота: «укажите username при оплате»."))
        bad = sum(n for k, n in self.failure_reasons(7) if k.startswith("bad_username"))
        if s["orders"] >= 5 and bad / max(1, s["orders"]) > 0.3:
            recs.append(("bad_username_rate", "WARNING", f"{bad} ошибок username на {s['orders']} заказов. Уточните в "
                         "описании лота, где взять username (Настройки → Имя пользователя)."))
        if s["delivered"] >= 5 and s["confirm_rate"] < 60:
            recs.append(("low_confirm", "INFO", f"Подтверждают только {s['confirm_rate']}% выданных заказов — деньги "
                         "дольше «висят»."))
        if s["refund_rate"] > 10 and s["orders"] >= 5:
            recs.append(("refunds", "WARNING", f"Доля возвратов {s['refund_rate']}% — проверьте причины в CRM."))
        econ = getattr(self.engine, "econ", None) if self.engine else None
        if econ is not None:
            for f in econ.calculator():
                if f.price and f.profit is not None and f.profit < 0:
                    recs.append((f"loss_{f.stars}", "WARNING", f"Номинал {f.stars} ⭐ продаётся в убыток: цена "
                                 f"{f.price:g} ₽, себестоимость {f.cost_rub:g} ₽ (безубыточность от "
                                 f"{f.breakeven_price:g} ₽)."))
            rec = econ.reconcile()
            if rec.get("alarm"):
                recs.append(("balance_mismatch", "CRITICAL", f"Баланс кошелька расходится с учётом на {rec['diff']:+.3f} "
                             "TON — внесите пополнения в «Деньги» или проверьте кошелёк."))
        ph = self.db.q("SELECT * FROM price_history ORDER BY id DESC LIMIT 20")
        on_floor = sum(1 for r in ph if "пола" in (r["reason"] or ""))
        if on_floor >= 10:
            recs.append(("price_war", "WARNING", "Конкуренты долго демпингуют ниже вашей себестоимости. Не "
                         "опускайтесь — держите скорость выдачи; ищите TON дешевле."))
        no_sales = time.time() - float(self.db.scalar("SELECT MAX(created_at) FROM orders", (), 0) or 0)
        if self.db.scalar("SELECT COUNT(*) FROM orders") and no_sales > 2 * DAY and (cap or 0) > 0:
            recs.append(("no_sales", "WARNING", f"Нет продаж {no_sales / DAY:.1f} дн. Проверьте, активны ли лоты, их "
                         "позицию и цену; включите RTBA и автоподнятие."))
        if not self.cfg.rtba.enabled:
            recs.append(("rtba_off", "INFO", "RTBA выключен — цены не следуют за рынком."))
        if self.cfg.general.dry_run:
            recs.append(("dry_run", "INFO", "Включён dry_run — бот ничего не отправляет и не покупает. Выключите после "
                         "проверки."))
        if s["avg_review"] is not None and s["avg_review"] < 4.7:
            recs.append(("reviews", "WARNING", f"Средняя оценка {s['avg_review']:.2f}★ — изучите отзывы ниже 5★."))
        return recs

    async def tick(self) -> None:
        s = self.summary(1)
        for k in ("orders", "delivered", "revenue", "balance"):
            if s.get(k) is not None:
                self.db.metric(f"day_{k}", float(s[k]))
        active_codes = set()
        for code, sev, text in self.recommendations():
            active_codes.add(code)
            if self.db.set_recommendation(code, sev, text) and self.notifier and sev != "INFO":
                await self.notifier.notify(sev, f"rec_{code}", "💡 Рекомендация: " + text, throttle_key=f"rec_{code}")
        for r in self.db.q("SELECT code FROM recommendations WHERE active=1"):
            if r["code"] not in active_codes:
                self.db.clear_recommendation(r["code"])
        # ежедневный отчёт
        now = dt.datetime.now()
        key = f"report:{now.date().isoformat()}"
        if now.hour >= int(self.cfg.notifications.daily_report_hour) and not self.db.kv_get(key) and self.notifier:
            self.db.kv_set(key, True)
            await self.notifier.telegram("📊 " + self.text_report())

    def text_report(self) -> str:
        d, w = self.summary(1), self.summary(7)
        lines = [
            "Отчёт StarsBot",
            f"Сутки: заказов {d['orders']}, выдано {d['delivered']}, выручка {d['revenue']:g} ₽, звёзд {d['stars_sold']}",
            f"Неделя: заказов {w['orders']}, выручка {w['revenue']:g} ₽, прибыль "
            f"{w['profit'] if w['profit'] is not None else '—'} ₽, потрачено {w['ton_spent']:g} TON",
            f"Выдача: {w['delivery_rate']}% · автоматически {w['auto_rate']}% · подтверждают {w['confirm_rate']}% · "
            f"возвраты {w['refund_rate']}%",
            f"Медиана: оплата→username {fmt_dur(w['t_username'])}, username→оплата Fragment {fmt_dur(w['t_purchase'])}, "
            f"сеть {fmt_dur(w['t_chain'])}, всего {fmt_dur(w['t_total'])}",
            f"Кошелёк: {w['balance'] if w['balance'] is not None else '—'} TON (≈{w['capacity'] or '—'} ⭐) · "
            f"активных заказов: {w['active']}",
        ]
        recs = self.db.q("SELECT * FROM recommendations WHERE active=1 ORDER BY ts DESC LIMIT 5")
        if recs:
            lines.append("Рекомендации:")
            lines += [f"• {r['text']}" for r in recs]
        return "\n".join(lines)
