"""Автопилот: замыкает цикл продаж без участия продавца.

  деньги с FunPay → (вывод на кошелёк TON) → покупка звёзд на Fragment / звёзды на аккаунт для подарков →
  выдача покупателю → подтверждение заказа (напоминание) → если покупатель молчит — файл для поддержки FunPay →
  деньги снова на балансе FunPay → …

Что делает каждые 5 минут:
  1. Напоминает покупателю один раз подтвердить заказ, если звёзды/подарки выданы, а подтверждения нет
     (п. 2.1.2 правил FunPay: просим только после фактической выдачи; одно напоминание — без спама).
  2. Готовит файл для поддержки FunPay по заказам, которые давно выданы и не подтверждены: номер заказа,
     что и когда выдано, доказательства (хэш транзакции TON / список подарков), переписка.
  3. Когда TON на кошельке мало, а на балансе FunPay есть деньги — создаёт заявку на вывод на кошелёк бота
     (по умолчанию каждую заявку подтверждаете вы кнопкой).
  4. Пополнение звёзд аккаунта для подарков и включение/выключение лотов делают GiftService и Engine.stock_sync.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from pathlib import Path

from starsbot.core.models import OrderStatus as S

log = logging.getLogger("autopilot")


def _dt(ts) -> str:
    return datetime.fromtimestamp(float(ts)).strftime("%d.%m.%Y %H:%M") if ts else "—"


TRON_ADDRESS = re.compile(r"T[1-9A-HJ-NP-Za-km-z]{33}")


class Autopilot:
    def __init__(self, cfg, engine, home: Path | None = None):
        self.cfg = cfg
        self.engine = engine
        self.db = engine.db
        self.home = Path(home) if home else Path(".")
        self.state: dict = self.db.kv_get("autopilot_state") or {}

    @property
    def acfg(self):
        return self.cfg.get("autopilot") or {}

    def _save_state(self, **kw) -> None:
        self.state.update(kw)
        self.db.kv_set("autopilot_state", self.state)

    async def tick(self) -> None:
        if not self.acfg.get("enabled") or self.engine.paused:
            return
        await self.confirm_reminders()
        due = self.unconfirmed(float(self.acfg.get("support_after_hours") or 72))
        if due and time.time() - float(self.state.get("support_at") or 0) > 12 * 3600:
            path = self.make_support_file(due)
            await self.engine.notifier.notify("WARNING", "support_file", f"📄 Готов файл для поддержки FunPay: {len(due)} "
                                              f"неподтверждённых заказов. Файл: {path}. Откройте тикет «Покупатель не "
                                              "подтверждает заказ» и приложите его (кнопка в панели → FunPay).",
                                              throttle_key="support_file")
        await self.maybe_withdraw()
        self._save_state(tick_at=time.time())

    # ------------------------------------------------------------------ подтверждение
    async def confirm_reminders(self) -> int:
        hours = float(self.acfg.get("confirm_reminder_hours") or 0)
        if hours <= 0:
            return 0
        n = 0
        for o in self.db.q("SELECT * FROM orders WHERE status=? AND fp_status='paid' AND confirm_reminded=0 AND "
                           "delivered_at<? AND COALESCE(channel,'funpay')='funpay'",
                           (S.DELIVERED.value, time.time() - hours * 3600)):
            await self.engine.say(o, "confirm_reminder")
            self.db.update_order(o["order_id"], confirm_reminded=1)
            self.db.add_event(o["order_id"], "confirm_reminder")
            n += 1
        return n

    def unconfirmed(self, older_than_hours: float, include_done: bool = False) -> list:
        sql = ("SELECT * FROM orders WHERE status=? AND fp_status='paid' AND delivered_at<? AND "
               "COALESCE(channel,'funpay')='funpay'")
        if not include_done:
            sql += " AND support_file IS NULL"
        return self.db.q(sql + " ORDER BY delivered_at", (S.DELIVERED.value, time.time() - older_than_hours * 3600))

    def _proof(self, o) -> list[str]:
        lines = []
        if o["delivery"] == "gift":
            rows = self.db.q("SELECT * FROM gift_sends WHERE order_id=? AND state='sent' ORDER BY seq", (o["order_id"],))
            total = sum(int(r["stars"]) for r in rows)
            value = sum(int(r["convert_stars"]) for r in rows)
            lines.append(f"Способ выдачи: подарки Telegram на аккаунт @{o['tg_username']} "
                         f"(id {str(o['tg_recipient'] or '').split(':')[0]})")
            lines.append(f"Отправлено подарков: {len(rows)} на {total} ⭐ (при конвертации {value} ⭐):")
            for r in rows:
                lines.append(f"  • подарок {r['gift_id']} — {r['stars']} ⭐, отправлен {_dt(r['updated_at'])}")
            lines.append("Подтверждение: история звёзд Telegram-аккаунта продавца (исходящие оплаты подарков).")
        else:
            if o["delivery"] == "premium":
                lines.append(f"Способ выдачи: подарок Telegram Premium на {o['premium_months']} мес. по username "
                             f"@{o['tg_username']} через официальный сервис Telegram — fragment.com")
            else:
                lines.append(f"Способ выдачи: Telegram Stars по username @{o['tg_username']} через официальный "
                             "сервис Telegram — fragment.com")
            if o["tx_hash"] and not str(o["tx_hash"]).startswith("dry"):
                lines.append(f"Транзакция оплаты на Fragment (сеть TON): {o['tx_hash']}")
                from starsbot.evidence import tonviewer_tx
                if tonviewer_tx(o["tx_hash"]):
                    lines.append(f"Проверить: {tonviewer_tx(o['tx_hash'])}")
            if o["fragment_ref"]:
                lines.append(f"Комментарий платежа Fragment: {o['fragment_ref']}")
            if o["ton_amount"]:
                lines.append(f"Сумма оплаты: {float(o['ton_amount']):.4f} TON")
        return lines

    def make_support_file(self, orders, mark: bool = True) -> str:
        folder = Path(str(self.acfg.get("support_folder") or "data/support"))
        if not folder.is_absolute():
            folder = self.home / folder
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M")
        path = folder / f"funpay_support_{stamp}.txt"
        seller = getattr(self.engine.fp, "username", "") or "—"
        ids = ", ".join(f"#{o['order_id']}" for o in orders)
        out = [
            "Заявка в поддержку FunPay: покупатели не подтверждают выполненные заказы",
            f"Продавец: {seller} · сформировано {datetime.now().strftime('%d.%m.%Y %H:%M')}",
            "",
            "Здравствуйте! Прошу подтвердить выполнение заказов ниже. Товар выдан полностью и в срок, покупателям "
            "отправлено сообщение о выдаче и просьба подтвердить заказ, но подтверждения нет. Доказательства выдачи "
            "и переписка — по каждому заказу.",
            f"Заказы: {ids}",
            "",
        ]
        for o in orders:
            out += ["=" * 72,
                    f"Заказ #{o['order_id']} — https://funpay.com/orders/{o['order_id']}/",
                    f"Покупатель: {o['buyer_username']} · оплачен {_dt(o['paid_at'])} · сумма {o['price'] or 0:g} "
                    f"{o['currency'] or '₽'}",
                    f"Товар: {o['description'] or ''} ({o['stars']} ⭐)",
                    f"Выдан: {_dt(o['delivered_at'])} · напоминание о подтверждении: "
                    f"{'отправлено' if o['confirm_reminded'] else 'нет'}"]
            out += self._proof(o)
            msgs = self.db.q("SELECT ts, direction, text FROM messages WHERE order_id=? ORDER BY ts", (o["order_id"],))
            if msgs:
                out.append("Переписка в чате заказа:")
                for m in msgs[-25:]:
                    who = "Продавец (бот)" if m["direction"] == "out" else "Покупатель"
                    text = " ".join(str(m["text"] or "").split())[:400]
                    out.append(f"  [{_dt(m['ts'])}] {who}: {text}")
            out.append("")
            if mark:
                self.db.update_order(o["order_id"], support_file=str(path))
                self.db.add_event(o["order_id"], "support_file", path=str(path))
        out += ["=" * 72, "Спасибо!", ""]
        path.write_text("\n".join(out), encoding="utf-8")
        self._save_state(support_at=time.time(), support_last=str(path))
        return str(path)

    def support_files(self) -> list[dict]:
        folder = Path(str(self.acfg.get("support_folder") or "data/support"))
        if not folder.is_absolute():
            folder = self.home / folder
        if not folder.exists():
            return []
        return [{"name": p.name, "size": p.stat().st_size, "ts": p.stat().st_mtime}
                for p in sorted(folder.glob("funpay_support_*.txt"), reverse=True)[:30]]

    def support_path(self, name: str) -> Path | None:
        folder = Path(str(self.acfg.get("support_folder") or "data/support"))
        if not folder.is_absolute():
            folder = self.home / folder
        p = (folder / Path(name).name)
        return p if p.exists() and p.name.startswith("funpay_support_") else None

    # ------------------------------------------------------------------ вывод с FunPay на кошелёк
    def withdrawn_today(self) -> float:
        return float(self.db.scalar("SELECT SUM(amount_rub) FROM withdrawals WHERE ts>=? AND state IN "
                                    "('sent','pending','approved')", (time.time() - 86400,), 0) or 0)

    async def maybe_withdraw(self, force: bool = False) -> str:
        w = self.acfg.get("withdraw") or {}
        if not w.get("enabled") and not force:
            return "вывод выключен"
        if self.db.one("SELECT id FROM withdrawals WHERE state='pending'"):
            return "есть заявка, ждущая подтверждения"
        bal = (self.engine.wallet_info or {}).get("balance")
        if not force and bal is not None and float(bal) >= float(w.get("min_wallet_ton") or 0):
            return "TON на кошельке достаточно"
        try:
            info = await self.engine.fp.get_balance()
        except Exception as e:  # noqa: BLE001
            return f"баланс FunPay недоступен: {e}"
        self._save_state(funpay_balance=info.available_rub, funpay_balance_at=time.time(),
                         withdraw_methods=info.methods[:30])
        avail = float(info.available_rub or 0)
        if avail < float(w.get("min_funpay_rub") or 0):
            return f"на FunPay {avail:g} ₽ — меньше порога"
        amount = avail - float(w.get("keep_rub") or 0)
        cap = float(w.get("max_per_day_rub") or 0)
        if cap:
            amount = min(amount, cap - self.withdrawn_today())
        if amount <= 0:
            return "дневной лимит вывода исчерпан"
        method = str(w.get("method") or "")
        if not method and info.methods:
            ton = [v for v, t in info.methods if re.search(r"\bton\b|toncoin|gram", t, re.I)]
            usdt = [v for v, t in info.methods if re.search(r"usdt", t, re.I) and re.search(r"trc|tron", t, re.I)]
            method = (ton or usdt or [""])[0]
        if not method:
            return "не выбран способ вывода (в списке FunPay нет TON — выберите способ в панели → Кошелёк и автопилот)"
        title = next((t for v, t in info.methods if v == method), method)
        is_tron = bool(re.search(r"trc|tron|usdt", f"{method} {title}", re.I))
        wallet = str(w.get("wallet") or "")
        if is_tron:
            # USDT TRC20 нельзя отправить на TON-кошелёк бота: нужен адрес TRON (депозит биржи / обменника)
            if not TRON_ADDRESS.fullmatch(wallet):
                return ("вывод в USDT TRC20: укажите адрес TRON (начинается с T, 34 символа) — депозитный адрес "
                        "биржи, где USDT обменяете на TON и отправите на кошелёк бота")
        elif not wallet:
            wallet = str(getattr(self.engine.wallet, "friendly_address", "") or "")
        if not wallet or wallet == "—":
            return "не задан кошелёк для вывода"
        now = time.time()
        cur = self.db.exec("INSERT INTO withdrawals(ts,amount_rub,method,wallet,state,detail,updated_at) "
                           "VALUES(?,?,?,?,?,?,?)", (now, round(amount, 2), method, wallet, "pending", "", now))
        wid = int(cur.lastrowid)
        if w.get("require_approval", True) and not force:
            await self.engine.notifier.notify("WARNING", "withdraw_pending", f"💸 Автопилот: вывести {amount:.2f} ₽ с "
                                              f"FunPay на {wallet[:10]}… ({method})? На кошельке {bal} TON.",
                                              buttons=[("✔ Вывести", f"withdraw_ok {wid}"),
                                                       ("✖ Отмена", f"withdraw_no {wid}")])
            return f"заявка #{wid} ждёт подтверждения"
        return await self.execute_withdraw(wid)

    async def execute_withdraw(self, wid: int) -> str:
        row = self.db.one("SELECT * FROM withdrawals WHERE id=?", (wid,))
        if row is None or row["state"] not in ("pending", "approved"):
            return "заявка не найдена или уже обработана"
        try:
            info = await self.engine.fp.get_balance()
            msg = await self.engine.fp.withdraw(info, row["method"], row["wallet"], float(row["amount_rub"]))
        except Exception as e:  # noqa: BLE001
            self.db.exec("UPDATE withdrawals SET state='failed', detail=?, updated_at=? WHERE id=?",
                         (str(e)[:300], time.time(), wid))
            await self.engine.notifier.notify("WARNING", "withdraw_failed", f"Вывод #{wid} не выполнен: {e}. "
                                              "Если FunPay просит подтверждение по почте/2FA — подтвердите вручную.")
            return f"вывод не выполнен: {e}"
        self.db.exec("UPDATE withdrawals SET state='sent', detail=?, updated_at=? WHERE id=?",
                     (str(msg)[:300], time.time(), wid))
        await self.engine.notifier.notify("INFO", "withdraw_sent", f"💸 Заявка на вывод {row['amount_rub']:g} ₽ "
                                          f"отправлена: {msg}. Когда TON придут, бот сам продолжит работу.")
        return f"вывод #{wid}: {msg}"

    def cancel_withdraw(self, wid: int) -> str:
        self.db.exec("UPDATE withdrawals SET state='cancelled', updated_at=? WHERE id=? AND state='pending'",
                     (time.time(), wid))
        return f"заявка #{wid} отменена"

    def status(self) -> dict:
        w = self.acfg.get("withdraw") or {}
        return {"enabled": bool(self.acfg.get("enabled")), "withdraw": bool(w.get("enabled")),
                "confirm_reminder_hours": self.acfg.get("confirm_reminder_hours"),
                "unconfirmed": len(self.unconfirmed(float(self.acfg.get("confirm_reminder_hours") or 24),
                                                    include_done=True)),
                "support_due": len(self.unconfirmed(float(self.acfg.get("support_after_hours") or 72))),
                **self.state}
