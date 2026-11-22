"""Лот с ключами на FunPay без ручной настройки: найти свой лот в разделе или выставить новый.

Раздел по умолчанию — Team Fortress 2 → «Предметы» (node 1808, game 435). Новый лот создаётся по пустой форме
FunPay (lots/offerEdit?node=…): краткое и полное описание ru/en, цена, наличие. В тестовом режиме лот выставляется
выключенным — его включит сверка склада после выхода из теста.
"""
from __future__ import annotations

import logging
import re

from mannbot.funpay.client import FunPayError

log = logging.getLogger("lots")

DEFAULT_LOT = {"offer_id": 0, "node_id": 1808, "game_id": 435, "keys_per_unit": 1}
KEY_LOT_RE = re.compile(r"(?i)mann\s*co|манн|ключ.*ящик|crate\s*key")
KEY_OPTION_RE = re.compile(r"(?i)ключ|key")


def build_key_lot_fields(base: dict[str, str], selects: dict[str, list[tuple[str, str]]], cfg_fp, *,
                         price: float, amount: int, active: bool) -> dict[str, str]:
    f = {k: v for k, v in base.items() if k not in ("offer_id", "csrf_token", "deleted")}
    # в разделе может быть список «тип предмета»: выбираем вариант про ключи, если он есть
    for name, opts in selects.items():
        for value, text in opts:
            if value and KEY_OPTION_RE.search(text or ""):
                f[name] = value
                break
    texts = {"summary": (cfg_fp.get("lot_title_ru"), cfg_fp.get("lot_title_en")),
             "desc": (cfg_fp.get("lot_desc_ru"), cfg_fp.get("lot_desc_en"))}
    for kind, (ru, en) in texts.items():
        f[f"fields[{kind}][ru]"] = str(ru or "")
        f[f"fields[{kind}][en]"] = str(en or ru or "")
    f["price"] = f"{price:.2f}"
    f["amount"] = str(max(1, int(amount)))
    if active:
        f["active"] = "on"
    else:
        f.pop("active", None)               # чекбокс не отправлен = лот выключен
    return f


async def ensure_key_lot(engine) -> str:
    """Вернуть отчёт. Если у лотов из настроек нет offer_id: взять свой существующий лот с ключами в разделе,
    иначе выставить новый. id записывается в настройки (engine.save_config)."""
    cfg = engine.cfg
    lots = [dict(l) for l in (cfg.funpay.lots or [])] or [dict(DEFAULT_LOT)]
    if any(l.get("offer_id") for l in lots):
        return "лот уже указан"
    lot = lots[0]
    for k, v in DEFAULT_LOT.items():
        lot.setdefault(k, v)
        if not lot.get(k) and k != "offer_id":
            lot[k] = v
    node = int(lot["node_id"])
    fp = engine.fp
    taken = {str(l.get("offer_id")) for l in lots}
    offer_id, how = "", ""
    try:
        own = [o for o in await fp.get_own_lots(node) if KEY_LOT_RE.search(o.description or "")
               and o.offer_id not in taken]
    except FunPayError as e:
        own = []
        log.warning("свои лоты раздела %s: %s", node, e)
    if own:
        own.sort(key=lambda o: (not o.active, -(o.amount or 0)))
        offer_id, how = own[0].offer_id, f"найден ваш лот «{own[0].description[:60]}»"
    elif cfg.funpay.get("auto_create_lot", True):
        from mannbot.core.rtba import price_floor
        price = float(cfg.funpay.get("lot_start_price") or 0) or max(price_floor(cfg.rtba), 150.0)
        if cfg.rtba.get("ceiling_price"):
            price = min(price, float(cfg.rtba.ceiling_price))
        test_mode = bool(getattr(fp, "dry_run", False))
        base, selects = await fp.get_lot_form(node, 0)
        fields = build_key_lot_fields(base, selects, cfg.funpay, price=price, amount=1, active=not test_mode)
        offer_id = await fp.create_lot(fields)
        if not offer_id:
            for o in await fp.get_own_lots(node):
                if KEY_LOT_RE.search(o.description or "") and o.offer_id not in taken:
                    offer_id = o.offer_id
                    break
        if not offer_id:
            raise FunPayError("FunPay принял форму, но лот не найден в ваших лотах — проверьте раздел вручную")
        how = f"выставлен новый лот за {price:.2f} ₽" + (" (выключен, пока включён тестовый режим)" if test_mode else "")
    else:
        return "лота с ключами нет, автосоздание выключено (funpay.auto_create_lot)"
    lot["offer_id"] = int(offer_id)
    lots[0] = lot
    cfg.funpay.lots = lots                    # движок видит лот сразу, без перезапуска
    if getattr(engine, "save_config", None):
        engine.save_config({"funpay": {"lots": lots}}, rebuild=False)
    engine.db.add_event("-", "lot_adopted", offer_id=offer_id, how=how)
    return f"лот {offer_id}: {how}"
