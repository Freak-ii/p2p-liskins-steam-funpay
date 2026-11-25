"""Орбита в ShopBot: позиции, снятые в Пульте Орбиты центра, пропадают из бота, мини-приложения и канала.

Ключи (решает центр, center/orbit.py):
  buy.<товар>     — товар не продаётся: пункт RTBA buy.<товар> жёстко закрыт (режим «on» его не открывает),
                    кнопка и строка пропадают из приветствия и меню, плитка — из мини-приложения;
  post.<товар>    — товар не упоминается в постах канала (витрина, цены, закреп с кнопкой);
  service.<id>    — услуга магазина (VPN, App Store…) для версий ShopBot с разделом услуг.
Товары: stars, gift, premium, steam_keys. Опубликованные посты канала, где текст изменился, бот правит сам.
"""
from __future__ import annotations

import logging

from shopbot.orbit_gate import OrbitGate, OrbitTracker

log = logging.getLogger("shopbot.orbit")
PRODUCTS = ("stars", "gift", "premium", "steam_keys")


class ShopOrbit:
    def __init__(self, shop, gate: OrbitGate | None = None):
        self.shop = shop
        self.gate = gate or OrbitGate("shopbot")
        self.tracker = OrbitTracker(self.gate)

    def blocked(self, *keys: str) -> bool:
        return self.gate.blocked(*keys)

    def menu_hidden(self) -> set[str]:
        """Товары, которых нет в меню бота и в мини-приложении."""
        return {p for p in PRODUCTS if self.gate.blocked(f"buy.{p}")}

    def post_hidden(self) -> set[str]:
        """Товары, которых нет в постах и описаниях канала."""
        return {p for p in PRODUCTS if self.gate.blocked(f"buy.{p}", f"post.{p}")}

    async def tick(self) -> None:
        if not self.gate.enabled:
            return
        ch = self.tracker.changes()
        if ch is None:
            return
        closed, opened = ch
        applied = {}
        has_services = getattr(self.shop, "services", None) is not None
        for k in self.gate.closed():
            applied[k] = "ok" if not k.startswith("service.") or has_services else \
                "skip: в этой версии ShopBot нет раздела услуг"
        if closed or opened:
            try:
                await self.shop.rtba.tick()              # пункты buy.* закрываются/открываются сразу
            except Exception:  # noqa: BLE001
                log.exception("orbit: rtba")
            if self.shop.channel is not None:
                try:
                    res = await self.shop.channel.refresh_published()
                    for k in closed:
                        if k.startswith("post.") and res.startswith("error"):
                            applied[k] = res
                except Exception as e:  # noqa: BLE001
                    log.exception("orbit: channel")
                    for k in closed:
                        if k.startswith("post."):
                            applied[k] = f"error: пост канала не обновлён: {e}"
            if self.shop.miniapp is not None and self.shop.miniapp.url:
                try:
                    await self.shop.miniapp.refresh()
                except Exception:  # noqa: BLE001
                    log.exception("orbit: miniapp")
            self.shop.rtba.log("orbit", "orbit", "Пульт Орбиты", closed=sorted(closed), opened=sorted(opened))
        self.gate.ack(applied, self.tracker.seen_version)
