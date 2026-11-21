"""NFT-подарки через Fragment для TG Gift — сторона StarsBot.

TG Gift продаёт на FunPay коллекционные (улучшенные) подарки Telegram. Самый частый дешёвый путь — купить подарок
на fragment.com/gifts за TON и отправить его покупателю в Telegram. Кошелёк TON и сессия Fragment есть только у
StarsBot, а транзакции кошелька должны идти строго по одной, поэтому TG Gift не платит сам: он пишет задание в общий
файл координации (`nft_jobs`), а StarsBot выполняет его тем же безопасным путём, что и звёзды:

  1. проверка получателя (searchNftTransferRecipient) — до любой оплаты;
  2. самый дешёвый подарок коллекции не дороже max_ton из задания → «Купить сейчас» (getBidLink), транзакция
     записывается в базу до отправки и сверяется с блокчейном (внутренний заказ NFT-…, delivery='nft');
  3. когда покупка подтверждена, подарок лежит на кошельке → перевод в Telegram получателю
     (initNftTransferRequest → getNftTransferLink), снова одна транзакция со сверкой;
  4. состояние задания (bought / done / failed / manual) пишется в общий файл — TG Gift завершает заказ.

Что угодно непонятное после оплаты (транзакция без подтверждения, Fragment не отдаёт перевод) — задание «manual»
и сообщение продавцу: повторной оплаты вслепую нет.
"""
from __future__ import annotations

import logging
import secrets
import time

from starsbot.core.models import OrderStatus as S
from starsbot.fragment.client import FragmentAuthError, FragmentError

log = logging.getLogger("nft")

PREFIX = "NFT-"
DEFAULTS = {
    "enabled": True,
    "max_ton_per_item": 60.0,        # не покупать подарок дороже N TON, что бы ни просил TG Gift
    "max_ton_per_day": 200.0,        # всего на NFT-подарки за сутки, TON
    "transfer_max_ton": 0.6,         # перевод подарка в Telegram (газ сети и сбор) не дороже N TON
    "wallet_reserve_ton": 2.0,       # после покупки на кошельке должно остаться N TON на свои заказы звёзд
    "transfer_wait_minutes": 30,     # сколько ждать, пока купленный подарок появится на кошельке у Fragment
}


