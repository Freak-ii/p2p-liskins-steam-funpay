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
from shopbot import economy as E
from shopbot.loop import MoneyLoop
from shopbot.pricing import METHODS, PRODUCT_TITLES, Quote, apply_method, referral_discount, sell_quote, shop_price
from shopbot.rates import Rates
from shopbot.rtba import RTBA, PRODUCT_SUPPLIER
from shopbot.services import ServiceSupplier, delivery_text, mask, service_price, split_cost
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
        self.services = ServiceSupplier(cfg, db, demo=demo, rates=self.rates)
        self.suppliers["services"] = self.services
        self.providers = build_providers(cfg, demo=demo)
        from shopbot.orbit import ShopOrbit
        self.orbit = ShopOrbit(self)        # Пульт Орбиты центра: снятые товары пропадают из бота и канала
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
        if product == "service":
            return self.services.catalog.title(item, amount)
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
        if product == "service":
            return await self._quote_service(user, item, amount)
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
        tier = E.tier_for(self.cfg, self.db, user)
        loyal = E.loyalty_discount(self.cfg, tier, price, cost, product, disc)
        return Quote(product, self.product_title(product, item, amount), sup_amount, cost, price, round(disc + loyal, 2),
                     loyalty_rub=loyal, tier=tier.title if loyal else "")

    async def _quote_service(self, user, item: str, amount: int) -> Quote:
        it, v = self.services.catalog.variant(item)
        if it is None or v is None:
            raise ValueError("нет такого товара")
        if not self.rtba.is_open(f"svc.{it['id']}"):
            raise SupplierError("товар временно недоступен")
        q = await self.services.quote_item(item, amount)
        if not q.get("available"):
            raise SupplierError(q.get("reason") or "товар временно недоступен")
        cost = float(q["cost_rub"])
        price, _ = service_price(self.cfg, it, v, cost, amount)
        disc = referral_discount(self.cfg, user, price, cost, "service")
        tier = E.tier_for(self.cfg, self.db, user)
        loyal = E.loyalty_discount(self.cfg, tier, price, cost, "service", disc)
        return Quote("service", self.product_title("service", item, amount), amount, cost, price,
                     round(disc + loyal, 2), loyalty_rub=loyal, tier=tier.title if loyal else "")

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
                         asset: str = "USDT", *, secret: dict | None = None,
                         consent: dict | None = None) -> tuple[Any, dict]:
        user = self.db.user(uid)
        err = (self.services.catalog.validate(item, amount) if product == "service"
               else self.validate_amount(product, amount))
        if err:
            raise ValueError(err)
        if method == "stars" and product == "stars":
            raise ValueError("звёзды нельзя оплатить звёздами")
        q = await self.quote_buy(user, product, item, amount)
        q = apply_method(self.cfg, q, method, self.rates, asset)
        tier = E.tier_for(self.cfg, self.db, user)
        chk = self.rtba.check_order(user, product, q.total_rub, method, limit_mult=tier.limit_mult,
                                    recipient=recipient)
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
            supplier=PRODUCT_SUPPLIER[product], referrer_id=user["referred_by"], expires_at=time.time() + ttl,
            source=str(user["source"] or ""), loyalty_rub=q.loyalty_rub, tier=tier.id)
        if product == "service":
            self._service_consent(oid, item, consent)
            self.services.save_secret(oid, secret or {})
        if method == "balance":
            await self._pay_from_balance(o)
            return self.db.order(oid), {"id": "balance", "url": ""}
        prov = self.providers[method]
        try:
            pay = await prov.create(oid, q.total_rub, f"{q.title} · заказ {oid}", asset=q.asset,
                                    pay_amount=q.pay_amount, ttl=int(ttl),
                                    payer={"telegram_id": uid, "telegram_username": user["username"] or ""})
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

    def _service_consent(self, oid: str, item: str, consent: dict | None) -> None:
        """Доказательство для спора: покупатель до оплаты видел, как получит товар, и нажал «Понятно»."""
        sid, _ = self.services.catalog.split(item)
        it = self.services.catalog.item(sid) or {}
        c = consent or {}
        self.db.event(oid, "terms_accepted", service=sid, notice_hash=self.services.catalog.notice_hash(sid),
                      accepted_at=c.get("ts"), shown=bool(c), notice=str(it.get("notice") or "")[:1500])

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
        bonus = E.bonus_usable(self.cfg, u, total)          # бонусы — скидка до N% заказа, не деньги
        rub = round(total - bonus, 2)
        if rub > float(u["balance_rub"] or 0) + 1e-9:
            self.db.update_order(o["id"], status="cancelled", fail_reason="мало средств на балансе")
            raise ValueError(f"На балансе {float(u['balance_rub']) + float(u['bonus_rub']):.2f} ₽, нужно {total:.2f} ₽")
        if bonus:
            self.db.credit(u["id"], "bonus", -bonus, "purchase", o["id"], "оплата заказа бонусами")
            self.db.update_order(o["id"], bonus_rub=bonus)
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
            if o["product"] == "service":
                res = await self.services.order_item(oid, oid, o["item"], o["recipient"], int(o["amount"]))
            else:
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
        elif s == "manual" and o["status"] != "manual" and o["product"] == "service":
            await self._service_task(o, st)
        elif s == "manual" and o["status"] != "manual":
            self.db.update_order(oid, status="manual", fail_reason=str(st.get("detail") or "ручная выдача"))
            await self.notify("WARNING", "order_manual", f"#{oid}: {o['product']} ждёт ручной выдачи → "
                                                         f"{o['recipient']}", [("✔ Выдано", f"adm:delivered:{oid}")])
            await self.tell(o["user_id"], f"🕐 Заказ <b>#{oid}</b> передан оператору, выдача обычно до 30 минут.")

    async def _service_task(self, o, st: dict) -> None:
        """Перепродажа: задача оператору — что купить, где и для кого. Данные входа — только админам в личку."""
        oid = o["id"]
        self.db.update_order(oid, status="manual", fail_reason=str(st.get("detail") or "ручная выдача"))
        sid, _ = self.services.catalog.split(o["item"])
        it = self.services.catalog.item(sid) or {}
        who = ""
        if it.get("kind") in ("account", "topup"):
            sec = self.services.secret(oid)
            who = f"\nЛогин: <code>{sec.get('login') or o['recipient']}</code>"
            if sec.get("password"):
                who += f"\nПароль: <tg-spoiler>{sec['password']}</tg-spoiler>"
            if it.get("kind") == "topup":
                who += f"\nСумма: <b>{o['amount']} ₽</b> Steam"
        sla = int(self.cfg.get("services.resale_sla_minutes") or 120)
        await self.notify("WARNING", "service_task",
                          f"#{oid}: {o['product']} {self.product_title('service', o['item'], int(o['amount']))}\n"
                          f"{st.get('detail') or ''} (себестоимость ≈ {float(st.get('cost_rub') or 0):.0f} ₽){who}\n"
                          "Купите и пришлите код/ссылку кнопкой ниже (для подписки на аккаунт — «готово»).",
                          [("📦 Выдать", f"adm:svc:{oid}"), ("↩️ Вернуть деньги", f"adm:svcfail:{oid}")])
        await self.tell(o["user_id"], f"🕐 Заказ <b>#{oid}</b> принят в работу. Выдача обычно до {sla} минут — "
                                      "пришлём сюда, как только будет готово.")

    async def admin_service_deliver(self, oid: str, payload: str, who: str = "operator") -> str:
        o = self.db.order(oid)
        if o is None or o["product"] != "service" or o["status"] not in ("manual", "delivering"):
            return "заказ не ждёт выдачи"
        payload, real_cost = split_cost(payload)
        if payload.lower() in ("готово", "done", "ok", "+"):
            payload = ""
        sid, _ = self.services.catalog.split(o["item"])
        it = self.services.catalog.item(sid) or {}
        if it.get("kind") in ("code", "vpn") and not payload:
            return "для этого товара пришлите код/ключ"
        if it.get("kind") == "family" and not payload.startswith("http"):
            return "пришлите ссылку-приглашение в семью (https://…)"
        if real_cost is not None:
            self.services.record_cost(o["supplier_ref"] or oid, real_cost)
        self.services.mark(o["supplier_ref"] or oid, "delivered", payload)
        self.db.event(oid, "service_delivered_by", who=who, **({"cost_rub": real_cost} if real_cost else {}))
        await self.on_delivered(oid)
        return "выдано"

    async def admin_service_fail(self, oid: str, reason: str = "не удалось купить у поставщика") -> str:
        o = self.db.order(oid)
        if o is None or o["product"] != "service" or o["status"] not in ("manual", "delivering", "paid"):
            return "заказ не ждёт выдачи"
        self.services.mark(o["supplier_ref"] or oid, "failed")
        await self._fail(o, reason)
        return "деньги возвращены на баланс клиента"

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
        if o["product"] == "service":
            self.services.wipe(oid)
            self.services.family.release(oid)
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
        ref_user = self.db.user(o["referrer_id"]) if o["referrer_id"] else None
        if ref_user is not None:
            reward = self.rtba.referral_reward(float(o["price_rub"]) - float(o["discount_rub"]), float(o["margin_rub"]))
            fraud = E.self_referral(self.db, o, ref_user)
            if fraud:
                reward = 0.0
                self.db.event(oid, "ref_denied", reason=fraud)
                await self.notify("WARNING", "ref_self", f"#{oid}: награда пригласившему {ref_user['id']} не "
                                                         f"начислена — {fraud}")
            reward = round(min(reward, E.referral_day_cap_left(self.cfg, self.db, ref_user["id"])), 2)
            if reward > 0:
                self.db.credit(o["referrer_id"], "bonus", reward, "ref_reward", oid,
                               f"{self.cfg.get('referral.reward_pct')}% от покупки друга")
                self.db.update_order(oid, ref_reward_rub=reward)
                await self.tell(o["referrer_id"], f"🤝 Ваш друг оформил заказ — <b>+{reward:.2f} ₽</b> на бонусный "
                                "баланс.")
        if o["product"] == "service":
            await self._service_delivered(o)
        await self.tell(o["user_id"], self.receipt(self.db.order(oid)))
        await self.ask_review(self.db.order(oid))

    async def _service_delivered(self, o) -> None:
        sid, _ = self.services.catalog.split(o["item"])
        it = self.services.catalog.item(sid) or {}
        payload = self.services.payload(o["id"])
        job = self.db.one("SELECT source, cost_rub, expires_at, detail FROM service_jobs WHERE order_id=? "
                          "ORDER BY updated_at DESC LIMIT 1", (o["id"],))
        if job is not None and float(job["cost_rub"] or 0) and abs(float(job["cost_rub"]) - float(o["cost_rub"])) > 0.01:
            cost = float(job["cost_rub"])            # фактическая себестоимость (склад FIFO) — маржа честная
            self.db.update_order(o["id"], cost_rub=cost,
                                 margin_rub=round(float(o["price_rub"]) - float(o["discount_rub"]) - cost, 2))
        self.db.event(o["id"], "service_payload", source=job["source"] if job else "", payload=mask(payload))
        self.services.wipe(o["id"])
        exp = float(job["expires_at"] or 0) if job is not None else 0.0
        renewed = job is not None and job["detail"] == "продление"
        btns = None
        if it.get("kind") == "family" and not renewed:
            btns = [[("✅ Я вступил", f"fj:{o['id']}")], [("⚠️ Не получается", f"fp:{o['id']}")]]
        await self.tell(o["user_id"], delivery_text(it, payload, exp, renewed), btns)

    async def remind_expiring(self) -> None:
        """За 3 дня до конца срока VPN-ключа — напоминание с кнопкой продления (один раз на ключ)."""
        for j in self.services.expiring(3):
            self.services.mark_reminded(j["ref"])
            left = max(1, int((float(j["expires_at"]) - time.time()) // 86400) + 1)
            await self.tell(j["user_id"], f"⏳ Ваш ключ VPN (заказ <b>#{j['order_id']}</b>) закончится через "
                                          f"<b>{left} дн.</b> Продлите — ссылка в Happ останется той же.",
                            [("🔄 Продлить", f"rn:{j['order_id']}")])

    # ------------------------------------------------------------------ семьи Яндекс Плюс
    def _family_item(self, oid: str):
        o = self.db.order(oid)
        if o is None or o["product"] != "service":
            return o, None
        it = self.services.catalog.item(self.services.catalog.split(o["item"])[0]) or {}
        return o, (it if it.get("kind") == "family" else None)

    async def family_tick(self) -> None:
        fam = self.services.family
        for s in fam.expiring(3):                       # за 3 дня — напоминание с продлением
            fam.mark(s["id"], reminded=1)
            left = max(1, int((float(s["until"]) - time.time()) // 86400) + 1)
            await self.tell(s["user_id"], f"⏳ Яндекс Плюс (заказ <b>#{s['order_id']}</b>) закончится через "
                                          f"<b>{left} дн.</b> Продлите — останетесь в той же семье.",
                            [("🔄 Продлить", f"fr:{s['order_id']}")])
        for s in fam.expired():                         # срок вышел — исключить из семьи
            fam.mark(s["id"], status="leaving")
            await self.notify("WARNING", "family_remove",
                              f"Яндекс Плюс: срок места вышел — исключите <b>{s['member'] or 'участника'}</b> из "
                              f"семьи «{s['label']}» {s['login']} (заказ #{s['order_id']})",
                              [("✔ Исключён", f"adm:famrm:{s['id']}")])
            await self.tell(s["user_id"], f"⌛ Срок Яндекс Плюса по заказу <b>#{s['order_id']}</b> закончился. "
                                          "Оформите новый — пришлём приглашение.", [("🟡 Яндекс Плюс", "sv:yandex_plus")])
        for s in fam.unconfirmed():                     # ссылку выдали, «Я вступил» не нажато
            fam.mark(s["id"], reminded=1)
            await self.tell(s["user_id"], f"👋 Получилось вступить в семью по заказу <b>#{s['order_id']}</b>? "
                                          "Ссылка действует ограниченное время.",
                            [[("✅ Я вступил", f"fj:{s['order_id']}")], [("⚠️ Не получается", f"fp:{s['order_id']}")]])

    async def family_joined(self, uid: int, oid: str) -> str:
        o, it = self._family_item(oid)
        if o is None or it is None or o["user_id"] != uid:
            return "Заказ не найден."
        fam = self.services.family
        if not fam.joined(oid):
            return "Уже отмечено 👌"
        s = fam.seat(oid)
        a = fam.account(s["account_id"])
        fam.mark(s["id"], reminded=0)
        self.db.event(oid, "family_joined", account=a["label"], member=s["member"])
        await self.notify("INFO", "family_joined", f"#{oid}: {s['member'] or 'покупатель'} вступил в семью "
                                                   f"«{a['label']}» — проверьте в списке близких")
        till = time.strftime("%d.%m.%Y", time.localtime(float(s["until"])))
        return f"🎉 Готово! Плюс действует до <b>{till}</b>. Не выходите из семьи до конца срока."

    async def family_problem(self, uid: int, oid: str) -> str:
        o, it = self._family_item(oid)
        if o is None or it is None or o["user_id"] != uid:
            return "Заказ не найден."
        fam = self.services.family
        s = fam.seat(oid)
        a = fam.account(s["account_id"]) if s else None
        self.db.event(oid, "family_problem")
        await self.notify("WARNING", "family_problem",
                          f"#{oid}: покупатель {s['member'] if s else ''} не может вступить в семью "
                          f"«{a['label'] if a else '?'}». Проверьте: не был ли он в другой семье последние 14 дней, "
                          "тот ли аккаунт. Пришлите новую ссылку или переведите в другую семью.",
                          [("🔗 Новая ссылка", f"adm:famlink:{oid}"), ("↩️ Вернуть деньги", f"adm:famfail:{oid}")])
        return ("🛠 Передали оператору — пришлём новую ссылку. Проверьте, что входите в нужный Яндекс ID и что "
                "не выходили из другой семьи последние 14 дней.")

    async def admin_family_link(self, oid: str, link: str, who: str = "operator") -> str:
        o, it = self._family_item(oid)
        link = str(link or "").strip()
        if o is None or it is None:
            return "это не заказ Яндекс Плюса"
        if not link.startswith("http"):
            return "пришлите ссылку https://…"
        if o["status"] in ("manual", "delivering"):
            return await self.admin_service_deliver(oid, link, who)
        fam = self.services.family
        s = fam.seat(oid)
        if s is None or s["status"] not in ("reserved", "invited"):
            return "место не ждёт приглашения"
        fam.set_invite(oid, link)
        fam.mark(s["id"], reminded=0)
        self.db.event(oid, "family_new_link", who=who)
        await self.tell(o["user_id"], f"🔗 Новая ссылка-приглашение по заказу <b>#{oid}</b>:\n<code>{link}</code>",
                        [[("✅ Я вступил", f"fj:{oid}")], [("⚠️ Не получается", f"fp:{oid}")]])
        return "ссылка отправлена"

    async def admin_family_removed(self, seat_id: int) -> str:
        return "место освобождено" if self.services.family.removed(seat_id) else "место не найдено"

    async def admin_family_refund(self, oid: str) -> str:
        """Не смог вступить — возврат на баланс (до вступления место освобождается сразу)."""
        o, it = self._family_item(oid)
        if o is None or it is None or o["status"] != "delivered":
            return await self.admin_service_fail(oid)
        s = self.services.family.seat(oid)
        if s is None or s["status"] not in ("reserved", "invited"):
            return "участник уже вступил — возврат вручную"
        self.db.update_order(oid, status="manual")
        return await self.admin_service_fail(oid, "не удалось вступить в семью")

    async def admin_family_ban(self, aid: int) -> str:
        """Аккаунт потерян (бан, Яндекс отключил семью): всех участников — в другие семьи на оставшийся срок."""
        fam = self.services.family
        a = fam.account(aid)
        if a is None:
            return "нет аккаунта"
        fam.update_account(aid, status="banned")
        moved = waiting = 0
        for s in fam.to_migrate(aid):
            left = (float(s["until"]) - time.time()) / (30 * 86400)
            fam.mark(s["id"], status="moved", note="аккаунт потерян")
            res = fam.reserve(a["service"], s["order_id"], s["user_id"], s["member"], left, exclude=aid,
                              until=float(s["until"]))
            if not res.get("ok"):
                waiting += 1
                await self.notify("WARNING", "family_migrate", f"#{s['order_id']}: некуда перевести "
                                  f"{s['member'] or 'участника'} — купите Плюс на новый аккаунт или верните деньги")
                continue
            moved += 1
            if res.get("invite"):
                await self.tell(s["user_id"], f"🔄 Семья Яндекс Плюса по заказу <b>#{s['order_id']}</b> отключена. "
                                f"Бесплатно переводим вас в другую на оставшийся срок:\n<code>{res['invite']}</code>",
                                [[("✅ Я вступил", f"fj:{s['order_id']}")], [("⚠️ Не получается",
                                                                             f"fp:{s['order_id']}")]])
            else:
                b = fam.account(res["account"])
                await self.notify("WARNING", "family_migrate", f"#{s['order_id']}: переселение в «{b['label']}» "
                                  f"{b['login']} — пришлите ссылку", [("🔗 Ссылка", f"adm:famlink:{s['order_id']}")])
                await self.tell(s["user_id"], f"🔄 Семья Яндекс Плюса по заказу <b>#{s['order_id']}</b> отключена. "
                                "Переводим вас в другую бесплатно — ссылка придёт сюда в ближайшее время.")
        await self.notify("WARNING", "family_ban", f"Яндекс Плюс: аккаунт «{a['label']}» выведен, переселено "
                                                   f"{moved}, ждут места {waiting}")
        return f"переселено {moved}, ждут места {waiting}"

    # ------------------------------------------------------------------ отзывы
    async def ask_review(self, o) -> None:
        if not self.cfg.get("reviews.enabled") or o is None or o["kind"] != "buy":
            return
        if self.db.one("SELECT 1 FROM reviews WHERE order_id=?", (o["id"],)):
            return
        bonus = float(self.cfg.get("reviews.bonus_rub") or 0)
        extra = (f" За отзыв — <b>+{bonus:g} ₽</b> бонусов." if bonus and float(o["total_rub"] or 0) >=
                 float(self.cfg.get("reviews.bonus_min_order_rub") or 0) else "")
        await self.tell(o["user_id"], f"⭐ Оцените заказ <b>#{o['id']}</b>, это займёт секунду.{extra}",
                        [[(f"{'⭐' * n}", f"rv:{o['id']}:{n}") for n in (5, 4, 3)],
                         [(f"{'⭐' * n}", f"rv:{o['id']}:{n}") for n in (2, 1)]])

    async def add_review(self, uid: int, oid: str, rating: int, text: str = "") -> str:
        o = self.db.order(oid)
        if o is None or o["user_id"] != uid or o["kind"] != "buy" or o["status"] != "delivered":
            return "Отзыв можно оставить только к своему выданному заказу."
        rating = max(1, min(5, int(rating)))
        now = time.time()
        old = self.db.one("SELECT * FROM reviews WHERE order_id=?", (oid,))
        if old is None:
            bonus = float(self.cfg.get("reviews.bonus_rub") or 0)
            if float(o["total_rub"] or 0) < float(self.cfg.get("reviews.bonus_min_order_rub") or 0):
                bonus = 0.0
            self.db.exec("INSERT INTO reviews(order_id,user_id,rating,text,product,status,bonus_rub,created_at,"
                         "updated_at) VALUES(?,?,?,?,?,?,?,?,?)", (oid, uid, rating, text[:500], o["product"],
                                                                   "new", bonus, now, now))
            if bonus > 0:
                self.db.credit(uid, "bonus", bonus, "review", oid, "бонус за отзыв")
        else:
            self.db.exec("UPDATE reviews SET rating=?, text=CASE WHEN ?<>'' THEN ? ELSE text END, updated_at=? "
                         "WHERE order_id=?", (rating, text[:500], text[:500], now, oid))
        if rating <= 3:
            await self.notify("WARNING", "review_low", f"Отзыв {rating}⭐ к #{oid} от {uid}: {text[:200] or '—'}",
                              [("💬 Открыть заказ", f"adm:order:{oid}")])
        return "Спасибо за оценку!" + (" Напишите пару слов о заказе — это поможет другим покупателям."
                                        if not text else "")

    def receipt(self, o) -> str:
        lines = json.loads(o["breakdown"] or "[]")
        body = "\n".join(f"{a}: <b>{b}</b>" for a, b in lines)
        to = ("\nПолучатель: <b>" + ("ваш Steam" if o["product"] == "steam_keys" else
                                     "этот чат" if o["product"] == "service" and o["recipient"] in ("", "chat") else
                                     "продление ключа" if o["product"] == "service" and
                                     str(o["recipient"]).startswith("renew:") else
                                     o["recipient"] if o["product"] == "service" else "@" + o["recipient"]) + "</b>")
        return f"🎉 <b>Заказ #{o['id']} выполнен</b>\n\n{body}{to}\n\nСпасибо! Оцените нас в канале 💬"

    # ------------------------------------------------------------------ опрос
    async def poll_payments(self) -> None:
        now = time.time()
        for o in self.db.q("SELECT * FROM orders WHERE status IN ('awaiting_payment','awaiting_asset') "
                           "AND payment_id!=''"):
            await self._check_payment(o, now)
        # шлюз мог провести оплату на границе срока (processing → payed): истёкшие счета шлюза ещё час
        # проверяем на поздний приход — иначе деньги на кассе, а заказ «истёк»
        for o in self.db.q("SELECT o.* FROM orders o JOIN payments p ON p.id=o.payment_id WHERE o.status='expired' "
                           "AND p.provider IN ('crystalpay','aaio') AND o.expires_at>?", (now - 3600,)):
            await self._check_payment(o, now)
        # подарок без платежа (передача на аккаунт) тоже истекает
        for o in self.db.q("SELECT * FROM orders WHERE status='awaiting_asset' AND payment_id='' AND expires_at<?",
                           (now,)):
            self.db.update_order(o["id"], status="expired")

    async def _check_payment(self, o, now: float) -> str:
        prov = self.providers.get(o["pay_method"] if o["kind"] == "buy" else "stars")
        if prov is None:
            return ""
        try:
            st = await prov.check(o["payment_id"])
        except PaymentError as e:
            log.warning("проверка платежа %s: %s", o["id"], e)
            return ""
        if st == PAID:
            self.db.exec("UPDATE payments SET status='paid', updated_at=? WHERE id=?", (now, o["payment_id"]))
            await self.on_paid(o["id"])
        elif o["status"] != "expired" and (st == CANCELED or (o["expires_at"] and now > float(o["expires_at"]))):
            self.db.exec("UPDATE payments SET status='canceled', updated_at=? WHERE id=?", (now, o["payment_id"]))
            self.db.update_order(o["id"], status="expired")
        return st

    async def on_gateway_callback(self, provider: str, data: dict) -> bool:
        """Уведомление шлюза (CrystalPAY callback). Подпись проверяет провайдер; статус всё равно берём из API,
        поэтому повтор и подделанное тело ничего не выдают. Повторы безопасны (on_paid идемпотентен по статусу)."""
        prov = next((p for p in self.providers.values() if getattr(p, "name", "") == provider), None)
        if prov is None or not hasattr(prov, "verify_callback") or not prov.verify_callback(data):
            return False
        o = self.db.one("SELECT * FROM orders WHERE payment_id=? AND status IN ('awaiting_payment','awaiting_asset',"
                        "'expired')",
                        (str(data.get("id")),))
        if o is not None:
            await self._check_payment(o, time.time())
        return True

    async def poll_deliveries(self) -> None:
        await self.remind_expiring()
        await self.family_tick()
        sla = float(self.cfg.get("services.resale_sla_minutes") or 120) * 60
        for o in self.db.q("SELECT * FROM orders WHERE kind='buy' AND product='service' AND status='manual' AND "
                           "paid_at<?", (time.time() - sla,)):
            if not self.db.scalar("SELECT 1 FROM order_events WHERE order_id=? AND kind='sla_alert'", (o["id"],)):
                self.db.event(o["id"], "sla_alert")
                await self.notify("WARNING", "service_sla", f"#{o['id']}: сервис ждёт выдачи дольше "
                                  f"{sla / 60:.0f} мин — выдайте или верните деньги",
                                  [("📦 Выдать", f"adm:svc:{o['id']}"), ("↩️ Вернуть", f"adm:svcfail:{o['id']}")])
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
    def xtr_stock(self) -> int | None:
        """Звёзды на балансе бота (getMyStarBalance, обновляет RTBA)."""
        v = self.db.kv_get("xtr_balance")
        return int(v["stars"]) if isinstance(v, dict) and "stars" in v else None

    def quote_sell(self, product: str, amount: int, payout: str, gift_id: str = "", user=None):
        if product == "stars":
            return sell_quote(self.cfg, "stars", amount, "usdt", tier=E.tier_for(self.cfg, self.db, user),
                              xtr_stock=self.xtr_stock())
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
        sq = self.quote_sell(product, amount, payout, gift_id, user=user)
        oid = self.db.new_order_id("sell")
        o = self.db.create_order(
            oid, user_id=uid, kind="sell", product=product, item=gift_id, amount=sq.amount, status="awaiting_asset",
            payout_kind=payout, payout_amount=sq.net, payout_asset=sq.asset, fee_rub=0, breakdown=sq.lines,
            source=str(user["source"] or ""),
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
            risk = E.sell_risk(self.cfg, self.db, u, amount, exclude=oid)
            if chk.ok and risk.review:
                chk = type(chk)(False, risk.reason)
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

    async def _open_post(self) -> None:
        await asyncio.sleep(5)                 # бот успевает узнать свой @username
        try:
            res = await self.channel.ensure_open_post()
            if res not in ("выключено", "уже опубликован"):
                log.info("пост «Открыть магазин»: %s", res)
        except Exception:  # noqa: BLE001
            log.exception("пост «Открыть магазин»")

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
                 ("loop", 600, self._loop_tick), ("orbit", 5, self.orbit.tick)]
        if self.channel is not None:
            loops.append(("channel", 300, self.channel.autoupdate))
        for name, every, fn in loops:
            self.tasks.append(asyncio.create_task(self._loop(name, every, fn)))
        if self.bot is not None:
            self.tasks.append(asyncio.create_task(self.bot.run()))
        if self.channel is not None and self.bot is not None and self.bot.token and not self.demo:
            self.tasks.append(asyncio.create_task(self._open_post()))

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

