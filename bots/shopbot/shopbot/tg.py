"""Telegram-бот магазина (для клиентов). Long polling — работает с домашнего компьютера без белого IP.

Без токена (демо) сообщения не уходят в Telegram, а копятся в outbox: их показывает демо-чат в панели,
и через него можно пройти любой сценарий руками, как настоящий клиент.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import aiohttp

from shopbot import economy as E
from shopbot import texts as T
from shopbot.engine import STATUS_TITLES, parse_trade_url, parse_username
from shopbot.pricing import METHOD_TITLES, apply_method
from shopbot.suppliers import SupplierError

log = logging.getLogger("tg")


def kb(rows: list | None) -> dict | None:
    """[(text, data)] или [[(text, data), …], …]; data = "callback" или {"url": …}."""
    if not rows:
        return None
    if rows and isinstance(rows[0], tuple):
        rows = [rows]
    out = []
    for row in rows:
        line = []
        for text, data in row:
            if isinstance(data, dict):
                line.append({"text": text, **data})
            else:
                line.append({"text": text, "callback_data": str(data)[:64]})
        out.append(line)
    return {"inline_keyboard": out}


class ShopTelegram:
    def __init__(self, shop, token: str = "", api: str = "https://api.telegram.org"):
        self.shop = shop
        self.db = shop.db
        self.cfg = shop.cfg
        self.token = token
        self.api = api.rstrip("/")
        self.outbox: list[dict] = []
        self.offset = 0
        self._session: aiohttp.ClientSession | None = None
        shop.bot = self

    # ------------------------------------------------------------------ Bot API
    async def call(self, method: str, **params) -> Any:
        if not self.token:
            return None
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
        try:
            async with self._session.post(f"{self.api}/bot{self.token}/{method}", json=params) as r:
                d = await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.warning("Telegram %s: %s", method, e)
            return None
        if not d.get("ok"):
            log.warning("Telegram %s: %s", method, d.get("description"))
            return None
        return d.get("result")

    async def send(self, chat_id: int, text: str, buttons: list | None = None) -> None:
        markup = kb(buttons)
        self.outbox.append({"chat": int(chat_id), "text": text, "buttons": markup, "ts": time.time()})
        del self.outbox[:-2000]
        params: dict[str, Any] = {"chat_id": chat_id, "text": text[:4000], "parse_mode": "HTML",
                                  "disable_web_page_preview": True}
        if markup:
            params["reply_markup"] = markup
        await self.call("sendMessage", **params)

    async def send_invoice(self, chat_id: int, oid: str, title: str, description: str, stars: int) -> None:
        self.outbox.append({"chat": int(chat_id), "text": f"🧾 Счёт Telegram: {stars} ⭐ — {title}",
                            "buttons": None, "ts": time.time(), "invoice": oid})
        await self.call("sendInvoice", chat_id=chat_id, title=title[:32], description=description[:255],
                        payload=oid, currency="XTR", prices=[{"label": title[:32], "amount": int(stars)}])

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def run(self) -> None:
        if not self.token:
            return
        me = await self.call("getMe")
        if me:
            self.shop.db.kv_set("bot_me", {"username": me.get("username"), "id": me.get("id")})
        while True:
            ups = await self.call("getUpdates", offset=self.offset, timeout=30,
                                  allowed_updates=["message", "callback_query", "pre_checkout_query"])
            if ups is None:
                await asyncio.sleep(5)
                continue
            for u in ups:
                self.offset = u["update_id"] + 1
                try:
                    await self.handle(u)
                except Exception:  # noqa: BLE001
                    log.exception("обработка апдейта")

    def fees_text(self, r: dict) -> str:
        from shopbot.pricing import gift_star_rub, wallet_spread
        c, rt = self.cfg, self.shop.rtba
        lines = ["ℹ️ <b>Комиссии и курсы — честно</b>\n"]
        if rt.is_open("pay.wallet"):
            sp = wallet_spread(c)
            lines.append(f"💎 Крипта на кошелёк TON: курс USDT <b>{r['usdt_rub'] * (1 - sp / 100):.2f} ₽</b>, TON "
                         f"<b>{r['ton_rub'] * (1 - sp / 100):.2f} ₽</b> (биржа Bybit −{sp:g}%), других комиссий нет")
        if rt.is_open("pay.crypto"):
            fee = max(float(c.get("payments.crypto.fee_pct") or 0), float(c.get("payments.crypto.min_fee_pct") or 0))
            lines.append(f"🪙 @CryptoBot: курс Bybit — USDT <b>{r['usdt_rub']} ₽</b>; конвертация <b>+{fee:g}%</b>")
        if rt.is_open("pay.stars"):
            lines.append(f"⭐ Звёзды Telegram: 1 ⭐ = <b>{c.get('payments.stars.rub_per_star'):g} ₽</b>, без комиссии")
        if rt.is_open("pay.gift"):
            lines.append(f"🎁 Подарками: 1 ⭐ цены продажи подарка = <b>{gift_star_rub(c):.2f} ₽</b>")
        if rt.is_open("pay.skins"):
            lines.append(f"🎮 Скинами Steam: по ценам {c.get('payments.skins.market')} "
                         f"−{c.get('payments.skins.haircut_pct'):g}%")
        if rt.is_open("pay.card"):
            lines.append(f"💳 Карта: <b>+{c.get('payments.card.fee_pct'):g}%</b> — эквайринг банка")
        lines.append("👛 Баланс магазина: без комиссии\n")
        lines.append(f"💸 <b>Продажа нам</b>: комиссия <b>{c.get('buyback.fee_pct'):g}%</b>.")
        lines.append(f"🤝 Рефералка: <b>{c.get('referral.reward_pct'):g}%</b> от покупок друга на ваш бонусный баланс, "
                     f"другу −{c.get('referral.friend_discount_pct'):g}% на первый заказ.\n")
        lines.append("Перед оплатой вы всегда видите разбивку: цена товара, скидка, курс и итог.")
        return "\n".join(lines)

    async def setup_menu_button(self, url: str = "") -> None:
        """Кнопка меню в чате бота открывает мини-приложение (адрес даёт shopbot.miniapp, только https)."""
        url = (url or str(self.cfg.get("miniapp.url") or "")).strip()
        if not url.startswith("https://"):
            return
        text = str(self.cfg.get("miniapp.menu_text") or "Магазин")[:32]
        await self.call("setChatMenuButton", menu_button={"type": "web_app", "text": text, "web_app": {"url": url}})

    # ------------------------------------------------------------------ вход
    @property
    def bot_username(self) -> str:
        return str(self.cfg.get("telegram.bot_username") or (self.db.kv_get("bot_me") or {}).get("username")
                   or "your_shop_bot")

    def is_admin(self, uid: int) -> bool:
        return int(uid) in {int(x) for x in self.cfg.get("telegram.admin_chat_ids") or []}

    async def handle(self, u: dict) -> None:
        if "pre_checkout_query" in u:
            q = u["pre_checkout_query"]
            o = self.db.order(q.get("invoice_payload", ""))
            ok = o is not None and o["status"] in ("awaiting_payment", "awaiting_asset")
            await self.call("answerPreCheckoutQuery", pre_checkout_query_id=q["id"], ok=ok,
                            **({} if ok else {"error_message": "Счёт устарел, оформите заказ заново"}))
            return
        if "callback_query" in u:
            q = u["callback_query"]
            frm = q["from"]
            await self.call("answerCallbackQuery", callback_query_id=q["id"])
            self.db.touch_user(frm["id"], frm.get("username", ""), frm.get("first_name", ""),
                               frm.get("language_code", "ru"))
            await self.on_callback(frm["id"], str(q.get("data") or ""))
            return
        m = u.get("message")
        if not m:
            return
        frm = m.get("from") or {}
        if m.get("chat", {}).get("type") != "private":
            return
        if "successful_payment" in m:
            await self.on_star_payment(frm["id"], m["successful_payment"])
            return
        await self.on_text(frm["id"], frm.get("username", ""), frm.get("first_name", ""),
                           str(m.get("text") or ""), frm.get("language_code", "ru"))

    async def on_star_payment(self, uid: int, sp: dict) -> None:
        oid = sp.get("invoice_payload", "")
        o = self.db.order(oid)
        if o is None:
            return
        prov = self.shop.providers["stars"]
        if hasattr(prov, "paid"):
            prov.paid[o["payment_id"]] = sp.get("telegram_payment_charge_id", "")
        self.db.event(oid, "star_payment", charge=sp.get("telegram_payment_charge_id"), amount=sp.get("total_amount"))
        await self.shop.on_paid(oid)

    # ------------------------------------------------------------------ текст
    async def on_text(self, uid: int, username: str, first_name: str, text: str, lang: str = "ru") -> None:
        text = text.strip()
        if text.startswith("/start"):
            raw = text.split(maxsplit=1)[1] if " " in text else ""
            ref_code, src, arg = E.parse_start(raw)
            ref = None
            if ref_code:
                r = self.db.user_by_ref(ref_code)
                ref = r["id"] if r else None
            user, new = self.db.touch_user(uid, username, first_name, lang, referred_by=ref)
            if src:
                self.db.set_source(uid, src, float(self.cfg.get("sources.window_days") or 30))
            elif new and ref:
                self.db.set_source(uid, "ref", float(self.cfg.get("sources.window_days") or 30))
            if new and user["referred_by"]:
                await self.send(user["referred_by"], "🤝 По вашей ссылке пришёл новый друг!")
            self.db.set_session(uid, "")
            await self.menu(uid)
            if arg in ("stars", "gift", "premium", "steam_keys"):
                await self.on_callback(uid, f"buy:{arg}")
            elif arg in ("sell", "ref", "fees", "level"):
                await self.on_callback(uid, arg)
            return
        self.db.touch_user(uid, username, first_name, lang)
        if text in ("/menu", "/help"):
            self.db.set_session(uid, "")
            await self.menu(uid)
            return
        if text == "/balance":
            await self.on_callback(uid, "balance")
            return
        state, data = self.db.session(uid)
        if state == "review_text":
            self.db.set_session(uid, "")
            o = self.db.order(data.get("order", ""))
            r = self.db.one("SELECT rating FROM reviews WHERE order_id=?", (data.get("order", ""),))
            if o is not None and r is not None:
                await self.send(uid, await self.shop.add_review(uid, o["id"], int(r["rating"]), text),
                                [("◀️ В меню", "menu")])
                return
        if state == "stars_amount" or state == "sell_stars_amount" or state == "keys_qty":
            try:
                n = int(text.replace(" ", ""))
            except ValueError:
                await self.send(uid, "Пришлите число.")
                return
            if state == "stars_amount":
                await self.pick_amount(uid, "stars", "", n)
            elif state == "keys_qty":
                err = self.shop.validate_amount("steam_keys", n)
                if err:
                    await self.send(uid, err)
                    return
                self.db.set_session(uid, "trade_url", {"product": "steam_keys", "item": "", "amount": n})
                await self.send(uid, T.ASK_TRADE)
            else:
                await self.sell_stars_quote(uid, n)
            return
        if state == "fix_recipient" and (self.shop.db.order(data.get("order", "")) or {"product": ""})["product"] \
                == "steam_keys":
            url = parse_trade_url(text)
            if not url:
                await self.send(uid, "Это не похоже на ссылку обмена Steam. " + T.ASK_TRADE)
                return
            self.db.set_session(uid, "")
            self.db.exec("UPDATE users SET trade_url=? WHERE id=?", (url, uid))
            await self.shop.set_recipient(data["order"], url)
            return
        if state in ("recipient", "fix_recipient"):
            name = parse_username(text)
            if not name:
                await self.send(uid, T.NEED_USERNAME)
                return
            if state == "fix_recipient":
                self.db.set_session(uid, "")
                await self.shop.set_recipient(data["order"], name)
                return
            data["recipient"] = name
            await self.show_quote(uid, data)
            return
        if state == "trade_url":
            url = parse_trade_url(text)
            if not url:
                await self.send(uid, "Это не похоже на ссылку обмена Steam. " + T.ASK_TRADE)
                return
            self.db.exec("UPDATE users SET trade_url=? WHERE id=?", (url, uid))
            data["recipient"] = url
            await self.show_quote(uid, data)
            return
        await self.menu(uid)

    async def menu(self, uid: int) -> None:
        await self.send(uid, T.WELCOME.format(shop=self.cfg.get("general.shop_name")), T.MENU_BUTTONS)

    # ------------------------------------------------------------------ кнопки
    async def on_callback(self, uid: int, data: str) -> None:
        shop, rt = self.shop, self.shop.rtba
        parts = data.split(":")
        head = parts[0]
        if head == "menu":
            self.db.set_session(uid, "")
            await self.menu(uid)
        elif head == "buy":
            product = parts[1]
            if not rt.is_open(f"buy.{product}"):
                await self.send(uid, T.CLOSED, [("◀️ В меню", "menu")])
                return
            if product == "stars":
                c = self.cfg.get("catalog.stars")
                self.db.set_session(uid, "stars_amount", {"product": "stars"})
                denoms = [(f"{n} ⭐", f"st:{n}") for n in c["denominations"]]
                rows = [denoms[i:i + 4] for i in range(0, len(denoms), 4)] + [[("◀️ Назад", "menu")]]
                await self.send(uid, T.ASK_STARS.format(lo=c["min"], hi=c["max"]), rows)
            elif product == "gift":
                g = shop.gifts()
                btns = [(f"{x['emoji']} {x['title']} · {x['stars']}⭐", f"gf:{x['id']}") for x in g]
                rows = [btns[i:i + 2] for i in range(0, len(btns), 2)] + [[("◀️ Назад", "menu")]]
                await self.send(uid, "🎁 <b>Какой подарок отправить?</b>", rows)
            elif product == "premium":
                rows = [[(f"💎 {m} мес.", f"pm:{m}") for m in self.cfg.get("catalog.premium.months")],
                        [("◀️ Назад", "menu")]]
                await self.send(uid, "💎 <b>Telegram Premium</b> — на какой срок?", rows)
            elif product == "steam_keys":
                c = self.cfg.get("catalog.steam_keys")
                self.db.set_session(uid, "keys_qty", {"product": "steam_keys"})
                await self.send(uid, T.ASK_KEYS.format(title=c["title"], usd=c["steam_value_usd"], hi=c["max_qty"]),
                                [[("1", "kq:1"), ("3", "kq:3"), ("5", "kq:5"), ("10", "kq:10")],
                                 [("◀️ Назад", "menu")]])
        elif head == "st":
            await self.pick_amount(uid, "stars", "", int(parts[1]))
        elif head == "gf":
            g = shop.gift(parts[1])
            if g:
                await self.pick_amount(uid, "gift", g["id"], int(g["stars"]))
        elif head == "pm":
            await self.pick_amount(uid, "premium", parts[1], 1)
        elif head == "kq":
            self.db.set_session(uid, "trade_url", {"product": "steam_keys", "item": "", "amount": int(parts[1])})
            u = self.db.user(uid)
            if u["trade_url"]:
                await self.send(uid, T.ASK_TRADE, [("Моя прошлая ссылка", "tu:last")])
            else:
                await self.send(uid, T.ASK_TRADE)
        elif head == "tu":
            state, d = self.db.session(uid)
            d["recipient"] = self.db.user(uid)["trade_url"]
            await self.show_quote(uid, d)
        elif head == "to":
            state, d = self.db.session(uid)
            u = self.db.user(uid)
            if not u["username"]:
                await self.send(uid, "У вас нет @username в Telegram — пришлите получателя вручную.")
                return
            d["recipient"] = u["username"]
            await self.show_quote(uid, d)
        elif head == "pay":
            await self.pay(uid, parts[1], parts[2] if len(parts) > 2 else "USDT")
        elif head == "demo" and shop.demo:
            prov = next((p for p in shop.providers.values() if parts[2] in getattr(p, "payments", {})), None)
            if prov:
                prov.pay(parts[2])
                await shop.poll_payments()
        elif head == "sell":
            await self.sell_menu(uid, parts[1] if len(parts) > 1 else "")
        elif head == "ss":
            await self.sell_stars_quote(uid, int(parts[1]))
        elif head == "sellok":
            await self.sell_stars_create(uid)
        elif head == "sg":
            g = shop.gift(parts[1])
            if not g:
                return
            self.db.set_session(uid, "sell_gift", {"gift": g["id"]})
            rows = []
            if rt.is_open("payout.stars") and self.cfg.get("buyback.gifts.pay_stars"):
                q = shop.quote_sell("gift", 0, "stars", g["id"])
                rows.append((f"⭐ {int(q.net)} звёзд", "sp:stars"))
            if rt.is_open("payout.crypto") and self.cfg.get("buyback.gifts.pay_crypto"):
                q = shop.quote_sell("gift", 0, "usdt", g["id"])
                rows.append((f"🪙 {q.net:.2f} USDT", "sp:usdt"))
            if not rows:
                await self.send(uid, T.CLOSED)
                return
            await self.send(uid, f"{g['emoji']} <b>{g['title']}</b> — Telegram даёт за него {g['convert']} ⭐.\n"
                                 f"Как получить оплату?", [rows, [("◀️ Назад", "sell")]])
        elif head == "sp":
            state, d = self.db.session(uid)
            await self.sell_gift_create(uid, d.get("gift", ""), parts[1])
        elif head == "balance":
            await self.balance(uid)
        elif head == "wd":
            await self.send(uid, await shop.withdraw_stars(uid))
        elif head == "ref":
            await self.referral(uid)
        elif head == "level":
            await self.level(uid)
        elif head == "rv" and len(parts) == 3:
            oid, n = parts[1], int(parts[2]) if parts[2].isdigit() else 5
            msg = await shop.add_review(uid, oid, n)
            if "пару слов" in msg:
                self.db.set_session(uid, "review_text", {"order": oid})
            await self.send(uid, msg, [("◀️ В меню", "menu")])
        elif head == "fees":
            r = shop.rates.snapshot()
            await self.send(uid, self.fees_text(r), [("◀️ В меню", "menu")])
        elif head == "orders":
            await self.orders(uid)
        elif head == "support":
            sup = self.cfg.get("general.support_username") or "ответьте на это сообщение"
            await self.send(uid, T.SUPPORT.format(support=sup), [("◀️ В меню", "menu")])
        elif head == "adm" and self.is_admin(uid):
            action, oid = parts[1], parts[2]
            if action == "payout":
                res = await shop.payout(oid, approved_by=f"tg:{uid}")
            elif action == "reject":
                res = await shop.reject(oid)
            elif action == "delivered":
                res = await shop.admin_delivered(oid)
            elif action == "gift":
                res = await shop.confirm_gift_received(oid, who=f"tg:{uid}")
            elif action == "asset":
                res = await shop.confirm_asset(oid, who=f"tg:{uid}")
            else:
                res = "?"
            self.db.audit(f"tg:{uid}", f"adm_{action}", oid, res)
            await self.send(uid, f"#{oid}: {res}")

    # ------------------------------------------------------------------ шаги покупки
    async def pick_amount(self, uid: int, product: str, item: str, amount: int) -> None:
        err = self.shop.validate_amount(product, amount)
        if err:
            await self.send(uid, err)
            return
        self.db.set_session(uid, "recipient", {"product": product, "item": item, "amount": amount})
        await self.send(uid, T.ASK_RECIPIENT, [[("🙋 Себе", "to:me")], [("◀️ Назад", "menu")]])

    async def show_quote(self, uid: int, d: dict) -> None:
        shop = self.shop
        self.db.set_session(uid, "pay", d)
        user = self.db.user(uid)
        try:
            q = await shop.quote_buy(user, d["product"], d.get("item", ""), int(d["amount"]))
        except (SupplierError, ValueError) as e:
            await self.send(uid, f"{T.CLOSED}\n<i>{e}</i>", [("◀️ В меню", "menu")])
            return
        to = "ваш Steam (обмен)" if d["product"] == "steam_keys" else f"@{d['recipient']}"
        lines, rows = [], []
        for m in shop.methods_for(d["product"]):
            assets = (self.cfg.get(f"payments.{m}.assets") or ["USDT"]) if m in ("crypto", "wallet") else [""]
            for a in assets:
                qq = apply_method(self.cfg, type(q)(**{**q.__dict__, "lines": []}), m, shop.rates, a or "USDT")
                label = METHOD_TITLES[m] + (f" {a}" if a else "")
                amt = (f"{qq.pay_amount:g} {qq.asset}" if m in ("crypto", "wallet") else
                       f"{int(qq.pay_amount)} ⭐" if m == "stars" else
                       f"подарки на {int(qq.pay_amount)} ⭐" if m == "gift" else
                       f"скины на {qq.pay_amount:g} $" if m == "skins" else f"{qq.total_rub:.2f} ₽")
                fee = (f" (курс {qq.rate:.2f} ₽)" if m == "wallet" else
                       f" (комиссия {qq.fee_pct:g}%)" if qq.fee_pct else " (без комиссии)")
                lines.append(f"• {label}: <b>{amt}</b>{fee}")
                rows.append([(f"{label} · {amt}", f"pay:{m}:{a}" if a else f"pay:{m}")])
        if not rows:
            await self.send(uid, T.CLOSED, [("◀️ В меню", "menu")])
            return
        head = T.QUOTE_HEAD.format(title=q.title, to=to)
        price = f"Цена: <b>{q.price_rub:.2f} ₽</b>"
        if q.discount_rub:
            price += f" − скидка друга <b>{q.discount_rub:.2f} ₽</b>"
        rows.append([("◀️ Отмена", "menu")])
        await self.send(uid, head + price + "\n\n" + T.CHOOSE_METHOD.format(lines="\n".join(lines)), rows)

    async def pay(self, uid: int, method: str, asset: str) -> None:
        state, d = self.db.session(uid)
        if state != "pay" or not d.get("recipient"):
            await self.menu(uid)
            return
        try:
            o, pay = await self.shop.create_buy(uid, d["product"], d.get("item", ""), int(d["amount"]),
                                                d["recipient"], method, asset or "USDT")
        except (ValueError, SupplierError) as e:
            await self.send(uid, f"⚠️ {e}", [("◀️ В меню", "menu")])
            return
        if self.db.session(uid)[0] == "pay":     # оплата с баланса уже могла попросить исправить получателя
            self.db.set_session(uid, "")
        lines = "\n".join(f"{a}: <b>{b}</b>" for a, b in json.loads(o["breakdown"]))
        head = f"🧾 <b>Заказ #{o['id']}</b>\n{lines}\n\n"
        if method == "balance":
            return
        amount = (f"{o['pay_amount']:g} {o['pay_currency']}" if method in ("crypto", "wallet") else
                  f"{int(o['pay_amount'])} ⭐" if method == "stars" else f"{o['total_rub']:.2f} ₽")
        ttl = int(self.cfg.get(f"payments.{method}.ttl_minutes") or self.cfg.get("payments.order_ttl_minutes") or 30)
        rows = []
        if method == "wallet":
            text = T.PAY_WALLET.format(amount=amount, address=pay.get("address", ""), comment=pay.get("comment", ""),
                                       ttl=ttl)
            if pay.get("url"):
                rows.append([("💎 Открыть в Tonkeeper", {"url": pay["url"]})])
            await self.send(uid, head + text, rows or None)
        elif method == "gift":
            acc = self.cfg.get("payments.gift.account") or self.cfg.get("buyback.gifts.gift_account") or "@shop_gifts"
            await self.send(uid, head + T.PAY_GIFT.format(stars=int(o["pay_amount"]), account=acc, ttl=ttl, oid=o["id"]))
        elif method == "skins":
            await self.send(uid, head + T.PAY_SKINS.format(usd=o["pay_amount"], trade=self.cfg.get("payments.skins.trade_url"),
                                                           market=self.cfg.get("payments.skins.market"), ttl=ttl,
                                                           oid=o["id"]))
        elif pay.get("invoice"):
            await self.send(uid, head + T.PAY_LINK.format(amount=amount, ttl=ttl))
            await self.send_invoice(uid, o["id"], "Заказ " + o["id"], json.loads(o["breakdown"])[0][0],
                                    int(o["pay_amount"]))
        else:
            if pay.get("url", "").startswith("http"):
                rows.append([("💳 Оплатить " + amount, {"url": pay["url"]})])
            await self.send(uid, head + T.PAY_LINK.format(amount=amount, ttl=ttl), rows or None)
        if self.shop.demo:
            await self.send(uid, "🧪 Демо: платёж не настоящий.", [("✅ Оплатить (демо)", f"demo:pay:{pay['id']}")])

    # ------------------------------------------------------------------ выкуп
    async def sell_menu(self, uid: int, what: str) -> None:
        rt, c = self.shop.rtba, self.cfg
        if what == "stars":
            if not rt.is_open("sell.stars"):
                await self.send(uid, T.CLOSED, [("◀️ Назад", "sell")])
                return
            self.db.set_session(uid, "sell_stars_amount")
            await self.send(uid, T.SELL_STARS_ASK.format(lo=c.get("buyback.stars.min"), hi=c.get("buyback.stars.max")),
                            [[("500 ⭐", "ss:500"), ("1000 ⭐", "ss:1000"), ("5000 ⭐", "ss:5000")]])
            return
        if what == "gift":
            if not rt.is_open("sell.gift"):
                await self.send(uid, T.CLOSED, [("◀️ Назад", "sell")])
                return
            g = self.shop.gifts()
            btns = [(f"{x['emoji']} {x['title']}", f"sg:{x['id']}") for x in g]
            rows = [btns[i:i + 3] for i in range(0, len(btns), 3)] + [[("◀️ Назад", "sell")]]
            await self.send(uid, "🎁 <b>Какой подарок продаёте?</b>\n(уникальные/улучшенные — через поддержку, "
                                 "оценим вручную)", rows)
            return
        rows = [[("⭐ Продать звёзды", "sell:stars"), ("🎁 Продать подарок", "sell:gift")], [("◀️ В меню", "menu")]]
        await self.send(uid, T.SELL_MENU.format(usd=c.get("buyback.stars.usd_per_star"), fee=c.get("buyback.fee_pct")),
                        rows)

    async def sell_stars_quote(self, uid: int, n: int) -> None:
        lo, hi = int(self.cfg.get("buyback.stars.min")), int(self.cfg.get("buyback.stars.max"))
        if not lo <= n <= hi:
            await self.send(uid, f"Можно от {lo} до {hi} ⭐")
            return
        q = self.shop.quote_sell("stars", n, "usdt")
        self.db.set_session(uid, "sell_confirm", {"amount": n})
        lines = "\n".join(f"{a}: <b>{b}</b>" for a, b in q.lines)
        await self.send(uid, f"💸 <b>Продажа {n} ⭐</b>\n\n{lines}\n\nВыплата — в @CryptoBot на этот Telegram.",
                        [[("✅ Продать", "sellok")], [("◀️ Отмена", "menu")]])

    async def sell_stars_create(self, uid: int) -> None:
        state, d = self.db.session(uid)
        if state != "sell_confirm":
            return
        try:
            o, pay = await self.shop.create_sell(uid, "stars", int(d["amount"]), "usdt")
        except ValueError as e:
            await self.send(uid, f"⚠️ {e}")
            return
        self.db.set_session(uid, "")
        await self.send(uid, f"Оплатите счёт на <b>{int(o['pay_amount'])} ⭐</b> — это и есть передача звёзд "
                             f"магазину. Выплата {o['payout_amount']:.2f} USDT придёт сразу после оплаты.")
        await self.send_invoice(uid, o["id"], f"Продажа {int(o['amount'])} ⭐", "Передача звёзд магазину",
                                int(o["pay_amount"]))
        if self.shop.demo:
            await self.send(uid, "🧪 Демо: счёт не настоящий.", [("✅ Оплатить звёздами (демо)", f"demo:pay:{pay['id']}")])

    async def sell_gift_create(self, uid: int, gid: str, payout: str) -> None:
        try:
            o, info = await self.shop.create_sell(uid, "gift", 0, payout, gid)
        except ValueError as e:
            await self.send(uid, f"⚠️ {e}")
            return
        self.db.set_session(uid, "")
        g = self.shop.gift(gid) or {}
        lines = "\n".join(f"{a}: <b>{b}</b>" for a, b in json.loads(o["breakdown"]))
        await self.send(uid, T.SELL_GIFT_HOW.format(gift=f"{g.get('emoji', '')} {g.get('title', gid)}",
                                                    account=info.get("gift_account"), oid=o["id"], lines=lines))
        await self.shop.notify("INFO", "gift_incoming", f"Выкуп #{o['id']}: ждём {g.get('emoji', '')} "
                               f"{g.get('title', gid)} от {uid}", [("✔ Подарок пришёл", f"adm:gift:{o['id']}")])

    # ------------------------------------------------------------------ кабинет
    async def balance(self, uid: int) -> None:
        u = self.db.user(uid)
        ops = self.db.q("SELECT * FROM ledger WHERE user_id=? ORDER BY id DESC LIMIT 8", (uid,))
        unit = {"rub": "₽", "bonus": "₽ бонус", "stars": "⭐", "usdt": "USDT"}
        lines = "\n".join(f"{time.strftime('%d.%m %H:%M', time.localtime(r['ts']))} "
                          f"{'+' if r['delta'] > 0 else ''}{r['delta']:g} {unit[r['account']]} — {r['note'] or r['kind']}"
                          for r in ops) or "пока пусто"
        rows = [[("⭐ Вывести звёзды", "wd:stars")]] if int(u["stars_credit"] or 0) >= 50 else []
        rows.append([("◀️ В меню", "menu")])
        await self.send(uid, T.BALANCE.format(rub=u["balance_rub"], bonus=u["bonus_rub"], stars=u["stars_credit"],
                                              ops=lines), rows)

    async def referral(self, uid: int) -> None:
        u = self.db.user(uid)
        n = self.db.scalar("SELECT COUNT(*) FROM users WHERE referred_by=?", (uid,), 0)
        earned = float(self.db.scalar("SELECT SUM(delta) FROM ledger WHERE user_id=? AND kind='ref_reward'",
                                      (uid,), 0) or 0)
        link = f"https://t.me/{self.bot_username}?start=ref_{u['ref_code']}"
        c = self.cfg
        await self.send(uid, T.REF.format(link=link, pct=c.get("referral.reward_pct"),
                                          disc=c.get("referral.friend_discount_pct"),
                                          dmax=c.get("referral.friend_discount_max_rub"), n=n, earned=earned),
                        [[("📤 Поделиться", {"url": f"https://t.me/share/url?url={link}"})], [("◀️ В меню", "menu")]])

    async def level(self, uid: int) -> None:
        u = self.db.user(uid)
        t = E.tier_for(self.cfg, self.db, u)
        tiers = sorted(self.cfg.get("loyalty.tiers") or [], key=lambda x: float(x.get("min_rub") or 0))
        table = "\n".join(f"{'▶️' if x['id'] == t.id else '▫️'} <b>{x.get('title')}</b> от {float(x.get('min_rub') or 0):,.0f} ₽: "
                          f"−{x.get('discount_pct', 0):g}% к покупкам, +{x.get('buyback_bonus_pct', 0):g}% к выкупу"
                          .replace(",", " ") for x in tiers)
        nxt = (f"\nДо уровня «{t.next_title}» осталось <b>{max(0.0, t.next_rub - t.turnover):,.0f} ₽</b>."
               .replace(",", " ") if t.next_title else "\nУ вас максимальный уровень 🐳")
        rv = E.review_stats(self.db)
        await self.send(uid, f"🏅 <b>Ваш уровень: {t.title}</b>\nОборот за "
                             f"{self.cfg.get('loyalty.window_days') or 30} дн.: <b>{t.turnover:,.0f} ₽</b>"
                             .replace(",", " ") + nxt + "\n\n" + table +
                        (f"\n\n⭐ Рейтинг магазина: <b>{rv['avg']:g}</b> ({rv['count']} отзывов)" if rv["count"] else "")
                        + "\n\nКрупный объём? Напишите в поддержку — обсудим персональные условия.",
                        [("◀️ В меню", "menu")])

    async def orders(self, uid: int) -> None:
        rows = self.db.q("SELECT * FROM orders WHERE user_id=? ORDER BY created_at DESC LIMIT 10", (uid,))
        if not rows:
            await self.send(uid, T.ORDERS_EMPTY, [("◀️ В меню", "menu")])
            return
        lines = []
        for o in rows:
            what = self.shop.product_title(o["product"], o["item"], o["amount"])
            money = (f"{o['total_rub']:.2f} ₽" if o["kind"] == "buy" else
                     f"→ {o['payout_amount']:g} {'⭐' if o['payout_kind'] == 'stars' else o['payout_asset']}")
            lines.append(f"<b>#{o['id']}</b> {'🛒' if o['kind'] == 'buy' else '💸'} {what} · {money} · "
                         f"{STATUS_TITLES.get(o['status'], o['status'])}")
        await self.send(uid, "🧾 <b>Мои заказы</b>\n\n" + "\n".join(lines), [("◀️ В меню", "menu")])
