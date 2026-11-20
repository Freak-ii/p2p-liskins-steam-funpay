"""TON-кошелёк бота: подпись переводов (wallet v4r2 / v5r1) и работа с сетью через toncenter API v2.

Кошелёк должен быть уже развёрнут (это тот же кошелёк, что подключён к Fragment). Seqno, wallet_id и
публичный ключ читаются из данных контракта, поэтому код кошелька не нужен: бот лишь проверяет, что
ключ из сид-фразы совпадает с ключом в контракте, и только после этого что-либо подписывает.

Защита от двойной оплаты: каждая транзакция имеет seqno и valid_until. Пока seqno кошелька не
вырос, транзакция не исполнена; после valid_until она уже никогда не исполнится — только тогда
покупку можно безопасно повторить (см. Engine.watch_purchases).
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time
from dataclasses import dataclass

import aiohttp
from pytoniq_core import Address, Builder, Cell, ExternalMsgInfo, InternalMsgInfo, MessageAny, StateInit, begin_cell, CurrencyCollection
from starsbot.ton import mnemonic as MN
from nacl.bindings import crypto_sign, crypto_sign_BYTES

from starsbot.core.models import SentTx, TonMessage

log = logging.getLogger("ton")
NANO = 1_000_000_000
V5_SIGNED_EXTERNAL = 0x7369676E
V5_ACTION_SEND_MSG = 0x0EC3C86D
SEND_MODE = 3                     # платим комиссию отдельно + игнорируем ошибки действия


class TonError(Exception):
    def __init__(self, text: str, code: str = "ton"):
        super().__init__(text)
        self.code = code


@dataclass
class WalletState:
    balance: int                  # нанотоны
    seqno: int
    wallet_id: int
    public_key: bytes
    active: bool
    code_b64: str = ""
    data_b64: str = ""
    deploy: bool = False          # кошелёк ещё не развёрнут: первая транзакция несёт StateInit (как делает Tonkeeper)


def to_nano(ton: float) -> int:
    return int(round(float(ton) * NANO))


def from_nano(nano: int | str) -> float:
    return int(nano) / NANO


def decode_comment(payload_b64: str) -> str:
    """Текстовый комментарий (op=0 + snake-строка) из payload Fragment. Пусто, если это не текст."""
    if not payload_b64:
        return ""
    try:
        cell = Cell.one_from_boc(base64.b64decode(payload_b64 + "=" * (-len(payload_b64) % 4)))
        sl = cell.begin_parse()
        if sl.remaining_bits < 32 or sl.load_uint(32) != 0:
            return ""
        return sl.load_snake_string()
    except Exception:  # noqa: BLE001
        return ""


def parse_wallet_data(data: Cell, version: str) -> tuple[int, int, bytes]:
    """(seqno, wallet_id, public_key) из данных контракта кошелька."""
    sl = data.begin_parse()
    if version == "v5r1":
        sl.load_bit()                      # is_signature_allowed
        return sl.load_uint(32), sl.load_uint(32), sl.load_bytes(32)
    return sl.load_uint(32), sl.load_uint(32), sl.load_bytes(32)


class Toncenter:
    """Минимальный клиент toncenter.com API v2 (или совместимого сервера)."""

    def __init__(self, endpoint: str = "https://toncenter.com/api/v2", api_key: str = ""):
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key
        self.session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def _sess(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            headers = {"X-API-Key": self.api_key} if self.api_key else {}
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25), headers=headers)
        return self.session

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()

    async def call(self, method: str, *, params: dict | None = None, json_body: dict | None = None,
                   retries: int = 4):
        sess = await self._sess()
        url = f"{self.endpoint}/{method}"
        for attempt in range(1, retries + 1):
            async with self._lock:          # без API-ключа toncenter разрешает ~1 запрос в секунду
                gap = (0.25 if self.api_key else 1.1) - (time.monotonic() - self._last)
                if gap > 0:
                    await asyncio.sleep(gap)
                self._last = time.monotonic()
            try:
                if json_body is not None:
                    req = sess.post(url, json=json_body)
                else:
                    req = sess.get(url, params=params)
                async with req as r:
                    data = await r.json(content_type=None)
                    if r.status == 429 or r.status >= 500:
                        raise TonError(f"toncenter HTTP {r.status}", "network")
                    if not data.get("ok"):
                        raise TonError(f"toncenter {method}: {data.get('error') or data}", "api")
                    return data.get("result")
            except TonError as e:
                if e.code != "network" or attempt == retries:
                    raise
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
                if attempt == retries:
                    raise TonError(f"toncenter {method}: {e}", "network") from e
            await asyncio.sleep(1.5 * attempt)
        raise TonError("unreachable")

    async def address_info(self, address: str) -> dict:
        return await self.call("getAddressInformation", params={"address": address})

    async def send_boc(self, boc_b64: str) -> str:
        res = await self.call("sendBocReturnHash", json_body={"boc": boc_b64}, retries=2)
        return (res or {}).get("hash", "") if isinstance(res, dict) else ""

    async def transactions(self, address: str, limit: int = 20) -> list[dict]:
        return await self.call("getTransactions", params={"address": address, "limit": limit, "archival": "true"}) or []


class TonWallet:
    def __init__(self, mnemonic: str | list[str], address: str = "", *, version: str = "v5r1",
                 api: Toncenter | None = None, dry_run: bool = False, mnemonic_type: str = "auto"):
        try:
            self._cands = MN.candidates(mnemonic, mnemonic_type or "auto")
        except MN.MnemonicError as e:
            raise TonError(str(e), "config") from e
        if version not in ("v4r2", "v5r1"):
            raise TonError(f"неизвестная версия кошелька {version} (поддерживаются v4r2 и v5r1)", "config")
        self.version = version
        self.auto_address = not str(address or "").strip()
        key = self._cands[0]
        if not self.auto_address:
            try:
                self.address = Address(address.strip())
            except Exception as e:  # noqa: BLE001
                raise TonError(f"адрес кошелька не распознан: {e}", "config") from e
            found = MN.match_address(mnemonic, address.strip(), mnemonic_type or "auto")
            if found:                                  # адрес — стандартный кошелёк этой фразы: версию знаем точно
                key, self.version = found
        else:
            self.address = key.address(version)
        self._use(key)
        self.api = api or Toncenter()
        self.dry_run = dry_run
        self.state: WalletState | None = None
        self._resolved = not self.auto_address

    def _use(self, key: "MN.KeyCandidate") -> None:
        self.key_kind = key.kind
        self.public_key, self._secret = key.public_key, key.secret

    async def resolve_address(self) -> None:
        """Адрес не введён: перебираем адреса фразы (BIP39/TON × W5/v4r2) и берём тот, где есть кошелёк."""
        if self._resolved:
            return
        best = None
        for c in self._cands:
            for v in (self.version, "v4r2" if self.version == "v5r1" else "v5r1"):
                addr = c.address(v)
                try:
                    info = await self.api.address_info(addr.to_str(is_user_friendly=False))
                except TonError:
                    continue
                score = (2 if info.get("state") == "active" else 0) + (1 if int(info.get("balance") or 0) > 0 else 0)
                if best is None or score > best[0]:
                    best = (score, c, v, addr)
                if score >= 2:
                    break
            if best and best[0] >= 2:
                break
        if best is not None:
            _, c, v, addr = best
            self._use(c)
            self.version, self.address = v, addr
        self._resolved = True

    @property
    def raw_address(self) -> str:
        return self.address.to_str(is_user_friendly=False)

    @property
    def friendly_address(self) -> str:
        return self.address.to_str(is_user_friendly=True, is_bounceable=False)

    async def close(self) -> None:
        await self.api.close()

    # ------------------------------------------------------------------ состояние
    async def refresh(self) -> WalletState:
        await self.resolve_address()
        info = await self.api.address_info(self.raw_address)
        balance = int(info.get("balance") or 0)
        active = info.get("state") == "active"
        if not active or not info.get("data"):
            own = next((c for c in self._cands for v in (self.version,) if c.address(v) == self.address), None)
            if own is None:
                for c in self._cands:
                    for v in ("v5r1", "v4r2"):
                        if c.address(v) == self.address:
                            own, self.version = c, v
            if own is None:
                self.state = WalletState(balance, 0, 0, b"", False)
                raise TonError("кошелёк не активен, а адрес не совпадает с сид-фразой. Проверьте адрес или "
                               "отправьте с кошелька хотя бы одну транзакцию из Tonkeeper.", "inactive")
            self._use(own)
            si = MN.wallet_state_init(self.public_key, self.version)
            wid = MN.V5R1_WALLET_ID if self.version == "v5r1" else MN.V4R2_WALLET_ID
            self.state = WalletState(balance, 0, wid, self.public_key, False,
                                     base64.b64encode(si.code.to_boc()).decode(),
                                     base64.b64encode(si.data.to_boc()).decode(), deploy=True)
            return self.state
        data = Cell.one_from_boc(base64.b64decode(info["data"]))
        seqno, wallet_id, pub = parse_wallet_data(data, self.version)
        if pub != self.public_key:
            other = next((c for c in self._cands if c.public_key == pub), None)
            if other is None:
                alt = "v4r2" if self.version == "v5r1" else "v5r1"
                try:
                    s2, w2, p2 = parse_wallet_data(data, alt)
                except Exception:  # noqa: BLE001
                    p2 = b""
                other = next((c for c in self._cands if c.public_key == p2), None)
                if other is not None:
                    self.version, seqno, wallet_id, pub = alt, s2, w2, p2
            if other is None:
                raise TonError("сид-фраза не подходит к этому адресу. Проверьте адрес (Tonkeeper → Получить) и "
                               "слова фразы — 12 или 24, по порядку.", "key_mismatch")
            self._use(other)
        self.state = WalletState(balance, seqno, wallet_id, pub, True, info.get("code", ""), info["data"])
        return self.state

    async def balance_ton(self) -> float:
        st = await self.refresh()
        return from_nano(st.balance)

    def state_init_b64(self) -> str:
        """StateInit кошелька для Fragment (поле walletStateInit, как в TON Connect)."""
        if not self.state or not self.state.code_b64:
            return ""
        si = StateInit(code=Cell.one_from_boc(base64.b64decode(self.state.code_b64)),
                       data=Cell.one_from_boc(base64.b64decode(self.state.data_b64)))
        return base64.b64encode(si.serialize().to_boc()).decode()

    # ------------------------------------------------------------------ подпись
    def _sign(self, cell: Cell) -> bytes:
        return crypto_sign(cell.hash, self._secret)[:crypto_sign_BYTES]

    @staticmethod
    def internal_message(msg: TonMessage) -> Cell:
        dest = Address(msg.address)
        body = Cell.one_from_boc(base64.b64decode(msg.payload + "=" * (-len(msg.payload) % 4))) if msg.payload \
            else Cell.empty()
        init = None
        if msg.state_init:
            init = StateInit.deserialize(Cell.one_from_boc(base64.b64decode(msg.state_init)).begin_parse())
        info = InternalMsgInfo(ihr_disabled=True, bounce=dest.is_bounceable, bounced=False, src=None, dest=dest,
                               value=CurrencyCollection(grams=int(msg.amount)), ihr_fee=0, fwd_fee=0,
                               created_lt=0, created_at=0)
        return MessageAny(info=info, init=init, body=body).serialize()

    def build_body(self, messages: list[TonMessage], seqno: int, valid_until: int, wallet_id: int) -> Cell:
        if not 1 <= len(messages) <= 4:
            raise TonError("в транзакции должно быть от 1 до 4 сообщений", "bad_tx")
        msgs = [self.internal_message(m) for m in messages]
        if self.version == "v5r1":
            out_list = Cell.empty()
            for m in msgs:                         # OutList n = prev:^(OutList n-1) + action
                out_list = (begin_cell().store_ref(out_list).store_uint(V5_ACTION_SEND_MSG, 32)
                            .store_uint(SEND_MODE, 8).store_ref(m).end_cell())
            unsigned = (begin_cell().store_uint(V5_SIGNED_EXTERNAL, 32).store_uint(wallet_id, 32)
                        .store_uint(valid_until, 32).store_uint(seqno, 32)
                        .store_bit(1).store_ref(out_list)      # Maybe ^OutList
                        .store_bit(0)                          # has_other_actions
                        .end_cell())
            signature = self._sign(unsigned)
            return Builder().store_cell(unsigned).store_bytes(signature).end_cell()
        b = (begin_cell().store_uint(wallet_id, 32).store_uint(valid_until, 32).store_uint(seqno, 32)
             .store_uint(0, 8))                                # op = простой перевод
        for m in msgs:
            b.store_uint(SEND_MODE, 8).store_ref(m)
        unsigned = b.end_cell()
        signature = self._sign(unsigned)
        return Builder().store_bytes(signature).store_cell(unsigned).end_cell()

    def build_external(self, body: Cell) -> Cell:
        info = ExternalMsgInfo(src=None, dest=self.address, import_fee=0)
        init = None
        if self.state is not None and self.state.deploy:      # первая транзакция кошелька разворачивает контракт
            init = MN.wallet_state_init(self.public_key, self.version)
        return MessageAny(info=info, init=init, body=body).serialize()

    async def transfer(self, messages: list[TonMessage], valid_until: int, *, on_signed=None) -> SentTx:
        """Подписывает и отправляет перевод. on_signed(seqno, valid_until, msg_hash) вызывается ДО отправки в
        сеть — чтобы движок записал попытку в базу и после сбоя мог сверить её с блокчейном."""
        st = await self.refresh()
        total = sum(int(m.amount) for m in messages)
        if st.balance < total + to_nano(0.02):
            raise TonError(f"на кошельке {from_nano(st.balance):.4f} TON, нужно {from_nano(total):.4f} TON + комиссия",
                           "insufficient_funds")
        body = self.build_body(messages, st.seqno, valid_until, st.wallet_id)
        ext = self.build_external(body)
        boc = base64.b64encode(ext.to_boc()).decode()
        msg_hash = ext.hash.hex()
        if on_signed is not None:
            await on_signed(st.seqno, valid_until, msg_hash)
        if self.dry_run:
            log.info("[DRY-RUN] перевод %.4f TON (seqno %s) не отправлен", from_nano(total), st.seqno)
            return SentTx(seqno=st.seqno, valid_until=valid_until, msg_hash="dry" + msg_hash[:16], boc=boc)
        await self.api.send_boc(boc)
        log.info("Перевод %.4f TON отправлен (seqno %s, msg %s)", from_nano(total), st.seqno, msg_hash[:16])
        return SentTx(seqno=st.seqno, valid_until=valid_until, msg_hash=msg_hash, boc=boc)

    # ------------------------------------------------------------------ сверка с блокчейном
    async def current_seqno(self) -> int:
        return (await self.refresh()).seqno

    async def find_outgoing(self, dest: str, amount: int, since: float, comment_part: str = "") -> dict | None:
        """Ищет исходящую транзакцию кошелька на адрес dest с суммой amount после момента since."""
        want = Address(dest)
        for tx in await self.api.transactions(self.raw_address, limit=30):
            if float(tx.get("utime") or 0) < since - 120:
                continue
            for out in tx.get("out_msgs") or []:
                try:
                    same = Address(out.get("destination", "")) == want
                except Exception:  # noqa: BLE001
                    same = False
                if same and int(out.get("value") or 0) == int(amount):
                    if comment_part and comment_part not in (out.get("message") or ""):
                        continue
                    return {"hash": (tx.get("transaction_id") or {}).get("hash", ""), "utime": tx.get("utime"),
                            "fee": int(tx.get("fee") or 0), "comment": out.get("message", "")}
        return None


class UnconfiguredWallet:
    """Заглушка, пока сид-фраза/адрес не заданы: приложение запускается, бот — нет (validate_config)."""

    version = ""
    raw_address = ""
    friendly_address = "—"
    public_key = b""
    state = None

    def __init__(self, reason: str = "кошелёк не настроен"):
        self.reason = reason

    def _fail(self):
        raise TonError(f"кошелёк TON не настроен: {self.reason}", "config")

    async def refresh(self):
        self._fail()

    async def balance_ton(self) -> float:
        self._fail()

    async def current_seqno(self) -> int:
        self._fail()

    def state_init_b64(self) -> str:
        return ""

    async def transfer(self, messages, valid_until, *, on_signed=None):
        self._fail()

    async def find_outgoing(self, dest, amount, since, comment_part=""):
        self._fail()

    async def close(self) -> None:
        return None
