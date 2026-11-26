"""Клиент Fragment (fragment.com) для покупки Telegram Stars.

Fragment работает через свой AJAX-API: страница отдаёт адрес вида /api?hash=…, а все действия — это
POST с полем method. Сессия браузера, в которой подключён TON-кошелёк, задаётся cookie
(stel_ssid, stel_dt, stel_token, stel_ton_token). Покупка:

  1. searchStarsRecipient(query=username, quantity)  → recipient (внутренний id получателя) и имя;
  2. initBuyStarsRequest(recipient, quantity)          → req_id и сумма в TON;
  3. getBuyStarsLink(id=req_id, account, device, transaction=1) → транзакция TON Connect
     (адрес Fragment, сумма в нанотонах, payload с комментарием «… Ref#…»);
  4. кошелёк бота подписывает и отправляет транзакцию в сеть (starsbot.ton.wallet);
  5. (необязательно) метод подтверждения, который Fragment вернул в confirm_method.

Если Fragment поменяет разметку или названия методов, править нужно только этот файл.
"""
from __future__ import annotations

import asyncio
import html as html_lib
import json
import logging
import random
import re
import time
from typing import Any

import aiohttp

from starsbot.core.models import FragmentTx, Recipient, TonMessage
from starsbot.ton.wallet import decode_comment

log = logging.getLogger("fragment")
BASE = "https://fragment.com"
API_HASH_RE = re.compile(r'"apiUrl"\s*:\s*"\\?/api\\?\?hash=([0-9a-fA-F]+)"')
WALLET_INIT_RE = re.compile(r"Wallet\.init\((\{.*?\})\)", re.S)
COOKIE_NAMES = ("stel_ssid", "stel_dt", "stel_token", "stel_ton_token")


class FragmentError(Exception):
    def __init__(self, text: str, code: str = "fragment"):
        super().__init__(text)
        self.code = code


class FragmentAuthError(FragmentError):
    """Сессия Fragment истекла, кошелёк в ней не подключён или аккаунт не прошёл KYC — нужен человек."""

    def __init__(self, text: str, code: str = "auth"):
        super().__init__(text, code)


def parse_cookies(raw: str | dict | None) -> dict[str, str]:
    """«stel_ssid=…; stel_token=…» или dict → dict. Лишние cookie отбрасываются."""
    if not raw:
        return {}
    if isinstance(raw, dict):
        items = raw.items()
    else:
        items = (p.split("=", 1) for p in str(raw).replace("\n", ";").split(";") if "=" in p)
    out = {k.strip(): v.strip() for k, v in items}
    return {k: v for k, v in out.items() if k in COOKIE_NAMES and v}


def parse_api_hash(page: str) -> str | None:
    m = API_HASH_RE.search(page or "")
    return m.group(1) if m else None


def parse_wallet_init(page: str) -> dict:
    """Wallet.init({...}) на странице Fragment: адрес подключённого кошелька и logged_in."""
    m = WALLET_INIT_RE.search(page or "")
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}


GIFT_ITEM_RE = re.compile(r'<a href="/gift/([a-z0-9]+-\d+)[^"]*"\s+class="tm-grid-item"(.*?)</a>', re.S)
GIFT_VALUE_RE = re.compile(r'tm-grid-item-value[^>]*>\s*([\d][\d,\.]*)\s*<')
AJ_STATE_RE = re.compile(r"ajInit\((\{.*?\})\);", re.S)
BUY_BTN_RE = re.compile(r'js-buy-now-btn"[^>]*data-bid-amount="([\d\.]+)"')
GIFT_TYPE = 5                 # Aj.state.type на странице подарка fragment.com/gift/<slug>-<номер>


def _num(text: str) -> float:
    try:
        return float(str(text).replace(",", "").strip())
    except ValueError:
        return 0.0


def parse_gift_listings(page: str) -> list[dict]:
    """Страница коллекции /gifts/<slug>?sort=price_asc&filter=sale → [{'item': 'lolpop-236254', 'price': 5.0}]
    (только выставленные на продажу, по возрастанию цены)."""
    out = []
    for item, body in GIFT_ITEM_RE.findall(page or ""):
        m = GIFT_VALUE_RE.search(body)
        if not m or "For sale" not in body and "tm-status-avail" not in body:
            continue
        price = _num(m.group(1))
        if price > 0:
            out.append({"item": item, "price": price})
    out.sort(key=lambda x: x["price"])
    return out


