"""RTBA магазина: контроль каждого пункта в реальном времени.

Пункт — это товар, способ оплаты, выплата или функция (рефералка, автопост). У каждого режим:
  auto — RTBA сам открывает и закрывает пункт по проверкам; on — открыт принудительно (жёсткие запреты —
  нет настроек, выключен в каталоге — всё равно действуют); off — закрыт владельцем.
Каждое открытие/закрытие пишется в журнал rtba_log и шлётся админам; закрытый пункт пропадает из меню бота,
а заказы по нему не принимаются. Плюс проверки каждой операции: лимиты клиента, размер заказа, выплаты.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

log = logging.getLogger("rtba")

POINTS: list[tuple[str, str, str]] = [
    ("buy.stars", "Продажа звёзд", "Товары"), ("buy.gift", "Продажа подарков", "Товары"),
    ("buy.premium", "Продажа Premium", "Товары"), ("buy.steam_keys", "Ключи для Steam", "Товары"),
    ("sell.stars", "Выкуп звёзд за крипту", "Выкуп"), ("sell.gift", "Выкуп подарков", "Выкуп"),
    ("pay.wallet", "Крипта на общий кошелёк", "Оплата"), ("pay.gift", "Оплата подарками", "Оплата"),
    ("pay.skins", "Оплата скинами Steam", "Оплата"),
    ("pay.card", "Оплата картой (эквайринг)", "Оплата"), ("pay.crypto", "Оплата криптой (@CryptoBot)", "Оплата"),
    ("pay.stars", "Оплата звёздами", "Оплата"), ("pay.balance", "Оплата с баланса", "Оплата"),
    ("payout.crypto", "Выплаты криптой", "Выплаты"), ("payout.stars", "Выплаты звёздами", "Выплаты"),
    ("referral", "Реферальная программа", "Функции"), ("channel.autopost", "Автопост цен в канал", "Функции"),
]
TITLES = {k: t for k, t, _ in POINTS}
PRODUCT_SUPPLIER = {"stars": "stars", "gift": "gifts", "premium": "premium", "steam_keys": "steam_keys"}
CATALOG_KEY = {"stars": "stars", "gift": "gifts", "premium": "premium", "steam_keys": "steam_keys"}


@dataclass
class Check:
    ok: bool
    reason: str = ""
    hard: bool = False          # жёсткий запрет: режим «on» его не обходит
    value: str = ""


class RTBA:
    def __init__(self, shop):
        self.shop = shop
        self.cfg = shop.cfg
        self.db = shop.db
        now = time.time()
        for key, _, _ in POINTS:
            self.db.exec("INSERT OR IGNORE INTO rtba_points(key,mode,state,checked_at,changed_at) VALUES(?,?,?,?,?)",
                         (key, "auto", "open", now, now))

    # ------------------------------------------------------------------ состояние
    def point(self, key: str):
        return self.db.one("SELECT * FROM rtba_points WHERE key=?", (key,))

    def points(self) -> list[dict]:
        rows = {r["key"]: r for r in self.db.q("SELECT * FROM rtba_points")}
        out = []
        for key, title, group in POINTS:
            r = rows.get(key)
            out.append({"key": key, "title": title, "group": group, "mode": r["mode"] if r else "auto",
                        "state": r["state"] if r else "open", "reason": r["reason"] if r else "",
                        "value": r["value"] if r else "", "checked_at": r["checked_at"] if r else None,
                        "changed_at": r["changed_at"] if r else None})
        return out

    def is_open(self, key: str) -> bool:
        r = self.point(key)
        return r is not None and r["state"] == "open"

    def set_mode(self, key: str, mode: str, who: str = "admin") -> None:
        if key not in TITLES or mode not in ("auto", "on", "off"):
            raise ValueError("неизвестный пункт или режим")
        self.db.exec("UPDATE rtba_points SET mode=? WHERE key=?", (mode, key))
        self.log(key, f"mode:{mode}", f"режим изменил {who}")

    def log(self, key: str, decision: str, reason: str, **data) -> None:
        self.db.exec("INSERT INTO rtba_log(ts,key,decision,reason,data) VALUES(?,?,?,?,?)",
                     (time.time(), key, decision, reason, json.dumps(data, ensure_ascii=False, default=str)))

    async def _apply(self, key: str, check: Check) -> None:
        r = self.point(key)
        mode = r["mode"] if r else "auto"
        if mode == "off":
            state, reason = "closed", "выключено владельцем"
        elif mode == "on":
            state, reason = ("closed", check.reason) if (not check.ok and check.hard) else ("open", check.reason)
        else:
            state, reason = ("open", check.reason) if check.ok else ("closed", check.reason)
        now = time.time()
        changed = r is None or r["state"] != state
        self.db.exec("UPDATE rtba_points SET state=?, reason=?, value=?, checked_at=?, changed_at=CASE WHEN ? "
                     "THEN ? ELSE changed_at END WHERE key=?",
                     (state, reason, check.value, now, int(changed), now, key))
        if changed and r is not None:
            self.log(key, state, reason)
            word = "✅ открыт" if state == "open" else "⛔ закрыт"
            await self.shop.notify("WARNING" if state == "closed" else "INFO", f"rtba_{key}",
                                   f"RTBA: «{TITLES[key]}» {word}" + (f" — {reason}" if reason else ""))

    # ------------------------------------------------------------------ проверки пунктов
    def recent_fails(self, supplier: str) -> int:
        window = float(self.cfg.get("rtba.fail_window_minutes") or 60) * 60
        return int(self.db.scalar("SELECT COUNT(*) FROM supplier_fails WHERE supplier=? AND ts>=?",
                                  (supplier, time.time() - window), 0))

    def supplier_failed(self, supplier: str, error: str) -> None:
        self.db.exec("INSERT INTO supplier_fails(ts,supplier,error) VALUES(?,?,?)", (time.time(), supplier, error[:300]))

    async def check_product(self, product: str) -> Check:
        if not self.cfg.get(f"catalog.{CATALOG_KEY[product]}.enabled"):
            return Check(False, "выключено в каталоге", hard=True)
        sup_name = PRODUCT_SUPPLIER[product]
        fails, limit = self.recent_fails(sup_name), int(self.cfg.get("rtba.fail_threshold") or 3)
        if fails >= limit:
            last = self.db.scalar("SELECT MAX(ts) FROM supplier_fails WHERE supplier=?", (sup_name,), 0)
            if time.time() - float(last) < float(self.cfg.get("rtba.reopen_after_minutes") or 15) * 60:
                return Check(False, f"{fails} сбоя поставщика за час — пауза, проверю снова через "
                                    f"{self.cfg.get('rtba.reopen_after_minutes')} мин")
        probe = {"stars": (100, 0), "gift": (50, 0), "premium": (0, 3), "steam_keys": (1, 0)}[product]
        try:
            q = await self.shop.suppliers[sup_name].quote("gift" if product == "gift" else product, *probe)
        except Exception as e:  # noqa: BLE001
            return Check(False, f"поставщик недоступен: {e}")
        if not q.get("available"):
            return Check(False, f"у поставщика нет товара: {q.get('reason') or 'недоступно'}")
        from shopbot.pricing import shop_price
        price, why = shop_price(self.cfg, product, float(q["cost_rub"]), probe[0] or 1)
        unit = {"stars": "100 ⭐", "gift": "подарок 50 ⭐", "premium": "3 мес", "steam_keys": "1 ключ"}[product]
        return Check(True, why, value=f"{unit} = {price:.2f} ₽")

    async def check_payment(self, method: str) -> Check:
        if not self.cfg.get(f"payments.{method}.enabled"):
            return Check(False, "выключено в настройках", hard=True)
        if method == "balance":
            return Check(True)
        prov = self.shop.providers.get(method)
        if prov is None or not prov.configured:
            return Check(False, "не настроен провайдер", hard=True)
        if method == "wallet":
            if not self.shop.rates.fresh:
                return Check(False, f"курс Bybit устарел ({self.shop.rates.age_minutes:.0f} мин)", hard=True)
            from shopbot.pricing import wallet_spread
            return Check(True, value=f"спред {wallet_spread(self.cfg):g}% · {prov.address[:6]}…{prov.address[-4:]}")
        if method == "gift":
            return Check(True, value=f"на {self.cfg.get('payments.gift.account') or self.cfg.get('buyback.gifts.gift_account')}")
        if method == "skins":
            if not self.shop.rates.fresh:
                return Check(False, "курс USDT устарел", hard=True)
            return Check(True, value=f"скидка оценки {self.cfg.get('payments.skins.haircut_pct')}%")
        if method == "crypto" and not self.shop.rates.fresh:
            return Check(False, f"курс Bybit устарел ({self.shop.rates.age_minutes:.0f} мин)", hard=True)
        if method == "crypto":
            r = self.shop.rates.snapshot()
            return Check(True, value=f"USDT {r['usdt_rub']} ₽ · TON {r['ton_rub']} ₽")
        if method == "card":
            test = getattr(prov, "test", False)
            return Check(True, "тестовый режим ЮKassa" if test else "", value="тест" if test else "бой")
        return Check(True)

    async def treasury(self) -> float | None:
        prov = self.shop.providers.get("crypto")
        if prov is None or not prov.configured or not hasattr(prov, "balance"):
            return None
        try:
            bal = await prov.balance(self.cfg.get("buyback.payout_asset") or "USDT")
        except Exception:  # noqa: BLE001
            return None
        self.db.kv_set("treasury", {"usdt": bal, "ts": time.time()})
        return bal

    def paid_out_today(self) -> float:
        return float(self.db.scalar("SELECT SUM(payout_amount) FROM orders WHERE kind='sell' AND payout_kind='usdt' "
                                    "AND status IN ('paid_out','payout_pending','received') AND created_at>=?",
                                    (time.time() - 86400,), 0) or 0)

    async def check_payout_crypto(self) -> Check:
        bal = await self.treasury()
        if bal is None:
            return Check(False, "нет кошелька выплат (Crypto Pay)", hard=True)
        reserve = float(self.cfg.get("rtba.payout_reserve_usdt") or 0)
        if bal <= reserve:
            return Check(False, f"на балансе выплат {bal:.2f} USDT ≤ резерва {reserve:g}", value=f"{bal:.2f} USDT")
        return Check(True, value=f"{bal:.2f} USDT")

    async def check_sell(self, product: str) -> Check:
        key = "stars" if product == "stars" else "gifts"
        if not self.cfg.get(f"buyback.{key}.enabled"):
            return Check(False, "выкуп выключен", hard=True)
        cap = float(self.cfg.get("buyback.daily_cap_usdt") or 0)
        today = self.paid_out_today()
        if cap and today >= cap:
            return Check(False, f"дневной лимит выкупа {cap:g} USDT исчерпан")
        if product == "stars" and not self.is_open("payout.crypto"):
            return Check(False, "закрыты выплаты криптой")
        if product == "gift" and not (self.is_open("payout.crypto") or self.is_open("payout.stars")):
            return Check(False, "закрыты выплаты")
        if product == "gift" and not self.cfg.get("buyback.gifts.gift_account") and not self.shop.demo:
            return Check(False, "не указан аккаунт для приёма подарков", hard=True)
        return Check(True, value=f"за сутки {today:.2f}/{cap:g} USDT")

    async def tick(self) -> None:
        for product in ("stars", "gift", "premium", "steam_keys"):
            await self._apply(f"buy.{product}", await self.check_product(product))
        for method in ("wallet", "gift", "skins", "card", "crypto", "stars", "balance"):
            await self._apply(f"pay.{method}", await self.check_payment(method))
        await self._apply("payout.crypto", await self.check_payout_crypto())
        await self._apply("payout.stars", Check(self.is_open("buy.stars"), "" if self.is_open("buy.stars")
                                                else "звёзды у поставщика недоступны"))
        for product in ("stars", "gift"):
            await self._apply(f"sell.{product}", await self.check_sell(product))
        await self._apply("referral", Check(bool(self.cfg.get("referral.enabled")), "", hard=True,
                                            value=f"{self.cfg.get('referral.reward_pct')}%"))
        ch_ok = bool(self.cfg.get("channel.enabled") and self.cfg.get("channel.chat_id"))
        await self._apply("channel.autopost", Check(ch_ok, "" if ch_ok else "канал не подключён", hard=True))
        await self.refresh_xtr()
        self.db.kv_set("rtba_tick", time.time())

    async def refresh_xtr(self) -> None:
        """Запас звёзд на балансе бота (getMyStarBalance): от него зависит курс выкупа (economy.buyback_usd_per_star)."""
        bot = self.shop.bot
        if bot is None or self.shop.demo or not getattr(bot, "token", ""):
            return
        last = self.db.kv_get("xtr_balance") or {}
        if time.time() - float(last.get("ts") or 0) < 600:
            return
        try:
            r = await bot.call("getMyStarBalance")
        except Exception:  # noqa: BLE001 — метод может отсутствовать у старого Bot API
            return
        if isinstance(r, dict) and "amount" in r:
            self.db.kv_set("xtr_balance", {"stars": int(r["amount"]), "ts": time.time()})

    # ------------------------------------------------------------------ проверки операций
    def check_order(self, user, product: str, total_rub: float, method: str, *, limit_mult: float = 1.0,
                    recipient: str = "") -> Check:
        if user is None:
            return Check(False, "клиент не найден")
        if user["blocked"]:
            return Check(False, "клиент заблокирован")
        if not self.is_open(f"buy.{product}"):
            return Check(False, f"«{TITLES[f'buy.{product}']}» сейчас недоступно")
        if not self.is_open(f"pay.{method}"):
            return Check(False, f"«{TITLES[f'pay.{method}']}» сейчас недоступно")
        mx = float(self.cfg.get("rtba.max_order_rub") or 0)
        if mx and total_rub > mx:
            return Check(False, f"заказ больше {mx:g} ₽ — напишите в поддержку")
        new = time.time() - float(user["created_at"] or 0) < 86400 and not user["first_order_at"]
        limit = float(self.cfg.get("rtba.new_user_daily_rub" if new else "rtba.user_daily_rub") or 0)
        if not new:
            limit *= max(1.0, float(limit_mult or 1))          # киты: лимит уровня лояльности
        mr = int(self.cfg.get("antiscam.max_recipients_per_day") or 0)
        if mr and recipient and product in ("stars", "gift", "premium"):
            rows = self.db.q("SELECT DISTINCT lower(recipient) r FROM orders WHERE user_id=? AND kind='buy' AND "
                             "created_at>=? AND status NOT IN ('expired','cancelled')", (user["id"], time.time() - 86400))
            seen = {str(r["r"] or "").lstrip("@") for r in rows}
            if recipient.lstrip("@").lower() not in seen and len(seen) >= mr:
                return Check(False, f"за сутки уже {len(seen)} разных получателей — напишите в поддержку")
        spent = self.db.user_spent_since(user["id"], time.time() - 86400)
        if limit and spent + total_rub > limit:
            return Check(False, f"суточный лимит {limit:g} ₽ (уже {spent:.0f} ₽)")
        return Check(True)

    def payout_decision(self, user, amount_usdt: float) -> Check:
        if user["blocked"]:
            return Check(False, "клиент заблокирован")
        auto = float(self.cfg.get("rtba.auto_payout_usdt") or 0)
        if amount_usdt > auto:
            return Check(False, f"выплата {amount_usdt:.2f} USDT больше авто-лимита {auto:g} — нужна проверка")
        if time.time() - float(user["created_at"] or 0) < 3600 and amount_usdt > auto / 2:
            return Check(False, "новый клиент: первая крупная выплата — на проверку")
        return Check(True)

    def referral_reward(self, price_rub: float, margin_rub: float) -> float:
        if not self.is_open("referral"):
            return 0.0
        want = price_rub * float(self.cfg.get("referral.reward_pct") or 0) / 100
        cap = max(0.0, margin_rub) * float(self.cfg.get("referral.max_share_of_margin_pct") or 0) / 100
        return round(max(0.0, min(want, cap)), 2)
