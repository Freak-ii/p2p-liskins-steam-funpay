"""Ядро магазина: заказы покупки и выкупа, оплата, передача поставщикам экосистемы, выплаты, рефералка.

Покупка:  awaiting_payment → paid → delivering → delivered
                         ↘ expired            ↘ need_recipient (неверный @) / manual / refunded (на баланс)
Выкуп:    awaiting_asset → received → payout_pending (проверка) → paid_out
                         ↘ expired              ↘ manual
Каждое движение денег клиента — запись в ledger, клиент видит её в «Балансе».
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

from shopbot.payments import CANCELED, PAID, PaymentError, build_providers
from shopbot.loop import MoneyLoop
from shopbot.pricing import METHODS, PRODUCT_TITLES, Quote, apply_method, referral_discount, sell_quote, shop_price
from shopbot.rates import Rates
from shopbot.rtba import RTBA, PRODUCT_SUPPLIER
from shopbot.suppliers import SupplierError, build_suppliers

log = logging.getLogger("shop")
USERNAME_RE = re.compile(r"(?:^|[\s@/]|t\.me/)@?([A-Za-z][A-Za-z0-9_]{3,31})\b")
TRADE_RE = re.compile(r"https?://steamcommunity\.com/tradeoffer/new/\?partner=(\d+)&token=([\w-]{6,12})")
STATUS_TITLES = {
    "awaiting_payment": "ждёт оплаты", "paid": "оплачен", "delivering": "выдаётся", "delivered": "выполнен",
    "need_recipient": "нужен другой получатель", "manual": "у оператора", "refunded": "возврат на баланс",
    "expired": "истёк", "cancelled": "отменён", "awaiting_asset": "ждём звёзды/подарок", "received": "получено",
    "payout_pending": "выплата на проверке", "paid_out": "выплачено", "rejected": "отклонён",
}


def parse_username(text: str) -> str | None:
    m = USERNAME_RE.search((text or "").strip())
    return m.group(1) if m else None


# Ошибки данных покупателя у MannBot: ключи не ушли, нужна другая (или та же, но рабочая) ссылка на обмен
TRADE_FIX = {
    "invalid_token": "ссылка на обмен устарела — скопируйте новую",
    "bad_token": "в ссылке неверный token — скопируйте ссылку целиком",
    "no_token": "в ссылке нет token — скопируйте ссылку целиком",
    "bad_format": "это не ссылка на обмен Steam",
    "cannot_trade": "аккаунт сейчас не может обмениваться",
    "trade_banned": "у аккаунта бан обменов",
    "inventory_full": "инвентарь TF2 переполнен — освободите место",
    "inventory_private": "инвентарь скрыт — откройте его в настройках приватности Steam",
    "self": "это ссылка нашего же аккаунта",
    "trade_hold": "у аккаунта задержка обменов: включите мобильный Steam Guard (7 дней) и пришлите ссылку снова",
    "offer_declined": "обмен отклонён",
    "offer_expired": "обмен не приняли вовремя",
    "offer_countered": "обмен изменён (контрпредложения не принимаем)",
    "api_scam_suspect": "обмен отменён не нами — проверьте, не утёк ли ваш API-ключ Steam, и смените его",
}


def parse_trade_url(text: str) -> str | None:
    m = TRADE_RE.search(text or "")
    return f"https://steamcommunity.com/tradeoffer/new/?partner={m.group(1)}&token={m.group(2)}" if m else None


class Shop:
    def __init__(self, cfg, db, *, demo: bool = False):
        self.cfg = cfg
        self.db = db
        self.demo = demo
        self.rates = Rates(cfg, db, fetch=not demo)
        self.suppliers = build_suppliers(cfg, demo=demo)
        self.providers = build_providers(cfg, demo=demo)
        self.rtba = RTBA(self)
        self.loop = MoneyLoop(self)
        self.bot = None                     # shopbot.tg.bot.ShopTelegram
        self.channel = None                 # shopbot.channel.Channel
        self.miniapp = None                 # shopbot.miniapp.MiniApp (окно магазина)
        self.tasks: list[asyncio.Task] = []
        self.started_at = time.time()
        self.lock = asyncio.Lock()

    # ------------------------------------------------------------------ уведомления
    async def notify(self, level: str, code: str, text: str, buttons: list | None = None) -> None:
        self.db.alert(level, code, text)
        log.log(logging.WARNING if level != "INFO" else logging.INFO, "[%s] %s", code, text)
        if self.bot is not None:
            for chat in self.cfg.get("telegram.admin_chat_ids") or []:
                await self.bot.send(int(chat), f"🛠 {text}", buttons)

    async def tell(self, uid: int, text: str, buttons: list | None = None) -> None:
        if self.bot is not None:
            await self.bot.send(uid, text, buttons)

    # ------------------------------------------------------------------ каталог
    def gifts(self) -> list[dict]:
        return list(self.cfg.get("catalog.gifts.items") or [])

    def gift(self, gid: str) -> dict | None:
        return next((g for g in self.gifts() if g["id"] == gid), None)

    def _order_gift(self, o) -> dict | None:
        """Подарок заказа для поставщика: TG Gift выдаёт именно его (а не любой подарок той же цены)."""
        return self.gift(o["item"]) if o["product"] == "gift" else None

    def product_title(self, product: str, item: str, amount: int) -> str:
        if product == "stars":
            return f"{amount} ⭐ Telegram Stars"
        if product == "gift":
            g = self.gift(item) or {}
            return f"{g.get('emoji', '🎁')} Подарок «{g.get('title', item)}» ({g.get('stars', amount)} ⭐)"
        if product == "premium":
            return f"💎 Telegram Premium на {item} мес."
        if product == "steam_keys":
            return f"🔑 {self.cfg.get('catalog.steam_keys.title')} × {amount}"
        return PRODUCT_TITLES.get(product, product)

    def methods_for(self, product: str) -> list[str]:
        out = []
        for m in METHODS:
            if m == "stars" and product == "stars":      # звёзды за звёзды не продаём
                continue
            if self.rtba.is_open(f"pay.{m}"):
                out.append(m)
        return out

    # ------------------------------------------------------------------ покупка
    async def quote_buy(self, user, product: str, item: str, amount: int) -> Quote:
        sup = self.suppliers[PRODUCT_SUPPLIER[product]]
        months = int(item) if product == "premium" else 0
        sup_amount = amount
        if product == "gift":
            g = self.gift(item)
            if g is None:
                raise ValueError("нет такого подарка")
            sup_amount = int(g["stars"])
        q = await sup.quote("gift" if product == "gift" else product, sup_amount, months,
                            gift=self.gift(item) if product == "gift" else None)
        if not q.get("available"):
            raise SupplierError(q.get("reason") or "товар временно недоступен")
        cost = float(q["cost_rub"])
        price, _ = shop_price(self.cfg, product, cost, sup_amount if product in ("stars", "gift") else 1)
        disc = referral_discount(self.cfg, user, price, cost, product)
        return Quote(product, self.product_title(product, item, amount), sup_amount, cost, price, disc)

    def validate_amount(self, product: str, amount: int) -> str:
        if product == "stars":
            lo, hi = int(self.cfg.get("catalog.stars.min") or 50), int(self.cfg.get("catalog.stars.max") or 20000)
            if not lo <= amount <= hi:
                return f"Можно от {lo} до {hi} ⭐"
        if product == "steam_keys":
            hi = int(self.cfg.get("catalog.steam_keys.max_qty") or 50)
            if not 1 <= amount <= hi:
                return f"Можно от 1 до {hi} ключей"
        return ""

    async def create_buy(self, uid: int, product: str, item: str, amount: int, recipient: str, method: str,
                         asset: str = "USDT") -> tuple[Any, dict]:
        user = self.db.user(uid)
        err = self.validate_amount(product, amount)
        if err:
            raise ValueError(err)
        if method == "stars" and product == "stars":
            raise ValueError("звёзды нельзя оплатить звёздами")
        q = await self.quote_buy(user, product, item, amount)
        q = apply_method(self.cfg, q, method, self.rates, asset)
        chk = self.rtba.check_order(user, product, q.total_rub, method)
        if not chk.ok:
            self.rtba.log(f"buy.{product}", "order_denied", chk.reason, user=uid, total=q.total_rub)
            raise ValueError(chk.reason)
        oid = self.db.new_order_id("buy")
        ttl = float(self.cfg.get(f"payments.{method}.ttl_minutes") or self.cfg.get("payments.order_ttl_minutes")
                    or 30) * 60
        if method == "wallet" and q.asset == "USDT":
            q.pay_amount = self._unique_usdt(q.pay_amount)
            q.lines[-1] = ("К оплате", f"{q.pay_amount:g} USDT на кошелёк TON — ровно эту сумму")
        o = self.db.create_order(
            oid, user_id=uid, kind="buy", product=product, item=str(item), amount=amount, recipient=recipient,
            status="awaiting_payment", price_rub=q.price_rub, cost_rub=q.cost_rub, discount_rub=q.discount_rub,
            fee_rub=q.fee_rub, total_rub=q.total_rub, margin_rub=q.margin_rub, pay_method=method,
            pay_currency=q.asset, pay_amount=q.pay_amount, rate=q.rate, breakdown=q.lines,
            supplier=PRODUCT_SUPPLIER[product], referrer_id=user["referred_by"], expires_at=time.time() + ttl)
        if method == "balance":
            await self._pay_from_balance(o)
            return self.db.order(oid), {"id": "balance", "url": ""}
        prov = self.providers[method]
        try:
            pay = await prov.create(oid, q.total_rub, f"{q.title} · заказ {oid}", asset=q.asset,
                                    pay_amount=q.pay_amount, ttl=int(ttl))
        except PaymentError as e:
            self.db.update_order(oid, status="cancelled", fail_reason=str(e))
            await self.notify("WARNING", f"pay_{method}_error", f"#{oid}: не создан платёж ({e})")
            raise ValueError("Не получилось создать платёж, попробуйте другой способ") from e
        self.db.exec("INSERT INTO payments(id,order_id,provider,status,amount,currency,url,created_at,updated_at) "
                     "VALUES(?,?,?,?,?,?,?,?,?)", (pay["id"], oid, prov.name, "pending", q.pay_amount, q.asset,
                                                   pay.get("url", ""), time.time(), time.time()))
        asset_pay = method in ("gift", "skins")
        o = self.db.update_order(oid, payment_id=pay["id"], event="payment_created",
                                 **({"status": "awaiting_asset"} if asset_pay else {}))
        if asset_pay:
            what = "подарки Telegram" if method == "gift" else "скины Steam"
            await self.notify("INFO", f"asset_{method}", f"#{oid}: клиент {uid} оплачивает {what} на "
                              f"{q.pay_amount:g} {'⭐' if method == 'gift' else '$'} ({q.total_rub:.2f} ₽). "
                              "Проверьте поступление.",
                              [("✔ Получено", f"adm:asset:{oid}"), ("✋ Отклонить", f"adm:reject:{oid}")])
        return o, pay

    def _unique_usdt(self, amount: float) -> float:
        """USDT (TON) ищем по точной сумме: две открытые заявки не должны совпадать до цента."""
        taken = {round(float(r["pay_amount"]), 2) for r in self.db.q(
            "SELECT pay_amount FROM orders WHERE pay_method='wallet' AND pay_currency='USDT' "
            "AND status='awaiting_payment'")}
        a = round(amount, 2)
        while a in taken:
            a = round(a + 0.01, 2)
        return a

    async def confirm_asset(self, oid: str, who: str = "operator") -> str:
        """Оператор увидел подарки/скины от клиента — заказ оплачен."""
        o = self.db.order(oid)
        if o is None or o["kind"] != "buy" or o["pay_method"] not in ("gift", "skins") or \
                o["status"] not in ("awaiting_asset", "expired"):
            return "заказ не ждёт оплату активом"
        prov = self.providers.get(o["pay_method"])
        if prov is not None and hasattr(prov, "confirm"):
            prov.confirm(o["payment_id"])
        self.db.exec("UPDATE payments SET status='paid', updated_at=? WHERE id=?", (time.time(), o["payment_id"]))
        self.db.update_order(oid, event=f"asset_received_by_{who}")
        await self.on_paid(oid)
        return "оплата активом принята, выдаю"

    async def _pay_from_balance(self, o) -> None:
        u = self.db.user(o["user_id"])
        total = float(o["total_rub"])
        bonus = min(float(u["bonus_rub"] or 0), total)
        rub = round(total - bonus, 2)
        if rub > float(u["balance_rub"] or 0) + 1e-9:
            self.db.update_order(o["id"], status="cancelled", fail_reason="мало средств на балансе")
            raise ValueError(f"На балансе {float(u['balance_rub']) + float(u['bonus_rub']):.2f} ₽, нужно {total:.2f} ₽")
        if bonus:
            self.db.credit(u["id"], "bonus", -bonus, "purchase", o["id"], "оплата заказа бонусами")
        if rub:
            self.db.credit(u["id"], "rub", -rub, "purchase", o["id"], "оплата заказа с баланса")
        await self.on_paid(o["id"])

    async def on_paid(self, oid: str) -> None:
        async with self.lock:
            o = self.db.order(oid)
            if o is None or o["status"] not in ("awaiting_payment", "awaiting_asset", "expired"):
                return
            o = self.db.update_order(oid, status="paid", paid_at=time.time())
        if o["kind"] == "sell":
            if o["product"] == "stars":            # клиент продал нам звёзды — они на балансе бота (XTR)
                rps = float(self.cfg.get("payments.stars.rub_per_star") or 1.25)
                self.loop.record(o, method="stars", asset="XTR", amount=float(o["amount"]),
                                 rub=round(float(o["amount"]) * rps, 2))
            else:
                self.loop.record(o, method="gift", asset="GIFT", amount=float(o["amount"]),
                                 rub=round(float(o["amount"]) * float(self.cfg.get("payments.stars.rub_per_star")
                                                                      or 1.25), 2))
            await self.on_asset_received(oid)
            return
        self.loop.record(o)
        await self.tell(o["user_id"], f"✅ Оплата заказа <b>#{oid}</b> получена. Выдаю…")
        await self.deliver(oid)

    async def deliver(self, oid: str) -> None:
        o = self.db.order(oid)
        if self.cfg.get("general.dry_run") and not self.demo:
            self.db.update_order(oid, status="manual", fail_reason="dry_run: выдача выключена до боевого режима")
            await self.notify("WARNING", "dry_run_order", f"#{oid} оплачен, но магазин в тестовом режиме — выдайте "
                                                          "вручную или включите боевой режим")
            return
        sup = self.suppliers[o["supplier"]]
        months = int(o["item"]) if o["product"] == "premium" else 0
        try:
            res = await sup.order(oid, "gift" if o["product"] == "gift" else o["product"], o["recipient"],
                                  int(o["amount"]), months, gift=self._order_gift(o))
        except SupplierError as e:
            self.rtba.supplier_failed(o["supplier"], str(e))
            first = not self.db.scalar("SELECT COUNT(*) FROM order_events WHERE order_id=? AND kind='supplier_retry'",
                                       (oid,), 0)
            self.db.update_order(oid, event="supplier_retry", fail_reason=str(e))
            if first:                                       # одна тревога на заказ, дальше — тихие повторы
                await self.notify("WARNING", "supplier_error", f"#{oid}: поставщик {o['supplier']} не принял заказ "
                                                               f"({e}) — повторяю, при неудаче верну деньги на баланс")
            return
        if not res.get("ok"):
            self.rtba.supplier_failed(o["supplier"], str(res.get("error")))
            await self._fail(o, str(res.get("error") or "поставщик отказал"))
            return
        self.db.update_order(oid, status="delivering", supplier_ref=str(res.get("order_id") or oid),
                             supplier_status=str(res.get("status") or ""))
        await self._on_supplier_status(self.db.order(oid), res)

    async def _on_supplier_status(self, o, st: dict) -> None:
        s = st.get("status")
        oid = o["id"]
        if s != o["supplier_status"]:
            self.db.update_order(oid, supplier_status=str(s), event=f"supplier_{s}")
        if s == "delivered":
            await self.on_delivered(oid)
        elif s == "failed":
            detail = str(st.get("detail") or "")
            if o["product"] == "steam_keys" and detail in TRADE_FIX:
                self.db.update_order(oid, status="need_recipient", fail_reason=detail)
                self.db.set_session(o["user_id"], "fix_recipient", {"order": oid})
                await self.tell(o["user_id"], f"⚠️ По заказу <b>#{oid}</b> ключи не отправлены: "
                                f"{TRADE_FIX[detail]}.\nПришлите ссылку на обмен Steam ещё раз — отправлю сразу, "
                                f"деньги уже оплачены.")
            elif detail in ("not_found", "cannot_receive", "bad_format", "blacklisted") and o["product"] != "steam_keys":
                self.db.update_order(oid, status="need_recipient", fail_reason=detail)
                self.db.set_session(o["user_id"], "fix_recipient", {"order": oid})
                await self.tell(o["user_id"], f"⚠️ По заказу <b>#{oid}</b> получатель <b>@{o['recipient']}</b> не "
                                f"найден в Telegram или не может получить товар.\nПришлите правильный @username — "
                                f"выдам сразу, деньги уже оплачены.")
            else:
                self.rtba.supplier_failed(o["supplier"], detail or "failed")
                await self._fail(o, detail or "поставщик не смог выдать")
        elif s == "manual" and o["status"] != "manual":
            self.db.update_order(oid, status="manual", fail_reason=str(st.get("detail") or "ручная выдача"))
            await self.notify("WARNING", "order_manual", f"#{oid}: {o['product']} ждёт ручной выдачи → "
                                                         f"{o['recipient']}", [("✔ Выдано", f"adm:delivered:{oid}")])
            await self.tell(o["user_id"], f"🕐 Заказ <b>#{oid}</b> передан оператору, выдача обычно до 30 минут.")

    async def set_recipient(self, oid: str, recipient: str) -> None:
        o = self.db.order(oid)
        if o is None or o["status"] != "need_recipient":
            return
        new_ref = f"{oid}R{int(time.time()) % 1000}"
        self.db.update_order(oid, status="paid", recipient=recipient, event="recipient_fixed",
                             supplier_ref=new_ref, fail_reason="")
        await self.tell(o["user_id"], f"👌 Получатель @{recipient}. Выдаю…")
        sup = self.suppliers[o["supplier"]]
        try:
            res = await sup.order(new_ref, "gift" if o["product"] == "gift" else o["product"], recipient,
                                  int(o["amount"]), int(o["item"]) if o["product"] == "premium" else 0,
                                  gift=self._order_gift(o))
        except SupplierError as e:
            self.db.update_order(oid, event="supplier_retry", fail_reason=str(e))
            return
        self.db.update_order(oid, status="delivering", supplier_status=str(res.get("status")))
        await self._on_supplier_status(self.db.order(oid), res)

    async def _fail(self, o, reason: str) -> None:
        """Выдать не получилось: деньги клиента — на баланс магазина (можно потратить или вернуть по запросу)."""
        oid = o["id"]
        self.db.update_order(oid, status="refunded", fail_reason=reason)
        paid = float(o["total_rub"])
        self.db.credit(o["user_id"], "rub", paid, "refund", oid, f"возврат: {reason}")
        await self.tell(o["user_id"], f"↩️ Заказ <b>#{oid}</b> не удалось выдать ({reason}). "
                        f"<b>{paid:.2f} ₽</b> вернулись на ваш баланс магазина — оплатите ими другой заказ или "
                        "напишите в поддержку для возврата на карту/кошелёк.")
        await self.notify("WARNING", "order_refunded", f"#{oid}: возврат {paid:.2f} ₽ на баланс — {reason}")

    async def on_delivered(self, oid: str) -> None:
        o = self.db.order(oid)
        if o is None or o["status"] == "delivered":
            return
        self.db.update_order(oid, status="delivered", delivered_at=time.time())
        u = self.db.user(o["user_id"])
        self.db.exec("UPDATE users SET orders_count=orders_count+1, spent_rub=spent_rub+?, "
                     "first_order_at=COALESCE(first_order_at, ?) WHERE id=?", (o["total_rub"], time.time(), u["id"]))
        if o["referrer_id"] and self.db.user(o["referrer_id"]):
            reward = self.rtba.referral_reward(float(o["price_rub"]) - float(o["discount_rub"]), float(o["margin_rub"]))
            if reward > 0:
                self.db.credit(o["referrer_id"], "bonus", reward, "ref_reward", oid,
                               f"{self.cfg.get('referral.reward_pct')}% от покупки друга")
                self.db.update_order(oid, ref_reward_rub=reward)
                await self.tell(o["referrer_id"], f"🤝 Ваш друг оформил заказ — <b>+{reward:.2f} ₽</b> на бонусный "
                                "баланс.")
        await self.tell(o["user_id"], self.receipt(self.db.order(oid)))

    def receipt(self, o) -> str:
        lines = json.loads(o["breakdown"] or "[]")
        body = "\n".join(f"{a}: <b>{b}</b>" for a, b in lines)
        to = f"\nПолучатель: <b>{'@' + o['recipient'] if o['product'] != 'steam_keys' else 'ваш Steam'}</b>"
        return f"🎉 <b>Заказ #{o['id']} выполнен</b>\n\n{body}{to}\n\nСпасибо! Оцените нас в канале 💬"

    # ------------------------------------------------------------------ опрос
    async def poll_payments(self) -> None:
        now = time.time()
        for o in self.db.q("SELECT * FROM orders WHERE status IN ('awaiting_payment','awaiting_asset') "
                           "AND payment_id!=''"):
            prov = self.providers.get(o["pay_method"] if o["kind"] == "buy" else "stars")
            if prov is None:
                continue
            try:
                st = await prov.check(o["payment_id"])
            except PaymentError as e:
                log.warning("проверка платежа %s: %s", o["id"], e)
                continue
            if st == PAID:
                self.db.exec("UPDATE payments SET status='paid', updated_at=? WHERE id=?", (now, o["payment_id"]))
                await self.on_paid(o["id"])
            elif st == CANCELED or (o["expires_at"] and now > float(o["expires_at"])):
                self.db.exec("UPDATE payments SET status='canceled', updated_at=? WHERE id=?", (now, o["payment_id"]))
                self.db.update_order(o["id"], status="expired")
        # подарок без платежа (передача на аккаунт) тоже истекает
        for o in self.db.q("SELECT * FROM orders WHERE status='awaiting_asset' AND payment_id='' AND expires_at<?",
                           (now,)):
            self.db.update_order(o["id"], status="expired")

    async def poll_deliveries(self) -> None:
        for o in self.db.q("SELECT * FROM orders WHERE kind='buy' AND status IN ('delivering','paid','manual')"):
            if o["status"] == "paid":
                retries = int(self.db.scalar("SELECT COUNT(*) FROM order_events WHERE order_id=? AND "
                                             "kind='supplier_retry'", (o["id"],), 0))
                if retries >= int(self.cfg.get("rtba.max_delivery_retries") or 5) or \
                        time.time() - float(o["paid_at"] or 0) > 1800:
                    await self._fail(o, "поставщик недоступен")      # не держим деньги клиента бесконечно
                elif time.time() - float(o["paid_at"] or 0) > 20:
                    await self.deliver(o["id"])                    # поставщик был недоступен — повтор
                continue
            try:
                st = await self.suppliers[o["supplier"]].status(o["supplier_ref"] or o["id"])
            except SupplierError as e:
                log.warning("статус у поставщика %s: %s", o["id"], e)
                continue
            await self._on_supplier_status(o, st)
        for o in self.db.q("SELECT * FROM orders WHERE kind='sell' AND payout_kind='stars' AND status='received' "
                           "AND payout_id!=''"):
            try:
                st = await self.suppliers["stars"].status(o["payout_id"])
            except SupplierError:
                continue
            if st.get("status") == "delivered":
                self.db.update_order(o["id"], status="paid_out", delivered_at=time.time())

    # ------------------------------------------------------------------ выкуп у клиентов
    def quote_sell(self, product: str, amount: int, payout: str, gift_id: str = ""):
        if product == "stars":
            return sell_quote(self.cfg, "stars", amount, "usdt")
        g = self.gift(gift_id) or {}
        title = f"{g.get('emoji', '🎁')} {g.get('title', gift_id)}"
        return sell_quote(self.cfg, "gift", int(g.get("convert") or amount), payout, title)

    async def create_sell(self, uid: int, product: str, amount: int, payout: str, gift_id: str = "") -> tuple[Any, dict]:
        user = self.db.user(uid)
        if user is None or user["blocked"]:
            raise ValueError("операция недоступна")
        if not self.rtba.is_open(f"sell.{product}"):
            raise ValueError("Выкуп сейчас закрыт, попробуйте позже")
        if product == "stars":
            lo, hi = int(self.cfg.get("buyback.stars.min") or 100), int(self.cfg.get("buyback.stars.max") or 50000)
            if not lo <= amount <= hi:
                raise ValueError(f"Можно продать от {lo} до {hi} ⭐")
            payout = "usdt"                             # звёзды — только за крипту
        elif payout == "stars" and not self.rtba.is_open("payout.stars"):
            raise ValueError("Выплата звёздами сейчас недоступна — выберите крипту")
        elif payout == "usdt" and not self.rtba.is_open("payout.crypto"):
            raise ValueError("Выплата криптой сейчас недоступна")
        sq = self.quote_sell(product, amount, payout, gift_id)
        oid = self.db.new_order_id("sell")
        o = self.db.create_order(
            oid, user_id=uid, kind="sell", product=product, item=gift_id, amount=sq.amount, status="awaiting_asset",
            payout_kind=payout, payout_amount=sq.net, payout_asset=sq.asset, fee_rub=0, breakdown=sq.lines,
            expires_at=time.time() + float(self.cfg.get("payments.order_ttl_minutes") or 30) * 60)
        if product == "stars":
            prov = self.providers["stars"]
            pay = await prov.create(oid, 0, f"Продажа {sq.amount} ⭐ магазину")
            self.db.exec("INSERT INTO payments(id,order_id,provider,status,amount,currency,url,created_at,updated_at) "
                         "VALUES(?,?,?,?,?,?,?,?,?)", (pay["id"], oid, prov.name, "pending", sq.amount, "XTR",
                                                       pay.get("url", ""), time.time(), time.time()))
            o = self.db.update_order(oid, payment_id=pay["id"], pay_method="stars", pay_currency="XTR",
                                     pay_amount=sq.amount, event="invoice_created")
            return o, pay
        return o, {"id": "", "gift_account": self.cfg.get("buyback.gifts.gift_account") or "@shop_gifts"}

    async def confirm_gift_received(self, oid: str, who: str = "operator") -> str:
        """Подарок пришёл на аккаунт приёма (проверка оператором или Telethon-приёмником TG Gift)."""
        o = self.db.order(oid)
        if o is None or o["kind"] != "sell" or o["status"] not in ("awaiting_asset", "expired"):
            return "заказ не ждёт подарок"
        self.db.update_order(oid, status="paid", paid_at=time.time(), event=f"gift_received_by_{who}")
        await self.on_asset_received(oid)
        return "подарок принят, выплата запущена"

    async def on_asset_received(self, oid: str) -> None:
        o = self.db.update_order(oid, status="received")
        await self.tell(o["user_id"], f"📥 Получили {'звёзды' if o['product'] == 'stars' else 'подарок'} по заявке "
                                      f"<b>#{oid}</b>. Выплачиваю…")
        await self.payout(oid)

    async def payout(self, oid: str, approved_by: str = "") -> str:
        o = self.db.order(oid)
        u = self.db.user(o["user_id"])
        if o["status"] not in ("received", "payout_pending"):
            return "заявка не ждёт выплаты"
        if o["payout_kind"] == "stars":
            stars = int(o["payout_amount"])
            self.db.credit(u["id"], "stars", stars, "buyback", oid, "выкуп подарка")
            self.db.update_order(oid, status="paid_out", delivered_at=time.time())
            total = int(self.db.user(u["id"])["stars_credit"])
            await self.tell(u["id"], f"⭐ +{stars} ⭐ на ваш звёздный баланс магазина (всего {total} ⭐). "
                                     f"Вывести на аккаунт можно от 50 ⭐ в «👛 Баланс».")
            return "звёзды начислены"
        amount = float(o["payout_amount"])
        if not approved_by:
            chk = self.rtba.payout_decision(u, amount)
            if not chk.ok:
                self.db.update_order(oid, status="payout_pending", fail_reason=chk.reason)
                await self.notify("WARNING", "payout_review", f"Выплата #{oid}: {amount:.2f} USDT клиенту "
                                  f"{u['id']} (@{u['username'] or '—'}) — {chk.reason}",
                                  [("✔ Выплатить", f"adm:payout:{oid}"), ("✋ Отклонить", f"adm:reject:{oid}")])
                await self.tell(u["id"], f"🔎 Выплата по заявке <b>#{oid}</b> на проверке, обычно до часа.")
                return "на проверке"
        if not self.rtba.is_open("payout.crypto"):
            self.db.update_order(oid, status="payout_pending", fail_reason="выплаты криптой закрыты RTBA")
            await self.notify("WARNING", "payout_closed", f"Выплата #{oid} ждёт: выплаты криптой закрыты")
            return "выплаты закрыты"
        prov = self.providers["crypto"]
        try:
            t = await prov.transfer(u["id"], o["payout_asset"], amount, f"shop-{oid}", f"Выкуп, заявка {oid}")
        except PaymentError as e:
            self.db.update_order(oid, status="payout_pending", fail_reason=str(e))
            await self.notify("CRITICAL", "payout_failed", f"Выплата #{oid} {amount:.2f} USDT не прошла: {e}",
                              [("🔁 Повторить", f"adm:payout:{oid}")])
            return f"ошибка: {e}"
        self.db.update_order(oid, status="paid_out", payout_id=str(t.get("transfer_id")), delivered_at=time.time(),
                             event=f"paid_out{'_by_' + approved_by if approved_by else ''}")
        self.db.exec("UPDATE users SET sold_usdt=sold_usdt+? WHERE id=?", (amount, u["id"]))
        await self.tell(u["id"], f"💸 Выплачено <b>{amount:.2f} {o['payout_asset']}</b> в @CryptoBot по заявке "
                                 f"<b>#{oid}</b>.\n\n" + "\n".join(f"{a}: <b>{b}</b>"
                                                                    for a, b in json.loads(o["breakdown"])))
        return "выплачено"

    async def reject(self, oid: str, reason: str = "отклонено оператором") -> str:
        o = self.db.order(oid)
        if o is None or o["status"] not in ("payout_pending", "manual", "awaiting_asset", "need_recipient"):
            return "нельзя отклонить в этом статусе"
        if o["kind"] == "buy" and o["status"] == "awaiting_asset":     # не оплачен — возвращать нечего
            self.db.update_order(oid, status="cancelled", fail_reason=reason)
            await self.tell(o["user_id"], f"Заказ <b>#{oid}</b> отменён: оплата не подтверждена ({reason}). "
                                          "Если вы уже отправили подарки или скины — напишите в поддержку.")
            return "отменено"
        if o["kind"] == "buy":
            await self._fail(o, reason)
            return "возврат на баланс"
        self.db.update_order(oid, status="rejected", fail_reason=reason)
        await self.tell(o["user_id"], f"Заявка <b>#{oid}</b> отклонена: {reason}. Напишите в поддержку.")
        return "отклонено"

    async def withdraw_stars(self, uid: int) -> str:
        u = self.db.user(uid)
        stars = int(u["stars_credit"] or 0)
        if stars < 50:
            return f"Вывод звёзд — от 50 ⭐, у вас {stars} ⭐"
        if not u["username"]:
            return "Для вывода нужен @username в Telegram"
        if not self.rtba.is_open("payout.stars"):
            return "Вывод звёзд временно недоступен"
        ref = f"W{int(time.time())}{uid % 10000}"
        try:
            res = await self.suppliers["stars"].order(ref, "stars", u["username"], stars)
        except SupplierError as e:
            return f"Не получилось: {e}"
        if not res.get("ok"):
            return f"Не получилось: {res.get('error')}"
        self.db.credit(uid, "stars", -stars, "withdraw", ref, f"вывод на @{u['username']}")
        return f"⭐ {stars} ⭐ отправляются на @{u['username']} через Fragment — пара минут."

    async def admin_delivered(self, oid: str) -> str:
        o = self.db.order(oid)
        if o is None or o["status"] not in ("manual", "delivering"):
            return "заказ не ждёт ручной выдачи"
        sup = self.suppliers.get(o["supplier"])
        if hasattr(sup, "mark"):
            sup.mark(o["supplier_ref"] or oid, "delivered")
        await self.on_delivered(oid)
        return "отмечено выданным"

    # ------------------------------------------------------------------ сводка
    def summary(self, days: float = 1) -> dict:
        since = time.time() - days * 86400
        buy = self.db.one("SELECT COUNT(*) n, SUM(total_rub) rev, SUM(margin_rub) margin, SUM(fee_rub) fees, "
                          "SUM(ref_reward_rub) refs FROM orders WHERE kind='buy' AND status='delivered' AND "
                          "delivered_at>=?", (since,))
        sell = self.db.one("SELECT COUNT(*) n, SUM(payout_amount) usdt FROM orders WHERE kind='sell' AND "
                           "status='paid_out' AND payout_kind='usdt' AND delivered_at>=?", (since,))
        by_product = {r["product"]: {"n": r["n"], "rev": round(r["rev"] or 0, 2), "margin": round(r["m"] or 0, 2)}
                      for r in self.db.q("SELECT product, COUNT(*) n, SUM(total_rub) rev, SUM(margin_rub) m FROM "
                                         "orders WHERE kind='buy' AND status='delivered' AND delivered_at>=? "
                                         "GROUP BY product", (since,))}
        active = self.db.scalar("SELECT COUNT(*) FROM orders WHERE status IN ('awaiting_payment','paid','delivering',"
                                "'need_recipient','awaiting_asset','received')", (), 0)
        attention = self.db.scalar("SELECT COUNT(*) FROM orders WHERE status IN ('manual','payout_pending')", (), 0)
        return {"orders": buy["n"] or 0, "revenue": round(buy["rev"] or 0, 2), "margin": round(buy["margin"] or 0, 2),
                "pay_fees": round(buy["fees"] or 0, 2), "ref_paid": round(buy["refs"] or 0, 2),
                "buyback_n": sell["n"] or 0, "buyback_usdt": round(sell["usdt"] or 0, 2), "by_product": by_product,
                "active": active, "attention": attention, "users": self.db.scalar("SELECT COUNT(*) FROM users", (), 0),
                "new_users": self.db.scalar("SELECT COUNT(*) FROM users WHERE created_at>=?", (since,), 0)}

    async def _loop_tick(self) -> None:
        self.loop.tick()
        todo = self.loop.todo_text()
        last = float(self.db.kv_get("loop_notified") or 0)
        if todo and time.time() - last > 86400:            # раз в сутки напомнить, что лежит вне круга
            self.db.kv_set("loop_notified", time.time())
            await self.notify("INFO", "loop_idle", todo)

    # ------------------------------------------------------------------ циклы
    async def _loop(self, name: str, every: float, fn) -> None:
        while True:
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("цикл %s упал, продолжаю", name)
            await asyncio.sleep(every)

    def _restore_wallet(self) -> None:
        w = self.providers.get("wallet")
        if w is None or not hasattr(w, "restore"):
            return
        for o in self.db.q("SELECT * FROM orders WHERE pay_method='wallet' AND status='awaiting_payment' "
                           "AND payment_id!=''"):
            w.restore(o["payment_id"], o["pay_currency"], float(o["pay_amount"]), float(o["created_at"] or 0))

    async def start(self) -> None:
        self._restore_wallet()
        await self.rates.refresh()
        await self.rtba.tick()
        loops = [("rates", float(self.cfg.get("rates.refresh_seconds") or 120), self.rates.refresh),
                 ("rtba", float(self.cfg.get("rtba.interval_seconds") or 30), self.rtba.tick),
                 ("payments", 5, self.poll_payments), ("deliveries", 5, self.poll_deliveries),
                 ("loop", 600, self._loop_tick)]
        if self.channel is not None:
            loops.append(("channel", 300, self.channel.autoupdate))
        for name, every, fn in loops:
            self.tasks.append(asyncio.create_task(self._loop(name, every, fn)))
        if self.bot is not None:
            self.tasks.append(asyncio.create_task(self.bot.run()))

    async def stop(self) -> None:
        for t in self.tasks:
            t.cancel()
        for t in self.tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self.tasks.clear()
        if self.bot is not None:
            await self.bot.close()

