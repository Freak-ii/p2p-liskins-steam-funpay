"""Аналитика работы бота + движок рекомендаций."""
from __future__ import annotations

import datetime as dt
import json
import statistics
import time
from collections import Counter

from mannbot.core.models import OrderStatus as S
from mannbot.crm.db import Database

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
        keys_sold = sum(r["keys_qty"] or 0 for r in delivered)
        cost = float(self.cfg.rtba.cost_per_key or 0)
        fee = float(self.cfg.rtba.funpay_fee_pct or 0) / 100
        profit = sum((r["price"] or 0) * (1 - fee) - cost * (r["keys_qty"] or 0) for r in delivered) if cost else None
        t_link = _median([r["link_received_at"] - r["paid_at"] for r in rows if r["link_received_at"] and r["paid_at"]])
        t_offer = _median([r["offer_sent_at"] - r["link_received_at"] for r in rows
                           if r["offer_sent_at"] and r["link_received_at"]])
        t_accept = _median([r["delivered_at"] - r["offer_sent_at"] for r in delivered if r["offer_sent_at"]])
        t_total = _median([r["delivered_at"] - r["paid_at"] for r in delivered if r["paid_at"]])
        t_confirm = _median([r["completed_at"] - r["delivered_at"] for r in completed if r["delivered_at"]])
        n = len(rows)
        return {
            "days": days, "orders": n, "delivered": len(delivered), "completed": len(completed),
            "refunded": len(refunded), "revenue": round(revenue, 2), "keys_sold": keys_sold,
            "avg_check": round(revenue / n, 2) if n else 0, "profit": round(profit, 2) if profit is not None else None,
            "delivery_rate": round(len(delivered) / n * 100, 1) if n else 0,
            "confirm_rate": round(len(completed) / len(delivered) * 100, 1) if delivered else 0,
            "refund_rate": round(len(refunded) / n * 100, 1) if n else 0,
            "auto_rate": round(sum(1 for r in delivered if not self._had_manual(r["order_id"])) / len(delivered) * 100, 1)
            if delivered else 0,
            "t_link": t_link, "t_offer": t_offer, "t_accept": t_accept, "t_total": t_total, "t_confirm": t_confirm,
            "active": len(self.db.active_orders()), "stock": self.db.kv_get("last_stock"),
            "avg_review": self.db.scalar("SELECT AVG(review_stars) FROM orders WHERE review_stars IS NOT NULL "
                                         "AND created_at>=?", (since,), None),
        }

    def _had_manual(self, order_id: str) -> bool:
        return bool(self.db.scalar("SELECT COUNT(*) FROM order_events WHERE order_id=? AND kind IN "
                                   "('admin_approved','admin_resend','admin_mark_delivered','admin_take')", (order_id,)))

    def funnel(self, days: float = 7) -> list[tuple[str, int]]:
        since = time.time() - days * DAY
        q = lambda cond: int(self.db.scalar(f"SELECT COUNT(*) FROM orders WHERE created_at>=? AND {cond}", (since,)))
        return [("Оплачено", q("1=1")), ("Ссылка получена", q("link_received_at IS NOT NULL")),
                ("Трейд отправлен", q("offer_sent_at IS NOT NULL")), ("Ключи выданы", q("delivered_at IS NOT NULL")),
                ("Подтверждено", q("status='completed'"))]

    def failure_reasons(self, days: float = 30) -> list[tuple[str, int]]:
        since = time.time() - days * DAY
        c: Counter = Counter()
        for r in self.db.q("SELECT kind, data FROM order_events WHERE ts>=? AND kind IN ('bad_data','send_failed',"
                           "'offer_declined','offer_expired','offer_canceled','offer_countered','out_of_stock',"
                           "'confirm_failed','risk_review','risk_block')", (since,)):
            d = json.loads(r["data"] or "{}")
            c[f"{r['kind']}:{d.get('fail_reason') or d.get('code') or ''}".rstrip(":")] += 1
        return c.most_common(12)

    def daily_series(self, days: int = 30) -> list[dict]:
        out = []
        today = dt.date.today()
        for i in range(days - 1, -1, -1):
            d = today - dt.timedelta(days=i)
            start = dt.datetime.combine(d, dt.time()).timestamp()
            rows = self.db.q("SELECT price, keys_qty, status, delivered_at FROM orders WHERE created_at>=? AND "
                             "created_at<?", (start, start + DAY))
            out.append({"date": d.isoformat(), "orders": len(rows),
                        "revenue": round(sum(r["price"] or 0 for r in rows if r["status"] != "refunded"), 2),
                        "keys": sum(r["keys_qty"] or 0 for r in rows if r["delivered_at"])})
        return out

    def hourly_heatmap(self, days: int = 30) -> list[int]:
        since = time.time() - days * DAY
        hours = [0] * 24
        for r in self.db.q("SELECT created_at FROM orders WHERE created_at>=?", (since,)):
            hours[dt.datetime.fromtimestamp(r["created_at"]).hour] += 1
        return hours

    def price_series(self, limit: int = 300) -> list[dict]:
        rows = self.db.q("SELECT ts, new_price, best_competitor, applied, position FROM price_history "
                         "ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in reversed(rows)]

    def stock_series(self, limit: int = 300) -> list[dict]:
        return [dict(r) for r in reversed(self.db.q("SELECT ts, available FROM stock_history ORDER BY ts DESC LIMIT ?",
                                                     (limit,)))]

    def top_customers(self, limit: int = 10) -> list[dict]:
        return [dict(r) for r in self.db.q("SELECT * FROM customers ORDER BY total_spent DESC LIMIT ?", (limit,))]

    def sales_velocity(self, days: float = 3) -> float:
        """Ключей в сутки за последние N дней."""
        since = time.time() - days * DAY
        keys = self.db.scalar("SELECT SUM(keys_qty) FROM orders WHERE delivered_at>=?", (since,), 0)
        return keys / days

    # ------------------------------------------------------------------ рекомендации
    def recommendations(self) -> list[tuple[str, str, str]]:
        """[(code, severity, text)] — набор правил поверх аналитики."""
        recs: list[tuple[str, str, str]] = []
        s = self.summary(7)
        stock = self.db.kv_get("last_stock")
        vel = self.sales_velocity(3)
        if stock is not None and vel > 0:
            days_left = stock / vel
            if days_left < 2:
                recs.append(("restock", "CRITICAL" if days_left < 0.5 else "WARNING",
                             f"Ключей хватит примерно на {days_left:.1f} дн. (продаётся ~{vel:.1f}/день). Пополните склад."))
        if stock is not None and stock <= 0:
            recs.append(("stock_zero", "CRITICAL", "Склад пуст — лот скрыт, продажи стоят."))
        if s["orders"] >= 5 and s["t_link"] and s["t_link"] > 30 * 60:
            recs.append(("slow_links", "INFO", f"Покупатели присылают ссылку в среднем через {fmt_dur(s['t_link'])}. "
                         "Добавьте в описание лота: «сразу после оплаты пришлите ссылку на обмен»."))
        bad = sum(n for k, n in self.failure_reasons(7) if k.startswith("bad_data"))
        if s["orders"] >= 5 and bad / max(1, s["orders"]) > 0.3:
            recs.append(("bad_data_rate", "WARNING", f"{bad} ошибок данных на {s['orders']} заказов. Улучшите инструкцию "
                         "в описании лота (картинка «где взять ссылку на обмен», пример ссылки)."))
        holds = int(self.db.scalar("SELECT COUNT(*) FROM orders WHERE escrow_days>0 AND created_at>=?",
                                   (time.time() - 7 * DAY,)))
        if holds >= 3:
            recs.append(("trade_holds", "INFO", f"У {holds} покупателей была задержка обмена. Укажите в лоте требование "
                         "мобильного Steam Guard ≥ 7 дней."))
        if s["delivered"] >= 5 and s["confirm_rate"] < 60:
            recs.append(("low_confirm", "INFO", f"Подтверждают только {s['confirm_rate']}% выданных заказов — деньги "
                         "дольше «висят». Можно отправлять мягкое напоминание через сутки (вручную)."))
        if s["refund_rate"] > 10 and s["orders"] >= 5:
            recs.append(("refunds", "WARNING", f"Доля возвратов {s['refund_rate']}% — проверьте причины в CRM."))
        ph = self.db.q("SELECT * FROM price_history ORDER BY id DESC LIMIT 12")
        if ph:
            last = ph[0]
            if last["position"] and last["position"] > 3:
                recs.append(("price_position", "INFO", f"Наш лот на {last['position']} месте по цене. При текущем "
                             f"поле цены выше лидеров не подняться — снизьте min_margin_pct или найдите ключи дешевле."))
            on_floor = sum(1 for r in ph if "пола" in (r["reason"] or ""))
            if on_floor >= 6:
                recs.append(("price_war", "WARNING", "Конкуренты долго демпингуют ниже вашей себестоимости. "
                             "Не опускайтесь — держите сервис/скорость; пересмотрите закупочную цену."))
        no_sales = time.time() - float(self.db.scalar("SELECT MAX(created_at) FROM orders", (), 0) or 0)
        if self.db.scalar("SELECT COUNT(*) FROM orders") and no_sales > 2 * DAY and (stock or 0) > 0:
            recs.append(("no_sales", "WARNING", f"Нет продаж {no_sales / DAY:.1f} дн. Проверьте, активен ли лот, его "
                         "позицию и цену; включите RTBA и автоподнятие."))
        if not self.cfg.rtba.enabled:
            recs.append(("rtba_off", "INFO", "RTBA выключен — цена не следует за рынком. Включите rtba.enabled после "
                         "настройки cost_per_key."))
        if self.cfg.general.dry_run:
            recs.append(("dry_run", "INFO", "Включён dry_run — бот ничего не отправляет. Выключите после проверки."))
        if s["avg_review"] is not None and s["avg_review"] < 4.7:
            recs.append(("reviews", "WARNING", f"Средняя оценка {s['avg_review']:.2f}★ — изучите отзывы ниже 5★."))
        return recs

    async def tick(self) -> None:
        s = self.summary(1)
        for k in ("orders", "delivered", "revenue", "stock"):
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
            "Отчёт MannBot",
            f"Сутки: заказов {d['orders']}, выдано {d['delivered']}, выручка {d['revenue']:g}, ключей {d['keys_sold']}",
            f"Неделя: заказов {w['orders']}, выручка {w['revenue']:g}, прибыль {w['profit'] if w['profit'] is not None else '—'}",
            f"Выдача: {w['delivery_rate']}% · автоматически {w['auto_rate']}% · подтверждают {w['confirm_rate']}% · "
            f"возвраты {w['refund_rate']}%",
            f"Медиана: оплата→ссылка {fmt_dur(w['t_link'])}, ссылка→трейд {fmt_dur(w['t_offer'])}, "
            f"трейд→принят {fmt_dur(w['t_accept'])}, всего {fmt_dur(w['t_total'])}",
            f"Склад: {w['stock']} · активных заказов: {w['active']}",
        ]
        recs = self.db.q("SELECT * FROM recommendations WHERE active=1 ORDER BY ts DESC LIMIT 5")
        if recs:
            lines.append("Рекомендации:")
            lines += [f"• {r['text']}" for r in recs]
        return "\n".join(lines)