class NftSupplyManager:
    def __init__(self, cfg, engine):
        self.cfg = cfg
        self.engine = engine
        self.db = engine.db
        self.last: dict = {}

    @property
    def c(self) -> dict:
        return {**DEFAULTS, **(self.cfg.get("nft_supply") or {})}

    @property
    def enabled(self) -> bool:
        return bool(self.c.get("enabled")) and self.engine.coord.enabled and \
            self.cfg.general.get("profile", "stars") != "giftshop"

    @staticmethod
    def owns(o) -> bool:
        return o is not None and str(o["order_id"]).startswith(PREFIX)

    def job_of(self, oid: str) -> dict:
        return dict(self.db.kv_get(f"nft_job:{oid}") or {})

    def _save_job(self, oid: str, job: dict) -> None:
        self.db.kv_set(f"nft_job:{oid}", job)

    # ------------------------------------------------------------------ сведения для TG Gift
    def publish_info(self) -> None:
        """Курс TON (для себестоимости подарка в TG Gift), остаток кошелька и готовность Fragment."""
        e = self.engine
        rate = None
        if e.econ is not None:
            try:
                rate = e.econ.effective_rate()[0]
            except Exception:  # noqa: BLE001
                rate = None
        w = e.wallet_info or {}
        e.coord.set_info("starsbot", {
            "nft": self.enabled and bool(e.fragment_ok) and not e.paused,
            "rub_per_ton": rate or e.rates.value, "wallet_ton": w.get("balance"),
            "max_ton_per_item": float(self.c["max_ton_per_item"]),
            "transfer_ton": float(self.c.get("expected_transfer_ton") or 0.15), "ts": time.time()})

    def spent_24h(self) -> float:
        rows = self.db.q("SELECT order_id FROM orders WHERE order_id LIKE ? AND created_at>=? AND status NOT IN "
                         "('refunded','blocked')", (PREFIX + "%", time.time() - 86400))
        return sum(float(self.job_of(r["order_id"]).get("price") or 0) for r in rows)

    # ------------------------------------------------------------------ цикл
    async def tick(self) -> None:
        if not self.engine.coord.enabled or self.cfg.general.get("profile", "stars") == "giftshop":
            return
        self.publish_info()
        if not self.enabled or self.engine.paused:
            return
        for job in self.engine.coord.nft_jobs(("pending",)):
            res = await self.accept(job)
            self.last[job["ref"]] = {"ts": time.time(), "result": res}
            log.info("NFT %s: %s", job["ref"], res)

    async def accept(self, job: dict) -> str:
        """Задание TG Gift → внутренний заказ NFT-… (покупка пойдёт через общую очередь выдачи)."""
        e, c = self.engine, self.c
        coord = e.coord
        ref = str(job["ref"])
        max_ton = min(float(job.get("max_ton") or 0), float(c["max_ton_per_item"]))
        if max_ton <= 0:
            coord.nft_job_update(ref, "failed", note="в задании нет предельной цены")
            return "нет max_ton"
        day = float(c.get("max_ton_per_day") or 0)
        if day and self.spent_24h() + max_ton > day:
            coord.nft_job_update(ref, "failed", note=f"дневной лимит NFT {day:g} TON")
            await e.notifier.notify("WARNING", "nft_day_cap", f"NFT-подарки: дневной лимит {day:g} TON исчерпан — "
                                    "TG Gift отправит подарок другим путём или закроет лот.", throttle_key="nft_day_cap")
            return "дневной лимит"
        if not e.fragment_ok:
            return "Fragment не готов — жду"
        oid = f"{PREFIX}{time.strftime('%m%d%H%M')}-{secrets.token_hex(2).upper()}"
        username = str(job.get("username") or "").lstrip("@")
        e.db.create_order(order_id=oid, buyer_username=f"(NFT для {job.get('bot') or 'TG Gift'})", buyer_id=0,
                          chat_id=None, description=f"NFT {job.get('title') or job.get('collection')} → @{username}",
                          quantity=1, stars_per_unit=0, stars=0, price=0, currency="₽", fp_status="internal",
                          channel="internal", delivery="nft", approved=1, lang="ru", tg_username=username)
        self._save_job(oid, {"ref": ref, "collection": job["collection"], "title": job.get("title") or "",
                             "max_ton": max_ton, "stage": "check", "created": time.time()})
        e.db.add_event(oid, "created", nft=job["collection"], ref=ref, max_ton=max_ton)
        coord.nft_job_update(ref, "accepted", supplier=coord.bot, supplier_ref=oid)
        e._set(oid, S.READY, "nft_queued")
        e.enqueue(oid)
        return f"заказ {oid}"

    # ------------------------------------------------------------------ шаги (из очереди выдачи, под замком покупки)
    async def step(self, o) -> None:
        oid = o["order_id"]
        job = self.job_of(oid)
        stage = job.get("stage") or "check"
        try:
            if stage in ("check", "buy"):
                await self._buy(o, job)
            elif stage == "transfer":
                await self._transfer(o, job)
        except FragmentAuthError as err:
            await self.engine.fragment_auth_problem(err)
            self.engine.enqueue(oid, 60)
        except FragmentError as err:
            if err.code in ("network", "rate_limit"):
                self.engine.enqueue(oid, 60)
                return
            await self._fail(o, job, f"Fragment: {err}", bought=stage == "transfer")

    async def _buy(self, o, job: dict) -> None:
        e, c = self.engine, self.c
        oid = o["order_id"]
        fr = e.fragment
        username = str(o["tg_username"] or "")
        rec = await fr.gift_transfer_recipient(username)                 # получатель — до оплаты
        e._set(oid, event="nft_recipient_ok", tg_recipient=rec.recipient, tg_name=rec.name)
        listings = await fr.gift_listings(job["collection"], limit=8)
        cheap = [x for x in listings if x["price"] <= float(job["max_ton"])]
        if not cheap:
            best = listings[0]["price"] if listings else None
            await self._fail(o, job, f"на Fragment нет {job.get('title') or job['collection']} дешевле "
                             f"{job['max_ton']:g} TON" + (f" (самый дешёвый {best:g} TON)" if best else ""),
                             bought=False, quiet=True)
            return
        item = None
        for x in cheap[:3]:                                  # страница коллекции могла устареть — проверяем подарок
            info = await fr.gift_item(x["item"])
            if info["for_sale"] and 0 < info["price"] <= float(job["max_ton"]):
                item = {"item": x["item"], "price": info["price"]}
                break
        if item is None:
            await self._fail(o, job, "подходящие подарки уже купили — новых дешевле лимита нет", bought=False,
                             quiet=True)
            return
        balance = await e.wallet.balance_ton()
        need = item["price"] + float(c["transfer_max_ton"]) + float(c["wallet_reserve_ton"])
        if balance < need:
            await self._fail(o, job, f"на кошельке {balance:.2f} TON, а нужно {need:.2f} (подарок, перевод и запас "
                             "на звёзды)", bought=False)
            return
        tx = await fr.gift_buy_link(item["item"], item["price"])
        job.update(stage="buy", item=item["item"], price=item["price"])
        self._save_job(oid, job)
        e.coord.nft_job_update(job["ref"], "buying", item=item["item"], price_ton=item["price"])
        ok = await e.send_fragment_tx(o, f"buy:{item['item']}", tx, item["price"], stars=0)
        if not ok and S(e.db.get_order(oid)["status"]) in (S.MANUAL, S.READY):
            # транзакция не ушла (или не совпала) — денег не тратили
            await self._fail(o, job, "оплата подарка не отправлена", bought=False)

    async def _transfer(self, o, job: dict) -> None:
        e, c = self.engine, self.c
        oid = o["order_id"]
        fr = e.fragment
        try:
            rec = await fr.gift_transfer_recipient(str(o["tg_username"] or ""))
            req_id = await fr.gift_transfer_init(rec.recipient, job["item"])
            tx = await fr.gift_transfer_link(req_id, job["item"])
        except FragmentAuthError:
            raise
        except FragmentError as err:
            waited = time.time() - float(job.get("bought_at") or time.time())
            if waited < float(c["transfer_wait_minutes"]) * 60:
                log.info("NFT %s: перевод пока недоступен (%s) — подарок ещё доходит до кошелька", oid, err)
                e.enqueue(oid, 60)
                return
            await self._fail(o, job, f"подарок куплен, но Fragment не даёт его отправить: {err}", bought=True)
            return
        e.coord.nft_job_update(job["ref"], "sending")
        ok = await e.send_fragment_tx(o, req_id, tx, 0.0, stars=0, max_total_ton=float(c["transfer_max_ton"]))
        if not ok and S(e.db.get_order(oid)["status"]) in (S.MANUAL, S.READY):
            await self._fail(o, job, "перевод подарка не отправлен (сумма не совпала или ошибка кошелька)",
                             bought=True)

    async def on_tx_confirmed(self, o, tx: dict) -> None:
        """Транзакция этапа подтверждена блокчейном (вызывает Engine.on_delivered)."""
        e = self.engine
        oid = o["order_id"]
        job = self.job_of(oid)
        ton = float(o["ton_amount"] or 0)
        if job.get("stage") == "buy":
            job.update(stage="transfer", bought_at=time.time(), buy_ton=ton, buy_tx=tx.get("hash", ""))
            self._save_job(oid, job)
            e.coord.nft_job_update(job["ref"], "bought", cost_ton=ton)
            e._set(oid, S.READY, "nft_bought", fail_reason="")
            e.enqueue(oid, 20)                    # Fragment видит подарок на кошельке не сразу
            await e.notifier.notify("INFO", "nft_bought", f"🎁 NFT {job.get('title') or job['collection']} "
                                    f"({job.get('item')}) куплен за {ton:.3f} TON — отправляю @{o['tg_username']}",
                                    order_id=oid)
            return
        total = float(job.get("buy_ton") or 0) + ton
        cost_rub = e.ton_to_rub(total)
        job.update(stage="done", transfer_ton=ton, done_at=time.time())
        self._save_job(oid, job)
        e._set(oid, S.COMPLETED, "nft_sent", delivered_at=time.time(), completed_at=time.time(),
               tx_hash=tx.get("hash", ""), ton_amount=total, cost_rub=cost_rub, fail_reason="")
        e.coord.nft_job_update(job["ref"], "done", cost_ton=total, cost_rub=cost_rub,
                               note=f"{job.get('item')} → @{o['tg_username']}")
        await e.notifier.notify("INFO", "nft_sent", f"🎁 NFT {job.get('item')} отправлен @{o['tg_username']} "
                                f"({total:.3f} TON всего)", order_id=oid)

    async def _fail(self, o, job: dict, why: str, *, bought: bool, quiet: bool = False) -> None:
        """bought=False — денег не тратили, TG Gift может выдать другим путём; True — нужен человек."""
        e = self.engine
        oid = o["order_id"]
        if bought:
            e._set(oid, S.MANUAL, "nft_manual", fail_reason="nft_manual")
            e.coord.nft_job_update(job["ref"], "manual", note=why[:300])
            await e.notifier.notify("CRITICAL", "nft_manual", f"#{oid}: {why}. Подарок {job.get('item')} на "
                                    f"кошельке — отправьте @{o['tg_username']} вручную на fragment.com/gift/"
                                    f"{job.get('item')} и отметьте заказ TG Gift выданным.", order_id=oid)
            return
        e._set(oid, S.BLOCKED, "nft_failed", fail_reason="nft_failed")
        e.coord.nft_job_update(job["ref"], "failed", note=why[:300])
        if not quiet:
            await e.notifier.notify("WARNING", "nft_failed", f"NFT {job.get('title') or job['collection']}: {why}",
                                    order_id=oid, throttle_key=f"nft_failed:{job['collection']}")

    def status(self) -> dict:
        return {"enabled": self.enabled, "jobs": self.engine.coord.nft_jobs(limit=15), "spent_24h": self.spent_24h(),
                "limits": self.c}