def parse_gift_item(page: str) -> dict:
    """Страница подарка: продаётся ли, цена «купить сейчас», тип объекта для API и владелец."""
    state: dict = {}
    m = AJ_STATE_RE.search(page or "")
    if m:
        try:
            state = (json.loads(m.group(1)) or {}).get("state") or {}
        except json.JSONDecodeError:
            state = {}
    b = BUY_BTN_RE.search(page or "")
    owner = re.search(r'>Owner<.{0,300}?href="https?://[^"]*/address/([^"/?]+)"', page or "", re.S)
    return {"type": int(state.get("type") or GIFT_TYPE), "item": str(state.get("username") or ""),
            "title": str(state.get("itemTitle") or ""), "for_sale": b is not None,
            "price": _num(b.group(1)) if b else 0.0, "owner": owner.group(1) if owner else "",
            "ton_usd": float(state.get("tonRate") or 0)}


def _strip_html(text: str) -> str:
    return html_lib.unescape(re.sub(r"<[^>]+>", " ", text or "")).strip()


ERROR_CODES = [
    (re.compile(r"kyc|verif|верификац|sumsub|identity", re.I), "kyc"),
    (re.compile(r"no telegram users found|user not found|not found|не найден", re.I), "not_found"),
    (re.compile(r"can.?t|cannot|not allowed|bot", re.I), "cannot_receive"),
    (re.compile(r"session|log ?in|connect.*wallet|access denied|unauthori", re.I), "auth"),
    (re.compile(r"too many|flood|try again later|rate", re.I), "rate_limit"),
    (re.compile(r"minimum|at least|quantity", re.I), "bad_quantity"),
]


def classify_error(text: str) -> str:
    for rx, code in ERROR_CODES:
        if rx.search(text or ""):
            return code
    return "fragment"


