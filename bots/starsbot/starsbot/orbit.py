"""Орбита в StarsBot и TG Gift: снять позицию с продажи сразу на всех площадках, когда её закрыли в Центре.

Ключи (что закрывает Центр, см. center/orbit.py):
  StarsBot: stars — лоты звёзд FunPay; premium — лоты Premium; gifts — старые лоты «Подарком» StarsBot;
            ggsel:stars, ggsel:premium — товары GGSEL; ozon:stars — товары Ozon; avito:stars, wb:stars —
            объявления и коды (у них нет API остатков — бот сообщает, что снять вручную);
            supply:stars, supply:premium, supply:gift — заказы Telegram-магазина через /api/supply.
  TG Gift:  gifts — обычные подарки; nft — NFT-подарки; topup — «Звёзды → Подарком»; supply:gift.

Закрытие: лоты выключаются сразу (даже на паузе), а сверка остатков потом держит их выключенными
(blocked() в stock_sync). Открытие: кэш остатков сбрасывается, следующая сверка (раз в 90 с) вернёт лоты.
"""
from __future__ import annotations

import logging

from starsbot.orbit_gate import OrbitGate, OrbitTracker

log = logging.getLogger("starsbot.orbit")


def lot_groups(engine) -> list[tuple[str, list[dict], str, int]]:
    """(ключ Орбиты, лоты FunPay, префикс кэша остатка в kv, раздел FunPay)."""
    node = int(engine.cfg.funpay.node_id)
    out: list[tuple[str, list[dict], str, int]] = []
    g = engine.gifts
    if hasattr(g, "nft_lots"):                     # TG Gift (профиль giftshop)
        out += [("gifts", list(g.lots), "shop_lot_amount", node),
                ("nft", list(g.nft_lots), "shop_lot_amount", node),
                ("topup", list(g.topup_lots), "shop_lot_amount", getattr(g, "topup_node", node))]
        return out
    out.append(("stars", list(engine.lots), "lot_amount", node))
    if engine.premium is not None:
        out.append(("premium", list(engine.premium.lots), "premium_lot_amount", engine.premium.node))
    if g is not None and hasattr(g, "gcfg"):
        out.append(("gifts", list(g.lots), "gift_lot_amount", node))
    return out


class StarsOrbit:
    def __init__(self, engine, gate: OrbitGate | None = None):
        self.engine = engine
        self.gate = gate or OrbitGate("tggift" if engine.cfg.general.get("profile") == "giftshop" else "starsbot")
        self.tracker = OrbitTracker(self.gate)
        self.applied: dict[str, str] = {}

    def blocked(self, *keys: str) -> bool:
        return self.gate.blocked(*keys)

    def reason(self, key: str) -> str:
        return self.gate.reason(key)

    async def tick(self) -> None:
        if not self.gate.enabled:
            return
        ch = self.tracker.changes()
        if ch is None:
            return
        closed, opened = ch
        for key in sorted(closed):
            try:
                self.applied[key] = await self.close(key)
            except Exception as e:  # noqa: BLE001 — одна площадка не должна мешать остальным
                log.exception("orbit close %s", key)
                self.applied[key] = f"error: {e}"
        for key in sorted(opened):
            self.applied.pop(key, None)
            self.reopen(key)
        self.gate.ack(dict(self.applied), self.tracker.seen_version)
        e = self.engine
        if closed or opened:
            e.db.add_event("-", "orbit", closed=sorted(closed), opened=sorted(opened), version=self.tracker.seen_version)
        if closed:
            await e.notifier.notify("INFO", "orbit_closed", "🪐 Орбита: сняты с продажи — " + ", ".join(sorted(closed)),
                                    throttle_key="orbit_closed")

    # ------------------------------------------------------------------ закрытие
    async def close(self, key: str) -> str:
        e = self.engine
        for gkey, lots, prefix, node in lot_groups(e):
            if gkey != key:
                continue
            lots = [l for l in lots if l.get("offer_id")]
            if not lots:
                return "skip: лотов нет"
            if not getattr(e, "funpay_on", True):
                return "skip: FunPay выключен (только поставщик)"
            for lot in lots:
                await e.fp.update_lot(int(lot["offer_id"]), node, active=False)
                e.db.kv_set(f"{prefix}:{lot['offer_id']}", 0)
            return "ok"
        if key.startswith("supply:"):
            return "ok"                              # запросы магазина проверяются на лету (supply_api.quote)
        plat, _, what = key.partition(":")
        chan = e.channels.get(plat)
        if chan is None:
            return f"skip: {plat} не подключён"
        if plat == "ggsel":
            n = 0
            for p in chan.products():
                if bool(p.get("months")) != (what == "premium"):
                    continue
                await chan.client.update_offer(int(p["offer_id"]), quantity=0, is_unlimited_quantity=False)
                e.db.kv_set(f"ggsel_stock:{p['offer_id']}", 0)
                n += 1
            return "ok" if n else "skip: товаров нет"
        if plat == "ozon":
            stocks = {str(p["offer_id"]): 0 for p in chan.products() if p.get("offer_id")}
            if not stocks:
                return "skip: товаров нет"
            await chan.client.import_stocks(stocks)
            for offer in stocks:
                e.db.kv_set(f"ozon_stock:{offer}", 0)
            return "ok"
        if plat == "avito":
            await e.notifier.notify("WARNING", "orbit_manual", "🪐 Орбита: звёзды сняты с продажи — снимите объявления "
                                    "на Авито с публикации (у Авито нет API остатков).", throttle_key="orbit_avito")
            return "manual: снимите объявления звёзд на Авито с публикации"
        if plat == "wb":
            await e.notifier.notify("WARNING", "orbit_manual", "🪐 Орбита: звёзды сняты с продажи — уберите карточки "
                                    "кодов в кабинете Wildberries (у WB нет API для цифровых кодов).",
                                    throttle_key="orbit_wb")
            return "manual: уберите карточки кодов в кабинете WB"
        return "skip: неизвестный ключ"

    def reopen(self, key: str) -> None:
        """Сбросить кэш остатков: следующая сверка выставит лоты заново по деньгам на кошельке."""
        e = self.engine
        for gkey, lots, prefix, _ in lot_groups(e):
            if gkey == key:
                for lot in lots:
                    if lot.get("offer_id"):
                        e.db.kv_set(f"{prefix}:{lot['offer_id']}", None)
        plat, _, _ = key.partition(":")
        chan = e.channels.get(plat)
        if chan is not None and plat in ("ggsel", "ozon"):
            for p in chan.products():
                if p.get("offer_id"):
                    e.db.kv_set(f"{plat}_stock:{p['offer_id']}", None)
