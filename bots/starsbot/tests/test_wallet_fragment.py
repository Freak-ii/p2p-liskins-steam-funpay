import asyncio
import base64

import pytest
from nacl.signing import VerifyKey
from pytoniq_core import Cell, MessageAny, begin_cell
from pytoniq_core.crypto.keys import mnemonic_new

from starsbot.core.models import TonMessage
from starsbot.fragment import client as F
from starsbot.ton.wallet import TonError, TonWallet, V5_SIGNED_EXTERNAL, decode_comment, parse_wallet_data, to_nano

DEST = "EQBAjaOyi2wGWlk-EDkSabqqnF-MrrwMadnwqrurKpkla9nE"
ADDR = "UQCD39VS5jcptHL8vMjEXrzGaRcCVYto7HUn4bpAOg8xqEBI"


class FakeApi:
    def __init__(self, data_cell: Cell, balance=to_nano(10)):
        self.data, self.balance, self.sent = data_cell, balance, []

    async def address_info(self, address):
        return {"balance": str(self.balance), "state": "active", "code": "",
                "data": base64.b64encode(self.data.to_boc()).decode()}

    async def send_boc(self, boc):
        self.sent.append(boc)
        return "ok"

    async def close(self):
        pass


def wallet_data(version, pub, seqno=7, wallet_id=2147483409):
    b = begin_cell()
    if version == "v5r1":
        b.store_bit(1)
    return b.store_uint(seqno, 32).store_uint(wallet_id, 32).store_bytes(pub).end_cell()


@pytest.mark.parametrize("version", ["v4r2", "v5r1"])
def test_transfer_signs_and_records_before_send(version):
    words = mnemonic_new()
    w = TonWallet(words, ADDR, version=version, api=None)
    w.api = FakeApi(wallet_data(version, w.public_key))
    seen = []

    async def on_signed(seqno, valid_until, msg_hash):
        assert not w.api.sent                 # запись в базу — до отправки в сеть
        seen.append((seqno, valid_until, msg_hash))

    comment = begin_cell().store_uint(0, 32).store_snake_string("100 Telegram Stars Ref#abc").end_cell()
    payload = base64.b64encode(comment.to_boc()).decode()
    sent = asyncio.run(w.transfer([TonMessage(DEST, to_nano(1.15), payload)], 1_900_000_000, on_signed=on_signed))
    assert sent.seqno == 7 and seen[0][0] == 7 and seen[0][2] == sent.msg_hash
    assert len(w.api.sent) == 1
    ext = Cell.one_from_boc(base64.b64decode(w.api.sent[0]))
    msg = MessageAny.deserialize(ext.begin_parse())
    assert msg.init is None
    body = msg.body
    bits = body.begin_parse()
    vk = VerifyKey(w.public_key)
    if version == "v5r1":
        assert bits.preload_uint(32) == V5_SIGNED_EXTERNAL
        n = len(body.bits) - 512
        unsigned = begin_cell().store_bits(body.bits[:n])
        for r in body.refs:
            unsigned.store_ref(r)
        sig = body.bits[n:].tobytes()
    else:
        sig = body.bits[:512].tobytes()
        unsigned = begin_cell().store_bits(body.bits[512:])
        for r in body.refs:
            unsigned.store_ref(r)
    vk.verify(unsigned.end_cell().hash, sig)                                 # подпись верна


def test_key_mismatch_and_funds():
    w = TonWallet(mnemonic_new(), ADDR, version="v4r2", api=None)
    w.api = FakeApi(wallet_data("v4r2", b"\x01" * 32))
    with pytest.raises(TonError) as e:
        asyncio.run(w.refresh())
    assert e.value.code == "key_mismatch"
    w.api = FakeApi(wallet_data("v4r2", w.public_key), balance=to_nano(0.5))
    with pytest.raises(TonError) as e:
        asyncio.run(w.transfer([TonMessage(DEST, to_nano(1), "")], 1_900_000_000))
    assert e.value.code == "insufficient_funds" and not w.api.sent


def test_wallet_config_errors():
    with pytest.raises(TonError):
        TonWallet("one two three", ADDR)
    with pytest.raises(TonError):
        TonWallet(mnemonic_new(), ADDR, version="v3r2")


def test_parse_wallet_data_and_comment():
    pub = bytes(range(32))
    assert parse_wallet_data(wallet_data("v5r1", pub, 3, 9), "v5r1") == (3, 9, pub)
    assert parse_wallet_data(wallet_data("v4r2", pub, 4, 8), "v4r2") == (4, 8, pub)
    c = begin_cell().store_uint(0, 32).store_snake_string("50 Telegram Stars").end_cell()
    assert decode_comment(base64.b64encode(c.to_boc()).decode().rstrip("=")) == "50 Telegram Stars"


def test_fragment_parsers():
    assert F.parse_cookies("stel_ssid=a; foo=b; stel_token=c;\nstel_ton_token=d") == \
        {"stel_ssid": "a", "stel_token": "c", "stel_ton_token": "d"}
    page = '<script>ajInit({"version":1,"apiUrl":"\\/api?hash=abc123DEF","x":1});' \
           'Wallet.init({"address":"0:abc","logged_in":true});</script>'
    assert F.parse_api_hash(page) == "abc123DEF"
    assert F.parse_wallet_init(page) == {"address": "0:abc", "logged_in": True}
    assert F.classify_error("Please pass KYC verification first") == "kyc"
    assert F.classify_error("No Telegram users found.") == "not_found"
    assert F.classify_error("Too many requests, try again later") == "rate_limit"