class FragmentClient:
    def __init__(self, cookies: str | dict, wallet, *, user_agent: str, dry_run: bool = False,
                 min_interval: float = 1.0):
        self.cookies = parse_cookies(cookies)
        self.wallet = wallet
        self.user_agent = user_agent
        self.dry_run = dry_run
        self.min_interval = float(min_interval)
        self.api_hash: str = ""
        self.session: aiohttp.ClientSession | None = None
        self.wallet_address: str = ""
        self._lock = asyncio.Lock()
        self._last = 0.0
        self.on_cookies = None          # callback(dict): Fragment выдал новые cookie — сохранить их в настройки
        self.rotated_at = 0.0           # когда сервер последний раз обновил cookie сессии

    # ------------------------------------------------------------------ http
    async def _sess(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30), cookies=self.cookies,
                headers={"User-Agent": self.user_agent, "Accept-Language": "en,ru;q=0.8"})
        return self.session

    def cookie_header(self) -> str:
        return "; ".join(f"{k}={self.cookies[k]}" for k in COOKIE_NAMES if self.cookies.get(k))

    def harvest_cookies(self) -> bool:
        """Fragment продлевает сессию через Set-Cookie. Без сохранения после перезапуска бот снова берёт
        старые cookie из настроек и сессия «истекает» раньше, чем могла бы."""
        if self.session is None:
            return False
        jar = {c.key: c.value for c in self.session.cookie_jar if c.key in COOKIE_NAMES and c.value}
        changed = {k: v for k, v in jar.items() if self.cookies.get(k) != v}
        if not changed:
            return False
        self.cookies.update(changed)
        self.rotated_at = time.time()
        log.info("Fragment обновил cookie: %s", ", ".join(sorted(changed)))
        if self.on_cookies is not None:
            try:
                self.on_cookies(self.cookie_header())
            except Exception as e:  # noqa: BLE001 — сохранить не вышло: сессия в памяти всё равно новая
                log.warning("не удалось сохранить новые cookie Fragment: %s", e)
        return True

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()

    async def _throttle(self) -> None:
        async with self._lock:
            gap = self.min_interval - (time.monotonic() - self._last)
            if gap > 0:
                await asyncio.sleep(gap)
            self._last = time.monotonic()

    async def login(self) -> dict:
        """Открывает страницу покупки звёзд: берёт hash API и проверяет, что кошелёк подключён."""
        if not self.cookies.get("stel_ssid"):
            raise FragmentAuthError("не заданы cookie Fragment (нужны stel_ssid, stel_dt, stel_token, stel_ton_token)")
        await self._throttle()
        sess = await self._sess()
        try:
            async with sess.get(f"{BASE}/stars/buy") as r:
                page = await r.text()
                if r.status >= 500 or r.status == 429:
                    raise FragmentError(f"Fragment HTTP {r.status}", "network")
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise FragmentError(f"Fragment недоступен: {e}", "network") from e
        self.harvest_cookies()
        h = parse_api_hash(page)
        if not h:
            raise FragmentError("на странице Fragment не найден адрес API (разметка изменилась?)", "layout")
        self.api_hash = h
        winfo = parse_wallet_init(page)
        if not winfo.get("logged_in") or not winfo.get("address"):
            raise FragmentAuthError("в сессии Fragment не подключён TON-кошелёк: войдите на fragment.com, подключите "
                                    "кошелёк бота и скопируйте cookie заново")
        self.wallet_address = str(winfo["address"])
        if self.wallet is not None and self.wallet_address.lower() != self.wallet.raw_address.lower():
            raise FragmentAuthError(f"к Fragment подключён другой кошелёк ({self.wallet_address}), а бот платит с "
                                    f"{self.wallet.raw_address}. Подключите на Fragment кошелёк бота.")
        return winfo

    async def api(self, method: str, _page: str = "stars/buy", **params: Any) -> dict:
        if not self.api_hash:
            await self.login()
        data = {k: (json.dumps(v, separators=(",", ":")) if isinstance(v, (dict, list)) else v)
                for k, v in params.items()}
        data["method"] = method
        for attempt in range(1, 4):
            await self._throttle()
            sess = await self._sess()
            try:
                async with sess.post(f"{BASE}/api", params={"hash": self.api_hash}, data=data, headers={
                        "X-Requested-With": "XMLHttpRequest", "Origin": BASE, "Referer": f"{BASE}/{_page}",
                        "X-Aj-Referer": f"{BASE}/{_page.split('/')[0]}",
                        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                        "Accept": "application/json, text/javascript, */*; q=0.01"}) as r:
                    text = await r.text()
                    if r.status == 429 or r.status >= 500:
                        raise FragmentError(f"Fragment HTTP {r.status}", "network")
                self.harvest_cookies()
            except (aiohttp.ClientError, asyncio.TimeoutError, FragmentError) as e:
                if attempt == 3:
                    raise FragmentError(f"{method}: {e}", "network") from e
                await asyncio.sleep(2 * attempt + random.random())
                continue
            try:
                resp = json.loads(text)
            except json.JSONDecodeError:
                if attempt < 3 and ("hash" in text.lower() or not text.strip()):
                    self.api_hash = ""              # hash устарел — перечитываем страницу
                    await self.login()
                    continue
                raise FragmentError(f"{method}: ответ не JSON ({text[:120]!r})", "layout")
            if isinstance(resp, dict) and resp.get("error"):
                err = _strip_html(str(resp["error"]))
                code = classify_error(err)
                if code in ("auth", "kyc"):
                    raise FragmentAuthError(f"{method}: {err}", code)
                raise FragmentError(err, code)
            return resp if isinstance(resp, dict) else {"result": resp}
        raise FragmentError(f"{method}: нет ответа", "network")

    # ------------------------------------------------------------------ звёзды
    async def search_recipient(self, username: str, quantity: int | str = "") -> Recipient:
        resp = await self.api("searchStarsRecipient", query=username.lstrip("@"), quantity=quantity)
        found = resp.get("found") or {}
        if not found.get("recipient"):
            raise FragmentError(f"пользователь @{username} не найден в Telegram", "not_found")
        return Recipient(username=username.lstrip("@"), recipient=str(found["recipient"]),
                         name=_strip_html(str(found.get("name") or "")))

    async def init_buy(self, recipient: str, quantity: int) -> tuple[str, float]:
        # сайт перед созданием заявки обновляет состояние формы покупки; без этого Fragment может ответить
        # «Access denied»
        try:
            await self.api("updateStarsBuyState", mode="new", lv="false", dh=str(random.randint(100_000_000, 2_147_483_647)))
        except FragmentError as e:
            log.info("Fragment updateStarsBuyState: %s (продолжаем)", e)
        try:
            resp = await self.api("initBuyStarsRequest", recipient=recipient, quantity=int(quantity),
                                  payment_method="ton")
        except FragmentAuthError as e:
            if "access denied" in str(e).lower():
                # поиск получателя работает и без входа, а покупка — только у вошедшего аккаунта с KYC
                raise FragmentAuthError(
                    "Fragment отказал в покупке (Access denied). Проверьте в браузере на fragment.com/stars/buy, "
                    "что можете сами купить звёзды этим аккаунтом: чаще всего не пройдена верификация (KYC) "
                    "или cookie/User-Agent взяты не из того браузера", "auth") from e
            raise
        req_id = str(resp.get("req_id") or "")
        if not req_id:
            raise FragmentError(f"Fragment не создал запрос на покупку: {str(resp)[:200]}", "layout")
        try:
            amount = float(str(resp.get("amount", "0")).replace(",", "."))
        except ValueError:
            amount = 0.0
        return req_id, amount

    def _account(self) -> dict:
        w = self.wallet
        return {"address": w.raw_address, "chain": "-239", "walletStateInit": w.state_init_b64(),
                "publicKey": w.public_key.hex()}

    @staticmethod
    def _device() -> dict:
        return {"platform": "web", "appName": "tonkeeper", "appVersion": "3.27.2", "maxProtocolVersion": 2,
                "features": ["SendTransaction", {"name": "SendTransaction", "maxMessages": 4}]}

    async def get_buy_link(self, req_id: str, amount_ton: float = 0.0) -> FragmentTx:
        await self.wallet.refresh()
        resp = await self.api("getBuyStarsLink", account=self._account(), device=self._device(), transaction=1,
                              id=req_id, show_sender=0)
        if resp.get("need_verify"):
            raise FragmentAuthError("Fragment требует верификацию аккаунта (KYC): пройдите её на fragment.com", "kyc")
        tx = resp.get("transaction") or {}
        msgs = [TonMessage(address=str(m.get("address")), amount=int(m.get("amount")), payload=m.get("payload") or "",
                           state_init=m.get("stateInit") or "") for m in tx.get("messages") or []]
        if not msgs:
            raise FragmentError(f"Fragment не вернул транзакцию: {str(resp)[:200]}", "layout")
        valid_until = int(tx.get("validUntil") or (time.time() + 600))
        comment = decode_comment(msgs[0].payload)
        total = sum(m.amount for m in msgs) / 1e9
        return FragmentTx(req_id=req_id, valid_until=valid_until, messages=msgs, amount_ton=total or amount_ton,
                          comment=comment, confirm_method=str(resp.get("confirm_method") or ""),
                          confirm_params=dict(resp.get("confirm_params") or {}))

    # ------------------------------------------------------------------ Telegram Premium
    # Подарок Premium на fragment.com/premium/gift: те же шаги, что у звёзд, но методы *Premium* и срок в месяцах.
    async def search_premium_recipient(self, username: str, months: int) -> Recipient:
        resp = await self.api("searchPremiumGiftRecipient", _page="premium/gift", query=username.lstrip("@"), months=int(months))
        found = resp.get("found") or {}
        if not found.get("recipient"):
            err = _strip_html(str(resp.get("error") or ""))
            raise FragmentError(err or f"пользователь @{username} не найден в Telegram",
                                classify_error(err) if err else "not_found")
        return Recipient(username=username.lstrip("@"), recipient=str(found["recipient"]),
                         name=_strip_html(str(found.get("name") or "")))

    async def init_premium(self, recipient: str, months: int) -> tuple[str, float]:
        try:
            await self.api("updatePremiumState", _page="premium/gift", mode="new", lv="false",
                           dh=str(random.randint(100_000_000, 2_147_483_647)))
        except FragmentError as e:
            log.info("Fragment updatePremiumState: %s (продолжаем)", e)
        try:
            resp = await self.api("initGiftPremiumRequest", _page="premium/gift", recipient=recipient, months=int(months))
        except FragmentAuthError as e:
            if "access denied" in str(e).lower():
                raise FragmentAuthError(
                    "Fragment отказал в покупке Premium (Access denied). Проверьте в браузере на "
                    "fragment.com/premium/gift, что можете сами подарить Premium этим аккаунтом (KYC, cookie)",
                    "auth") from e
            raise
        req_id = str(resp.get("req_id") or "")
        if not req_id:
            raise FragmentError(f"Fragment не создал запрос на Premium: {str(resp)[:200]}", "layout")
        try:
            amount = float(str(resp.get("amount", "0")).replace(",", "."))
        except ValueError:
            amount = 0.0
        return req_id, amount

    async def get_premium_link(self, req_id: str, amount_ton: float = 0.0) -> FragmentTx:
        await self.wallet.refresh()
        resp = await self.api("getGiftPremiumLink", _page="premium/gift", account=self._account(), device=self._device(), transaction=1,
                              id=req_id, show_sender=0)
        return self._tx_from(resp, req_id, amount_ton)

    def _tx_from(self, resp: dict, req_id: str, amount_ton: float) -> FragmentTx:
        if resp.get("need_verify"):
            raise FragmentAuthError("Fragment требует верификацию аккаунта (KYC): пройдите её на fragment.com", "kyc")
        tx = resp.get("transaction") or {}
        msgs = [TonMessage(address=str(m.get("address")), amount=int(m.get("amount")), payload=m.get("payload") or "",
                           state_init=m.get("stateInit") or "") for m in tx.get("messages") or []]
        if not msgs:
            raise FragmentError(f"Fragment не вернул транзакцию: {str(resp)[:200]}", "layout")
        valid_until = int(tx.get("validUntil") or (time.time() + 600))
        total = sum(m.amount for m in msgs) / 1e9
        return FragmentTx(req_id=req_id, valid_until=valid_until, messages=msgs, amount_ton=total or amount_ton,
                          comment=decode_comment(msgs[0].payload), confirm_method=str(resp.get("confirm_method") or ""),
                          confirm_params=dict(resp.get("confirm_params") or {}))

    # ------------------------------------------------------------------ коллекционные подарки (NFT)
    # fragment.com/gifts: подарки продаются за TON. «Купить сейчас» — метод getBidLink (type=5, username=<slug-номер>,
    # bid=цена): после транзакции подарок оказывается на кошельке. Отправка в Telegram получателю — как на сайте:
    # searchNftTransferRecipient → initNftTransferRequest(recipient, slug) → getNftTransferLink(id) → транзакция.
    async def page(self, path: str) -> str:
        await self._throttle()
        sess = await self._sess()
        try:
            async with sess.get(f"{BASE}/{path.lstrip('/')}") as r:
                text = await r.text()
                if r.status >= 500 or r.status == 429:
                    raise FragmentError(f"Fragment HTTP {r.status}", "network")
                if r.status == 404:
                    raise FragmentError(f"{path}: нет такой страницы на Fragment", "not_found")
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise FragmentError(f"Fragment недоступен: {e}", "network") from e
        return text

    async def gift_listings(self, collection: str, limit: int = 10) -> list[dict]:
        page = await self.page(f"gifts/{collection}?sort=price_asc&filter=sale")
        return parse_gift_listings(page)[:limit]

    async def gift_item(self, item: str) -> dict:
        return parse_gift_item(await self.page(f"gift/{item}"))

    async def gift_buy_link(self, item: str, amount: float) -> FragmentTx:
        await self.wallet.refresh()
        resp = await self.api("getBidLink", _page=f"gift/{item}", account=self._account(), device=self._device(),
                              transaction=1, type=GIFT_TYPE, username=item, bid=f"{amount:g}")
        return self._tx_from(resp, f"buy:{item}", amount)

    async def gift_transfer_recipient(self, username: str) -> Recipient:
        resp = await self.api("searchNftTransferRecipient", _page="my/assets", query=username.lstrip("@"))
        found = resp.get("found") or {}
        if not found.get("recipient"):
            err = _strip_html(str(resp.get("error") or ""))
            raise FragmentError(err or f"пользователь @{username} не найден в Telegram",
                                classify_error(err) if err else "not_found")
        return Recipient(username=username.lstrip("@"), recipient=str(found["recipient"]),
                         name=_strip_html(str(found.get("name") or "")))

    async def gift_transfer_init(self, recipient: str, item: str) -> str:
        resp = await self.api("initNftTransferRequest", _page=f"gift/{item}", recipient=recipient, slug=item)
        req_id = str(resp.get("req_id") or "")
        if not req_id:
            raise FragmentError(f"Fragment не создал перевод подарка: {str(resp)[:200]}", "layout")
        return req_id

    async def gift_transfer_link(self, req_id: str, item: str) -> FragmentTx:
        await self.wallet.refresh()
        resp = await self.api("getNftTransferLink", _page=f"gift/{item}", account=self._account(),
                              device=self._device(), transaction=1, id=req_id, show_sender=0)
        return self._tx_from(resp, req_id, 0.0)

    async def confirm(self, tx: FragmentTx, boc: str) -> None:
        """Сообщает Fragment, что транзакция отправлена (как это делает сайт после TON Connect). Не критично."""
        if not tx.confirm_method or self.dry_run:
            return
        try:
            await self.api(tx.confirm_method, **tx.confirm_params, boc=boc)
        except FragmentError as e:
            log.info("Fragment %s: %s (не критично, оплата идёт по блокчейну)", tx.confirm_method, e)
