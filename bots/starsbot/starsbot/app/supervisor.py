"""Supervisor: управляет жизненным циклом бота внутри приложения (запуск/остановка/перезапуск,
сохранение настроек, проверка подключений). База и CRM доступны всегда, даже когда бот остановлен."""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import secrets
import time
from collections import deque
from pathlib import Path
from typing import Any

import yaml

from starsbot.analytics.analytics import Analytics
from starsbot.config import DEFAULTS, Section, funpay_enabled, load_config, validate_config
from starsbot.core import messages as M
from starsbot.core.engine import Engine
from starsbot.crm.db import Database
from starsbot.coord import Coordinator
from starsbot.economics import Economics
from starsbot.notify.notifier import Notifier
from starsbot.rates import RateProvider

log = logging.getLogger("app")

SECRET_FIELDS = {("gifts", "api_hash"), ("funpay", "golden_key"), ("fragment", "cookies"), ("ton", "mnemonic"), ("ton", "toncenter_api_key"),
                 ("notifications.telegram", "bot_token"), ("dashboard", "token"), ("dashboard", "operator_token"),
                 ("dashboard", "viewer_token"), ("ozon", "api_key"), ("ozon.activation_bot", "bot_token"),
                 ("ggsel", "api_key"), ("treasury.bybit", "api_key"), ("treasury.bybit", "api_secret"), ("treasury.hub", "mnemonic"),
                 ("avito", "client_secret"), ("wb.activation_bot", "bot_token"),
                 ("supply", "token")}


class MemoryLogHandler(logging.Handler):
    """Последние строки журнала для страницы «Журнал»."""

    def __init__(self, capacity: int = 1500):
        super().__init__()
        self.lines: deque[dict] = deque(maxlen=capacity)
        self.seq = 0
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)-8s %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.seq += 1
            self.lines.append({"n": self.seq, "level": record.levelname, "text": self.format(record)})
        except Exception:  # noqa: BLE001
            pass


