"""Канал магазина: витрина, прайс, выкуп, рефералка — посты в стиле Tribute с кнопками-диплинками в бот.

Посты собираются как черновики (видно в панели «Канал»). В Telegram уходят только когда владелец включил
channel.enabled, указал chat_id канала (бот — админ канала) и нажал «Опубликовать». Пост с ценами RTBA потом
обновляет сам (editMessageText), если цена сдвинулась больше чем на channel.reprice_threshold_pct.
"""
from __future__ import annotations

import json
import time

from shopbot import texts as T
from shopbot.pricing import shop_price
from shopbot.suppliers import SupplierError

POSTS = [("showcase", "Витрина (закреп)"), ("prices", "Цены на сегодня"), ("sell", "Продать нам"),
         ("referral", "Рефералка")]


class Channel:
    def __init__(self, shop):
        self.shop = shop
        self.db = shop.db
        self.cfg = shop.cfg
        shop.channel = self

    def link(self, payload: str) -> str:
        return f"https://t.me/{self.shop.bot.bot_username if self.shop.bot else 'your_shop_bot'}?start={payload}"

    async def price_rows(self) -> tuple[list[str], dict]:
        shop, rows, mins = self.shop, [], {}
        try:
            q = await shop.suppliers["stars"].quote("stars", 100)
            per = float(q["cost_rub"]) / 100 if q.get("available") else None
        except SupplierError:
            per = None
        if per and shop.rtba.is_open("buy.stars"):
            for n in [x for x in self.cfg.get("catalog.stars.denominations") or [] if x in (50, 100, 250, 500, 1000)]:
                price, _ = shop_price(self.cfg, "stars", per * n, n)
                rows.append(f"⭐ {n} — <b>{price:.0f} ₽</b>")
                mins.setdefault("stars", price)
        for months in self.cfg.get("catalog.premium.months") or []:
            try:
                q = await shop.suppliers["premium"].quote("premium", 0, int(months))
            except SupplierError:
                break
            if q.get("available") and shop.rtba.is_open("buy.premium"):
                price, _ = shop_price(self.cfg, "premium", float(q["cost_rub"]), 1)
                rows.append(f"💎 Premium {months} мес. — <b>{price:.0f} ₽</b>")
                mins.setdefault("premium", price)
        gifts = sorted(shop.gifts(), key=lambda g: g["stars"])
        if gifts and shop.rtba.is_open("buy.gift"):
            try:
                q = await shop.suppliers["gifts"].quote("gift", int(gifts[0]["stars"]))
                if q.get("available"):
                    price, _ = shop_price(self.cfg, "gift", float(q["cost_rub"]), int(gifts[0]["stars"]))
                    rows.append(f"🎁 Подарки — от <b>{price:.0f} ₽</b>")
                    mins["gift"] = price
            except SupplierError:
                pass
        return rows, mins

    async def build(self) -> list[dict]:
        rows, mins = await self.price_rows()
        c = self.cfg
        shop_name = c.get("general.shop_name")
        posts = {
            "showcase": (T.POST_SHOWCASE.format(shop=shop_name, stars_from=f"{mins.get('stars', 0):.0f}",
                                                gift_from=f"{mins.get('gift', 0):.0f}",
                                                prem_from=f"{mins.get('premium', 0):.0f}"),
                         [[("⭐ Звёзды", {"url": self.link("stars")}), ("🎁 Подарки", {"url": self.link("gift")})],
                          [("💎 Premium", {"url": self.link("premium")}), ("🎮 Steam", {"url": self.link("steam_keys")})],
                          [("💸 Продать звёзды или подарок", {"url": self.link("sell")})]]),
            "prices": (T.POST_PRICES.format(rows="\n".join(rows) or "—"),
                       [[("🛒 Купить в боте", {"url": self.link("stars")})]]),
            "sell": (T.POST_SELL.format(fee=c.get("buyback.fee_pct")), [[("💸 Продать", {"url": self.link("sell")})]]),
            "referral": (T.POST_REF.format(pct=c.get("referral.reward_pct"), disc=c.get("referral.friend_discount_pct")),
                         [[("🤝 Получить ссылку", {"url": self.link("ref")})]]),
        }
        out = []
        now = time.time()
        for pid, title in POSTS:
            text, buttons = posts[pid]
            row = self.db.one("SELECT * FROM channel_posts WHERE id=?", (pid,))
            if row is None:
                self.db.exec("INSERT INTO channel_posts(id,title,text,buttons,updated_at) VALUES(?,?,?,?,?)",
                             (pid, title, text, json.dumps(buttons, ensure_ascii=False), now))
            elif row["text"] != text:
                self.db.exec("UPDATE channel_posts SET text=?, buttons=?, updated_at=? WHERE id=?",
                             (text, json.dumps(buttons, ensure_ascii=False), now, pid))
            out.append({"id": pid, "title": title, "text": text, "buttons": buttons,
                        **{k: (row[k] if row else None) for k in ("message_id", "status", "published_at")}})
        self.db.kv_set("price_mins", mins)
        return out

    async def publish(self, pid: str) -> str:
        if not (self.cfg.get("channel.enabled") and self.cfg.get("channel.chat_id")):
            return "канал не подключён: включите channel.enabled и укажите chat_id (бот — админ канала)"
        if self.shop.bot is None or not self.shop.bot.token:
            return "нет токена бота"
        posts = {p["id"]: p for p in await self.build()}
        p = posts.get(pid)
        if p is None:
            return "нет такого поста"
        from shopbot.tg import kb
        res = await self.shop.bot.call("sendMessage", chat_id=self.cfg.get("channel.chat_id"), text=p["text"],
                                       parse_mode="HTML", reply_markup=kb(p["buttons"]),
                                       disable_web_page_preview=True)
        if not res:
            return "Telegram не принял пост (бот админ канала?)"
        self.db.exec("UPDATE channel_posts SET message_id=?, status='published', published_at=? WHERE id=?",
                     (res["message_id"], time.time(), pid))
        return f"опубликовано, message_id {res['message_id']}"

    async def autoupdate(self) -> None:
        """RTBA-пункт channel.autopost: обновить опубликованный пост с ценами, когда цены заметно сдвинулись."""
        if not self.shop.rtba.is_open("channel.autopost") or not self.cfg.get("channel.autoupdate_price_post"):
            return
        old = self.db.kv_get("price_mins") or {}
        posts = {p["id"]: p for p in await self.build()}
        new = self.db.kv_get("price_mins") or {}
        thr = float(self.cfg.get("channel.reprice_threshold_pct") or 3) / 100
        moved = any(abs(new.get(k, 0) - v) > v * thr for k, v in old.items() if v)
        row = self.db.one("SELECT * FROM channel_posts WHERE id='prices'")
        if not moved or not row or not row["message_id"]:
            return
        from shopbot.tg import kb
        await self.shop.bot.call("editMessageText", chat_id=self.cfg.get("channel.chat_id"),
                                 message_id=row["message_id"], text=posts["prices"]["text"], parse_mode="HTML",
                                 reply_markup=kb(posts["prices"]["buttons"]))
        self.shop.rtba.log("channel.autopost", "price_post_updated", "цены сдвинулись", old=old, new=new)
