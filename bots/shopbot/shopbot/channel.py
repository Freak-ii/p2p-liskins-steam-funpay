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

POSTS = [("open", "Кнопка «Открыть магазин» (закреп, без цен)"), ("showcase", "Витрина"), ("prices", "Цены на сегодня"), ("sell", "Продать нам"),
         ("referral", "Рефералка")]


class Channel:
    def __init__(self, shop):
        self.shop = shop
        self.db = shop.db
        self.cfg = shop.cfg
        shop.channel = self

    def link(self, payload: str) -> str:
        return f"https://t.me/{self.shop.bot.bot_username if self.shop.bot else 'your_shop_bot'}?start={payload}"

    def app_link(self) -> str:
        """Ссылка, открывающая мини-приложение сразу (Main Mini App в BotFather)."""
        return f"https://t.me/{self.shop.bot.bot_username if self.shop.bot else 'your_shop_bot'}?startapp"

    async def ensure_open_post(self) -> str:
        """Пост с кнопкой «Открыть магазин» — опубликовать и закрепить один раз (channel.open_post.auto).
        Внизу канала Telegram кнопку бота не даёт, поэтому кнопка живёт под закреплённым постом."""
        if not self.cfg.get("channel.open_post.auto") or not self.cfg.get("channel.chat_id"):
            return "выключено"
        row = self.db.one("SELECT * FROM channel_posts WHERE id='open'")
        if row is not None and row["message_id"]:
            return "уже опубликован"
        res = await self.publish("open", force=True)
        row = self.db.one("SELECT * FROM channel_posts WHERE id='open'")
        if row is not None and row["message_id"] and self.cfg.get("channel.open_post.pin", True):
            await self.shop.bot.call("pinChatMessage", chat_id=self.cfg.get("channel.chat_id"),
                                     message_id=row["message_id"], disable_notification=True)
        return res

    def hidden(self) -> set[str]:
        """Товары, снятые в Пульте Орбиты центра: их нет в постах и описаниях канала."""
        orbit = getattr(self.shop, "orbit", None)
        return orbit.post_hidden() if orbit is not None else set()

    async def price_rows(self) -> tuple[list[str], dict]:
        shop, rows, mins = self.shop, [], {}
        hidden = self.hidden()
        try:
            q = await shop.suppliers["stars"].quote("stars", 100)
            per = float(q["cost_rub"]) / 100 if q.get("available") else None
        except SupplierError:
            per = None
        if per and shop.rtba.is_open("buy.stars") and "stars" not in hidden:
            for n in [x for x in self.cfg.get("catalog.stars.denominations") or [] if x in (50, 100, 250, 500, 1000)]:
                price, _ = shop_price(self.cfg, "stars", per * n, n)
                rows.append(f"⭐ {n} — <b>{price:.0f} ₽</b>")
                mins.setdefault("stars", price)
        for months in self.cfg.get("catalog.premium.months") or []:
            try:
                q = await shop.suppliers["premium"].quote("premium", 0, int(months))
            except SupplierError:
                break
            if q.get("available") and shop.rtba.is_open("buy.premium") and "premium" not in hidden:
                price, _ = shop_price(self.cfg, "premium", float(q["cost_rub"]), 1)
                rows.append(f"💎 Premium {months} мес. — <b>{price:.0f} ₽</b>")
                mins.setdefault("premium", price)
        gifts = sorted(shop.gifts(), key=lambda g: g["stars"])
        if gifts and shop.rtba.is_open("buy.gift") and "gift" not in hidden:
            try:
                q = await shop.suppliers["gifts"].quote("gift", int(gifts[0]["stars"]), gift=gifts[0])
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
        hidden = self.hidden()
        open_text = T.POST_OPEN.format(shop=shop_name)
        if hidden:
            open_text = open_text.replace("Telegram Stars, подарки, Premium и ключи Steam", T.goods_text(hidden), 1)
        showcase = T.strip_lines(T.POST_SHOWCASE.format(shop=shop_name, stars_from=f"{mins.get('stars', 0):.0f}",
                                                        gift_from=f"{mins.get('gift', 0):.0f}",
                                                        prem_from=f"{mins.get('premium', 0):.0f}"),
                                 hidden, T.SHOWCASE_LINE)
        goods = [(t, {"url": self.link(p)}) for p, t in (("stars", "⭐ Звёзды"), ("gift", "🎁 Подарки"),
                                                          ("premium", "💎 Premium"), ("steam_keys", "🎮 Steam"))
                 if p not in hidden]
        posts = {
            "open": (open_text, [[("🛒 Открыть магазин", {"url": self.app_link()})]]),
            "showcase": (showcase, [goods[i:i + 2] for i in range(0, len(goods), 2)] +
                         [[("💸 Продать звёзды или подарок", {"url": self.link("sell")})]]),
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

    async def publish(self, pid: str, force: bool = False) -> str:
        # force — только для поста «open» без цен (его разрешает channel.open_post.auto)
        if not self.cfg.get("channel.chat_id") or not (self.cfg.get("channel.enabled") or (force and pid == "open")):
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

    async def refresh_published(self) -> str:
        """Пересобрать посты и поправить в канале те опубликованные, у которых изменился текст (Пульт Орбиты)."""
        before = {r["id"]: r["text"] for r in self.db.q("SELECT id, text FROM channel_posts")}
        posts = await self.build()
        if not self.cfg.get("channel.chat_id") or self.shop.bot is None or not getattr(self.shop.bot, "token", ""):
            return "ok"
        from shopbot.tg import kb
        edited = 0
        for p in posts:
            if p.get("message_id") and p.get("status") == "published" and before.get(p["id"]) != p["text"]:
                res = await self.shop.bot.call("editMessageText", chat_id=self.cfg.get("channel.chat_id"),
                                               message_id=p["message_id"], text=p["text"], parse_mode="HTML",
                                               reply_markup=kb(p["buttons"]), disable_web_page_preview=True)
                if not res:
                    return f"error: Telegram не принял правку поста «{p['title']}»"
                edited += 1
        return f"ok: поправлено постов {edited}" if edited else "ok"

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