class Supervisor:
    def __init__(self, home: Path, *, demo: bool = False, overrides: dict | None = None):
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        self.config_path = self.home / "config.yaml"
        self.demo = demo
        self.overrides = overrides or {}
        self.inherited: list[str] = []        # TG Gift: настройки, взятые у StarsBot (golden_key, уведомления)
        self.raw: dict = self._read_raw()
        self.cfg: Section = self._load()
        self._apply_patch_file()
        self._add_new_denominations()
        db_path = self.cfg.general.database
        if db_path != ":memory:" and not Path(db_path).is_absolute():
            db_path = str(self.home / db_path)
        self.db = Database(db_path)
        self.state = "stopped"          # stopped | starting | running | error
        self.last_error = ""
        self.started_at: float | None = None
        self.restart_required = False
        self.task: asyncio.Task | None = None
        self._started_once = False
        self.quit_event: asyncio.Event | None = None     # выставляется кнопкой «Выход» в интерфейсе
        self.engine: Engine
        self.analytics: Analytics
        self.notifier: Notifier
        self.econ: Economics
        self._build()

    # ------------------------------------------------------------------ config
    def _add_new_denominations(self) -> None:
        """StarsBot: в своём списке лотов «По username» дописать номиналы, которых раньше не было (200, 300, 2500,
        750, 1500, …) — без id лота, чтобы они появились в «Лотах» с кнопкой «выставить». Один раз на номинал."""
        if self.profile != "stars" or not self.config_path.exists():
            return
        raw_lots = (self.raw.get("funpay") or {}).get("lots")
        if not isinstance(raw_lots, list) or not raw_lots:
            return
        from starsbot.config import CUSTOM_DENOMINATIONS, DENOMINATIONS
        have = {int(l.get("stars") or 0) for l in raw_lots if isinstance(l, dict)}
        done = set((self.raw.get("funpay") or {}).get("denominations_added") or [])
        new = [d for d in DENOMINATIONS + CUSTOM_DENOMINATIONS if d not in have and d not in done]
        if not new:
            return
        lots = [dict(l) for l in raw_lots] + [
            {"stars": d, "offer_id": 0, "position": 0, **({"custom": True} if d in CUSTOM_DENOMINATIONS else {})}
            for d in new]
        lots.sort(key=lambda l: int(l.get("stars") or 0))
        self.save({"funpay": {"lots": lots, "denominations_added": sorted(done | set(new))}}, rebuild=False)
        log.warning("в лоты добавлены новые номиналы (без id, выставляются кнопкой): %s", ", ".join(map(str, new)))

    def _apply_patch_file(self) -> None:
        """config.patch.yaml рядом с config.yaml — разовая правка настроек (без секретов): сливается в config.yaml
        при запуске и переименовывается в config.patch.applied.yaml. Так поддержка может поправить настройки,
        не трогая файл с ключами."""
        patch = self.home / "config.patch.yaml"
        if not patch.exists():
            return
        try:
            upd = yaml.safe_load(patch.read_text(encoding="utf-8")) or {}
            if not isinstance(upd, dict):
                raise ValueError("ожидается словарь секций")
            for section, key in SECRET_FIELDS:              # секреты через патч не меняются
                node = upd
                for part in section.split("."):
                    node = node.get(part) if isinstance(node, dict) else None
                if isinstance(node, dict):
                    node.pop(key, None)
            self.save(upd, rebuild=False)
            patch.replace(self.home / "config.patch.applied.yaml")
            log.warning("применены настройки из config.patch.yaml: %s", ", ".join(upd))
        except Exception as e:  # noqa: BLE001
            log.error("config.patch.yaml не применён: %s", e)

    def _read_raw(self) -> dict:
        if self.config_path.exists():
            return yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        return {}

    def _load(self) -> Section:
        cfg = load_config(self.config_path if self.config_path.exists() else None, self.overrides)
        if cfg.general.log_file and not Path(cfg.general.log_file).is_absolute():
            cfg.general.log_file = str(self.home / cfg.general.log_file)
        M.set_overrides(cfg.get("messages"))
        if cfg.general.get("profile") == "giftshop":
            from starsbot.giftshop.profile import inherit_shared
            self.inherited = inherit_shared(cfg, self.home)
        return cfg

    @property
    def profile(self) -> str:
        return str(self.cfg.general.get("profile") or "stars")

    def start_orbit_watch(self, interval: float = 5) -> None:
        """Пульт Орбиты центра действует и при остановленной торговле: снятая позиция выключает лоты сразу,
        а не при следующем запуске бота. Пока торговля идёт, это делает цикл orbit движка."""
        if getattr(self, "orbit_task", None):
            return

        async def loop() -> None:
            while True:
                try:
                    if self.state not in ("running", "starting") and self.engine is not None:
                        await self.engine.orbit.tick()
                except Exception:  # noqa: BLE001 — ошибка уйдёт в подтверждение центру при следующей попытке
                    log.exception("orbit")
                await asyncio.sleep(interval)
        self.orbit_task = asyncio.create_task(loop())

    def ensure_token(self) -> str:
        token = str(self.cfg.dashboard.token or "")
        if len(token) < 12:
            token = secrets.token_urlsafe(18)
            self.save({"dashboard": {"token": token}}, rebuild=False)
        return token

    @property
    def problems(self) -> list[str]:
        return validate_config(self.cfg)

    @property
    def configured(self) -> bool:
        return not [p for p in self.problems if not p.startswith("dashboard")]

    def save(self, updates: dict, *, rebuild: bool = True) -> None:
        """Сливает изменения в config.yaml. Пустая строка в секретном поле = «не менять»."""
        raw = copy.deepcopy(self.raw)
        new_cookies = str((updates.get("fragment") or {}).get("cookies") or "").strip()
        if new_cookies and new_cookies != str((raw.get("fragment") or {}).get("cookies") or "").strip():
            updates = copy.deepcopy(updates)
            updates["fragment"]["cookies_set_at"] = int(time.time())   # от этого считается возраст сессии

        def merge(dst: dict, src: dict, path: str = "") -> None:
            for k, v in src.items():
                p = f"{path}.{k}" if path else k
                if isinstance(v, dict) and k != "messages":
                    dst.setdefault(k, {})
                    if not isinstance(dst[k], dict):
                        dst[k] = {}
                    merge(dst[k], v, p)
                else:
                    if (path, k) in SECRET_FIELDS and v in ("", None):
                        continue
                    dst[k] = v
        merge(raw, updates)
        if "messages" in updates:
            raw["messages"] = updates["messages"]
        tmp = self.config_path.with_suffix(".tmp")
        tmp.write_text("# Настройки StarsBot (редактируются в приложении)\n" +
                       yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
        tmp.replace(self.config_path)
        self.raw = raw
        self.cfg = self._load()
        if rebuild:
            if self.state == "running":
                self.restart_required = True
            else:
                self._build()

    def save_rotated_fragment_cookies(self, header: str) -> None:
        """Fragment продлил сессию: пишем новые cookie в config.yaml без перезапуска бота.
        cookies_set_at не трогаем (это «вставлены вручную»), возраст считается от cookies_refreshed_at."""
        if not header or header == str((self.raw.get("fragment") or {}).get("cookies") or "").strip():
            return
        raw = copy.deepcopy(self.raw)
        raw.setdefault("fragment", {})
        raw["fragment"]["cookies"] = header
        raw["fragment"]["cookies_refreshed_at"] = int(time.time())
        tmp = self.config_path.with_suffix(".tmp")
        tmp.write_text("# Настройки StarsBot (редактируются в приложении)\n" +
                       yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
        tmp.replace(self.config_path)
        self.raw = raw

    def secret_is_set(self, section: str, key: str) -> bool:
        node: Any = self.cfg
        for part in section.split("."):
            node = node.get(part, {})
        return bool(node.get(key))

    # ------------------------------------------------------------------ build / run
    def _build(self) -> None:
        cfg = self.cfg
        self.notifier = Notifier(cfg.notifications, self.db)
        shop = self.profile == "giftshop"
        coord = Coordinator.from_env("tggift" if shop else "starsbot", cfg.funpay.coord_db or None)
        if shop and not (cfg.general.demo or self.demo):
            from starsbot.funpay.client import FunPayClient
            from starsbot.giftshop.engine import NullFragment
            from starsbot.ton.wallet import UnconfiguredWallet
            fp = FunPayClient(cfg.funpay.golden_key, cfg.funpay.user_agent, dry_run=cfg.general.dry_run,
                              coordinator=coord)
            wallet, fragment = UnconfiguredWallet("TG Gift не платит TON — звёзды покупает StarsBot"), NullFragment()
            rates = RateProvider(cfg.rates, self.db)
        elif shop:
            from starsbot.giftshop.demo import FakeShopFunPay
            from starsbot.giftshop.engine import NullFragment
            from starsbot.rates import StaticRate
            from starsbot.ton.wallet import UnconfiguredWallet
            fp, wallet, fragment, rates = FakeShopFunPay(), UnconfiguredWallet("демо"), NullFragment(), StaticRate(105.0)
        elif cfg.general.demo or self.demo:
            from starsbot.demo import FakeFragment, FakeFunPay, FakeWallet
            from starsbot.rates import StaticRate
            fp, wallet = FakeFunPay(), FakeWallet(balance=120.0)
            fragment, rates = FakeFragment(wallet), StaticRate(105.0)
        else:
            from starsbot.fragment.client import FragmentClient
            from starsbot.funpay.client import FunPayClient
            from starsbot.ton.wallet import Toncenter, TonWallet, UnconfiguredWallet
            fp = FunPayClient(cfg.funpay.golden_key, cfg.funpay.user_agent, dry_run=cfg.general.dry_run,
                              coordinator=coord)
            try:
                wallet = TonWallet(cfg.ton.mnemonic, cfg.ton.address, version=cfg.ton.version,
                                   api=Toncenter(cfg.ton.toncenter_endpoint, cfg.ton.toncenter_api_key),
                                   dry_run=cfg.general.dry_run, mnemonic_type=cfg.ton.get("mnemonic_type", "auto"))
            except Exception as e:  # noqa: BLE001 — кошелёк ещё не настроен: панель и CRM работают без него
                wallet = UnconfiguredWallet(str(e))
            fragment = FragmentClient(cfg.fragment.cookies, wallet,
                                      user_agent=cfg.fragment.get("user_agent") or cfg.funpay.user_agent,
                                      dry_run=cfg.general.dry_run, min_interval=cfg.fragment.min_interval)
            fragment.on_cookies = self.save_rotated_fragment_cookies
            rates = RateProvider(cfg.rates, self.db)
        if not funpay_enabled(cfg):
            # «Только поставщик»: на FunPay не заходим; настоящий клиент — только чтобы один раз выключить лоты
            from starsbot.funpay.off import FunPayOff
            real = fp
            can_park = bool(cfg.funpay.golden_key) or bool(cfg.general.demo or self.demo)
            fp = FunPayOff((lambda: real) if can_park else None)
        self.coord = coord
        if shop:
            from starsbot.giftshop.engine import GiftShopEngine
            self.engine = GiftShopEngine(cfg, self.db, fp, fragment, wallet, self.notifier, rates, coord)
        else:
            self.engine = Engine(cfg, self.db, fp, fragment, wallet, self.notifier, rates, coord)
        if shop:
            from starsbot.giftshop.engine import shop_economics
            self.econ = shop_economics(cfg, self.db, rates, self.engine)
        else:
            self.econ = Economics(cfg, self.db, rates, self.engine)
        self.engine.econ = self.econ
        self.analytics = Analytics(cfg, self.db, self.notifier, self.engine)
        self.engine.analytics = self.analytics
        from starsbot.gifts.service import GiftService, InternalChannel
        g = cfg.get("gifts") or {}
        if cfg.general.demo or self.demo:
            from starsbot.gifts.telegram import FakeTelegramGifts
            tg = FakeTelegramGifts()
            if shop:
                from starsbot.giftshop.demo import shop_catalog
                tg._catalog = shop_catalog()[1]
        else:
            from starsbot.gifts.telegram import TelegramGifts
            sess = Path(str(g.get("session_file") or "data/telegram.session"))
            tg = TelegramGifts(int(g.get("api_id") or 0), str(g.get("api_hash") or ""),
                               sess if sess.is_absolute() else self.home / sess, dry_run=cfg.general.dry_run)
        if shop:
            from starsbot.giftshop.lots import ShopLotManager
            from starsbot.giftshop.service import ShopGiftService
            self.engine.gifts = ShopGiftService(cfg, self.engine, tg)
            self.engine.lot_manager = ShopLotManager(self.engine, save_config=self.save)
            from starsbot.giftshop.market import Assortment
            from starsbot.giftshop.nft import NftDesk, PublicFragment
            if cfg.general.demo or self.demo:
                from starsbot.giftshop.demo import demo_nft
                public = demo_nft(tg)
            else:
                public = PublicFragment(user_agent=str(cfg.funpay.get("user_agent") or "Mozilla/5.0"))
            self.engine.gifts.nft = NftDesk(self.engine.gifts, public)      # NFT: Fragment или рынок Telegram
            self.engine.gifts.assortment = Assortment(self.engine.gifts)    # подбор NFT-лотов по рынку
        else:
            self.engine.gifts = GiftService(cfg, self.engine, tg)
            self.engine.channels["internal"] = InternalChannel(self.engine.gifts)
            from starsbot.supply_api import ShopChannel
            self.engine.channels["shop"] = ShopChannel(self.engine)     # заказы Telegram-магазина (ShopBot)
            from starsbot.premium.service import PremiumService
            self.engine.premium = PremiumService(cfg, self.engine)
            self.engine.premium.register_supply_channels()
            from starsbot.funpay.lots import LotManager
            self.engine.lot_manager = LotManager(self.engine, save_config=self.save)
            from starsbot.supply import SupplyManager
            self.engine.supply = SupplyManager(cfg, self.engine)
            from starsbot.nft_supply import NftSupplyManager
            self.engine.nft = NftSupplyManager(cfg, self.engine)       # NFT-подарки с Fragment для TG Gift
        from starsbot.autopilot import Autopilot
        self.engine.autopilot = Autopilot(cfg, self.engine, self.home)
        self.engine.treasury = self._treasury(cfg)
        from starsbot.ozon.channel import build_channel
        ozon = build_channel(cfg, self.db, self.engine, self.notifier, demo=bool(cfg.general.demo or self.demo))
        if ozon is not None and not shop:
            self.engine.channels["ozon"] = ozon
        from starsbot.ggsel.channel import build_channel as build_ggsel
        ggsel = build_ggsel(cfg, self.db, self.engine, self.notifier, demo=bool(cfg.general.demo or self.demo))
        if ggsel is not None:
            self.engine.channels["ggsel"] = ggsel
        from starsbot.avito.channel import build_channel as build_avito
        avito = build_avito(cfg, self.db, self.engine, self.notifier, demo=bool(cfg.general.demo or self.demo))
        if avito is not None:
            self.engine.channels["avito"] = avito
        from starsbot.wb.channel import build_channel as build_wb
        wb = build_wb(cfg, self.db, self.engine, self.notifier, demo=bool(cfg.general.demo or self.demo))
        if wb is not None:
            self.engine.channels["wb"] = wb

    def _treasury(self, cfg):
        from starsbot.treasury.service import Treasury
        t = cfg.get("treasury") or {}
        if cfg.general.demo or self.demo:
            from starsbot.treasury.fake import FakeBybit, FakeHub
            w = self.engine.wallet
            wallets = {w.friendly_address: w}
            hub = FakeHub(balance=0.0, wallets=wallets)
            wallets[hub.friendly_address] = hub
            bybit = FakeBybit(usdt_fund=300.0, usdt_unified=500.0, price=round(105.0 / 86.8, 4), wallets=wallets)
            self.demo_treasury = (bybit, hub)
            return Treasury(cfg, self.engine, bybit=bybit, hub=hub)
        bybit = hub = None
        b, h = t.get("bybit") or {}, t.get("hub") or {}
        if h.get("mnemonic"):
            from starsbot.ton.wallet import Toncenter, TonWallet
            try:
                hub = TonWallet(h["mnemonic"], h.get("address") or "", version=h.get("version") or "v5r1",
                                api=Toncenter(cfg.ton.toncenter_endpoint, cfg.ton.toncenter_api_key),
                                dry_run=cfg.general.dry_run, mnemonic_type=h.get("mnemonic_type") or "auto")
            except Exception as e:  # noqa: BLE001
                log.warning("хаб TON не настроен: %s", e)
        if b.get("api_key") and b.get("api_secret"):
            from starsbot.treasury.bybit import BybitClient
            # вывод разрешён только на свои кошельки: рабочий кошелёк бота и хаб
            allowed = [getattr(self.engine.wallet, "friendly_address", ""), getattr(hub, "friendly_address", "")]
            bybit = BybitClient(b["api_key"], b["api_secret"], base_url=b.get("base_url") or "https://api.bybit.com",
                                allowed_addresses=[a for a in allowed if a and a != "—"])
        return Treasury(cfg, self.engine, bybit=bybit, hub=hub)

    async def start(self) -> str:
        if self.state in ("running", "starting"):
            return "Бот уже запущен"
        problems = [p for p in self.problems if not p.startswith("dashboard")]
        if problems and not (self.cfg.general.demo or self.demo):
            return "Сначала заполните настройки: " + "; ".join(problems)
        if not (self.demo and not self._started_once):   # в демо сохраняем данные эмулятора после сценария
            self._build()
        self._started_once = True
        self.restart_required = False
        self.state, self.last_error, self.started_at = "starting", "", time.time()
        self.task = asyncio.create_task(self._run())
        return "Бот запускается…"

    async def _run(self) -> None:
        try:
            starter = asyncio.create_task(self.engine.start())
            # engine.start() сначала логинится, потом крутит циклы бесконечно
            while not starter.done() and not self.engine._tasks:
                await asyncio.sleep(0.2)
            if not starter.done():
                self.state = "running"
                log.info("Бот запущен")
            await starter
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            self.state, self.last_error = "error", f"{type(e).__name__}: {e}"
            log.exception("Бот остановился с ошибкой")
            await self.notifier.notify("CRITICAL", "bot_crash", f"Бот остановился с ошибкой: {e}")
        finally:
            await self._close_clients()

    async def stop(self) -> str:
        if self.task and not self.task.done():
            await self.engine.stop()
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await self._close_clients()
        if self.state != "error":
            self.state = "stopped"
        self.started_at = None
        log.info("Бот остановлен")
        self._build()
        return "Бот остановлен"

    async def restart(self) -> str:
        await self.stop()
        self.state = "stopped"
        return await self.start()

    async def _close_clients(self) -> None:
        for c in (self.engine.fp, self.engine.fragment, self.engine.wallet, self.notifier,
                  *self.engine.channels.values(), self.engine.gifts,
                  getattr(getattr(self.engine, "treasury", None), "bybit", None),
                  getattr(getattr(self.engine, "treasury", None), "hub", None)):
            if c is None:
                continue
            try:
                await c.close()
            except Exception:  # noqa: BLE001
                pass

    async def shutdown(self) -> None:
        await self.stop()

    def status(self) -> dict:
        return {"state": self.state, "error": self.last_error, "configured": self.configured,
                "restart_required": self.restart_required, "dry_run": bool(self.cfg.general.dry_run),
                "paused": self.engine.paused, "uptime": (time.time() - self.started_at) if self.started_at else 0,
                "wallet": self.db.kv_get("wallet") or {}, "fragment": self.engine.fragment_status,
                "coordination": bool(self.coord.enabled), "demo": bool(self.cfg.general.demo or self.demo)}

    # ------------------------------------------------------------------ connection tests
    async def run_test(self, coro, timeout: float = 45) -> dict:
        try:
            return await asyncio.wait_for(coro, timeout)
        except asyncio.TimeoutError:
            return {"ok": False, "text": f"Нет ответа за {timeout:.0f} с — проверьте интернет/доступ к сайту"}

    async def test_funpay(self, golden_key: str | None = None) -> dict:
        from starsbot.funpay.client import FunPayClient
        fp = FunPayClient(golden_key or self.cfg.funpay.golden_key, self.cfg.funpay.user_agent, dry_run=True)
        try:
            await fp.login()
            sales = await fp.get_sales(state="paid")
            ours = [x for x in sales if self.engine.is_our_order(x.description)]
            return {"ok": True, "text": f"Вход выполнен: {fp.username} (id {fp.user_id}). Оплаченных заказов сейчас: "
                                        f"{len(sales)}, из них на звёзды: {len(ours)}."}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "text": f"Не удалось войти на FunPay: {e}"}
        finally:
            await fp.close()

    def _wallet(self, data: dict | None = None):
        from starsbot.ton.wallet import Toncenter, TonWallet
        d = {k: v for k, v in (data or {}).items() if v}
        ton = {**self.cfg.ton, **d}
        return TonWallet(ton["mnemonic"], ton.get("address", ""), version=ton["version"],
                         api=Toncenter(ton["toncenter_endpoint"], ton.get("toncenter_api_key", "")), dry_run=True,
                         mnemonic_type=ton.get("mnemonic_type", "auto"))

    async def test_wallet(self, data: dict | None = None) -> dict:
        try:
            wallet = self._wallet(data)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "text": f"Кошелёк: {e}"}
        try:
            st = await wallet.refresh()
            kind = "12 слов BIP39" if wallet.key_kind == "bip39" else "формат TON"
            extra = " Кошелёк ещё не развёрнут — бот развернёт его первой же оплатой." if st.deploy else ""
            return {"ok": True, "text": f"Кошелёк {wallet.friendly_address} ({wallet.version}, {kind}): баланс "
                                        f"{st.balance / 1e9:.4f} TON, seqno {st.seqno}. Сид-фраза подходит.{extra}",
                    "address": wallet.friendly_address, "version": wallet.version}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "text": f"Кошелёк: {e}"}
        finally:
            await wallet.close()

    async def test_fragment(self, cookies: str | None = None, wallet_data: dict | None = None) -> dict:
        from starsbot.fragment.client import FragmentClient
        try:
            wallet = self._wallet(wallet_data)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "text": f"Сначала настройте кошелёк: {e}"}
        fr = FragmentClient(cookies or self.cfg.fragment.cookies, wallet, user_agent=self.cfg.funpay.user_agent,
                            dry_run=True)
        try:
            await wallet.refresh()
            await fr.login()
            probe = ""
            uname = str((wallet_data or {}).get("probe_username") or "").lstrip("@")
            if uname:
                rec = await fr.search_recipient(uname, 50)
                probe = f" Тестовый поиск @{rec.username}: {rec.name or 'найден'}."
            return {"ok": True, "text": f"Fragment: сессия активна, подключён кошелёк бота {wallet.friendly_address}."
                                        f"{probe} KYC проверится на первой покупке."}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "text": f"Fragment: {e}"}
        finally:
            await fr.close()
            await wallet.close()

    async def test_telegram(self, token: str | None = None, chat_ids: list | None = None) -> dict:
        tg = Section({**self.cfg.notifications.telegram, "enabled": True})
        if token:
            tg["bot_token"] = token
        if chat_ids:
            tg["admin_chat_ids"] = chat_ids
        if not tg.bot_token or not tg.admin_chat_ids:
            return {"ok": False, "text": "Укажите токен бота и chat_id"}
        try:
            import aiohttp
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
                for cid in tg.admin_chat_ids:
                    async with s.post(f"https://api.telegram.org/bot{tg.bot_token}/sendMessage",
                                      json={"chat_id": cid, "text": "✅ StarsBot: уведомления подключены"}) as r:
                        data = await r.json()
                        if not data.get("ok"):
                            return {"ok": False, "text": f"Telegram: {data.get('description')}"}
            return {"ok": True, "text": "Тестовое сообщение отправлено в Telegram"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "text": f"Telegram недоступен: {e}"}


def default_config_dict() -> dict:
    return copy.deepcopy(DEFAULTS)
