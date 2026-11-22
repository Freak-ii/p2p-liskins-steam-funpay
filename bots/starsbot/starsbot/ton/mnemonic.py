"""Сид-фраза кошелька: 12 или 24 слова, как в Tonkeeper.

Tonkeeper умеет два вида фраз:
  • «TON» — собственный формат TON (обычно 24 слова, но бывает и 12): ключ = PBKDF2(HMAC(фраза), «TON default seed»);
  • «BIP39» — общий стандарт (12 слов, как у MetaMask/Trust/MyTonWallet): ключ по пути m/44'/607'/0' (SLIP-0010).

Какой формат у фразы, по самим словам не всегда понятно, поэтому бот строит ключи обоих видов и сам выбирает тот,
что подходит к адресу кошелька (или к публичному ключу в контракте). Адрес кошелька можно не вводить: бот
вычисляет адреса W5 (v5r1) и v4r2 для каждого ключа и находит тот, на котором есть деньги/история.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import unicodedata
from dataclasses import dataclass

from nacl.bindings import crypto_sign_seed_keypair
from pytoniq_core import Address, Cell, StateInit, begin_cell
from pytoniq_core.crypto.keys import mnemonic_to_private_key, words as BIP39_WORDS

# Коды стандартных кошельков (те же, что в Tonkeeper / pytoniq)
V4R2_CODE_B64 = (
    "te6ccgECFAEAAtQAART/APSkE/S88sgLAQIBIAIPAgFIAwYC5tAB0NMDIXGwkl8E4CLXScEgkl8E4ALTHyGCEHBsdWe9IoIQZHN0cr2wkl8F4AP6Q"
    "DAg+kQByMoHy//J0O1E0IEBQNch9AQwXIEBCPQKb6Exs5JfB+AF0z/IJYIQcGx1Z7qSODDjDQOCEGRzdHK6kl8G4w0EBQB4AfoA9AQw+CdvIjBQ"
    "CqEhvvLgUIIQcGx1Z4MesXCAGFAEywUmzxZY+gIZ9ADLaRfLH1Jgyz8gyYBA+wAGAIpQBIEBCPRZMO1E0IEBQNcgyAHPFvQAye1UAXKwjiOCEGR"
    "zdHKDHrFwgBhQBcsFUAPPFiP6AhPLassfyz/JgED7AJJfA+ICASAHDgIBIAgNAgFYCQoAPbKd+1E0IEBQNch9AQwAsjKB8v/ydABgQEI9ApvoTGA"
    "CASALDAAZrc52omhAIGuQ64X/wAAZrx32omhAEGuQ64WPwAARuMl+1E0NcLH4AFm9JCtvaiaECAoGuQ+gIYRw1AgIR6STfSmRDOaQPp/5g3gSgB"
    "t4EBSJhxWfMYQE+PKDCNcYINMf0x/THwL4I7vyZO1E0NMf0x/T//QE0VFDuvKhUVG68qIF+QFUEGT5EPKj+AAkpMjLH1JAyx9SMMv/UhD0AMntV"
    "PgPAdMHIcAAn2xRkyDXSpbTB9QC+wDoMOAhwAHjACHAAuMAAcADkTDjDQOkyMsfEssfy/8QERITAG7SB/oA1NQi+QAFyMoHFcv/ydB3dIAYyMsF"
    "ywIizxZQBfoCFMtrEszMyXP7AMhAFIEBCPRR8qcCAHCBAQjXGPoA0z/IVCBHgQEI9FHyp4IQbm90ZXB0gBjIywXLAlAGzxZQBPoCFMtqEssfyz/"
    "Jc/sAAgBsgQEI1xj6ANM/MFIkgQEI9Fnyp4IQZHN0cnB0gBjIywXLAlAFzxZQA/oCE8tqyx8Syz/Jc/sAAAr0AMntVA==")
V5R1_CODE_B64 = (
    "te6ccgECFAEAAoEAART/APSkE/S88sgLAQIBIAINAgFIAwQC3NAg10nBIJFbj2Mg1wsfIIIQZXh0br0hghBzaW50vbCSXwPgghBleHRuuo60gCDXI"
    "QHQdNch+kAw+kT4KPpEMFi9kVvg7UTQgQFB1yH0BYMH9A5voTGRMOGAQNchcH/bPOAxINdJgQKAuZEw4HDiEA8CASAFDAIBIAYJAgFuBwgAGa3Odq"
    "JoQCDrkOuF/8AAGa8d9qJoQBDrkOuFj8ACAUgKCwAXsyX7UTQcdch1wsfgABGyYvtRNDXCgCAAGb5fD2omhAgKDrkPoCwBAvIOAR4g1wsfghBzaW"
    "duuvLgin8PAeaO8O2i7fshgwjXIgKDCNcjIIAg1yHTH9Mf0x/tRNDSANMfINMf0//XCgAK+QFAzPkQmiiUXwrbMeHywIffArNQB7Dy0IRRJbry4I"
    "VQNrry4Ib4I7vy0IgikvgA3gGkf8jKAMsfAc8Wye1UIJL4D95w2zzYEAP27aLt+wL0BCFukmwhjkwCIdc5MHCUIccAs44tAdcoIHYeQ2wg10nACPL"
    "gkyDXSsAC8uCTINcdBscSwgBSMLDy0InXTNc5MAGk6GwShAe78uCT10rAAPLgk+1V4tIAAcAAkVvg69csCBQgkXCWAdcsCBwS4lIQseMPINdKERIT"
    "AJYB+kAB+kT4KPpEMFi68uCR7UTQgQFB1xj0BQSdf8jKAEAEgwf0U/Lgi44UA4MH9Fvy4Iwi1woAIW4Bs7Dy0JDiyFADzxYS9ADJ7VQAcjDXLAgkj"
    "i0h8uCS0gDtRNDSAFETuvLQj1RQMJExnAGBAUDXIdcKAPLgjuLIygBYzxbJ7VST8sCN4gAQk1vbMeHXTNA=")

V4R2_WALLET_ID = 698983191                      # 0x29a9a317 — стандартный subwallet для workchain 0
V5R1_WALLET_ID = ((1 << 31) ^ -239) & 0xFFFFFFFF  # клиентский контекст (wc 0, версия 0, subwallet 0) ^ mainnet id
BIP39_PATH = (44, 607, 0)                       # m/44'/607'/0' — как у Tonkeeper и MyTonWallet для 12 слов
_BIP39_INDEX = {w: i for i, w in enumerate(BIP39_WORDS)}


class MnemonicError(ValueError):
    pass


@dataclass
class KeyCandidate:
    kind: str                 # "ton" | "bip39"
    public_key: bytes
    secret: bytes             # 64 байта, формат NaCl (seed + pk)

    def address(self, version: str) -> Address:
        return wallet_address(self.public_key, version)


def normalize_words(mnemonic: str | list[str]) -> list[str]:
    if isinstance(mnemonic, str):
        raw = mnemonic.replace(",", " ").replace("\n", " ").split()
    else:
        raw = list(mnemonic)
    out = []
    for w in raw:
        w = re.sub(r"^\d+[.)]", "", w.strip().lower())     # «1.word» — нумерация при копировании из Tonkeeper
        if w and not w.isdigit():
            out.append(w)
    return out


def check_words(words: list[str]) -> None:
    if len(words) not in (12, 24):
        raise MnemonicError(f"в сид-фразе {len(words)} слов — нужно 12 или 24 (как показывает Tonkeeper)")
    unknown = [w for w in words if w not in _BIP39_INDEX]
    if unknown:
        raise MnemonicError("незнакомые слова в сид-фразе: " + ", ".join(unknown[:5]) +
                            " — проверьте опечатки (слова английские, из списка BIP39)")


def bip39_checksum_ok(words: list[str]) -> bool:
    bits = "".join(f"{_BIP39_INDEX[w]:011b}" for w in words)
    ent_len = len(bits) * 32 // 33
    ent, cs = bits[:ent_len], bits[ent_len:]
    data = int(ent, 2).to_bytes(ent_len // 8, "big")
    want = f"{hashlib.sha256(data).digest()[0]:08b}"[:len(cs)]
    return cs == want


def ton_is_basic_seed(words: list[str]) -> bool:
    """Проверка «это фраза формата TON» (как tonweb-mnemonic.isBasicSeed)."""
    entropy = hmac.new(" ".join(words).encode(), b"", hashlib.sha512).digest()
    seed = hashlib.pbkdf2_hmac("sha512", entropy, b"TON seed version", 100000 // 256)
    return seed[0] == 0


def bip39_seed(words: list[str], passphrase: str = "") -> bytes:
    m = unicodedata.normalize("NFKD", " ".join(words)).encode()
    salt = unicodedata.normalize("NFKD", "mnemonic" + passphrase).encode()
    return hashlib.pbkdf2_hmac("sha512", m, salt, 2048)


def slip10_ed25519(seed: bytes, path: tuple[int, ...] = BIP39_PATH) -> bytes:
    digest = hmac.new(b"ed25519 seed", seed, hashlib.sha512).digest()
    key, chain = digest[:32], digest[32:]
    for index in path:
        data = b"\x00" + key + (index | 0x80000000).to_bytes(4, "big")
        digest = hmac.new(chain, data, hashlib.sha512).digest()
        key, chain = digest[:32], digest[32:]
    return key


def candidates(mnemonic: str | list[str], kind: str = "auto") -> list[KeyCandidate]:
    """Возможные ключи для фразы. kind: auto | ton | bip39."""
    words = normalize_words(mnemonic)
    check_words(words)
    out: list[KeyCandidate] = []
    is_ton = ton_is_basic_seed(words)
    is_bip = bip39_checksum_ok(words)
    if kind in ("auto", "ton") and (is_ton or kind == "ton" or not is_bip):
        pk, sk = mnemonic_to_private_key(words)
        out.append(KeyCandidate("ton", pk, sk))
    if kind in ("auto", "bip39") and (is_bip or kind == "bip39"):
        pk, sk = crypto_sign_seed_keypair(slip10_ed25519(bip39_seed(words)))
        out.append(KeyCandidate("bip39", pk, sk))
    if not out:
        raise MnemonicError("сид-фраза не подходит ни под формат TON, ни под BIP39 — проверьте порядок слов")
    if kind == "auto" and len(words) == 12:
        out.sort(key=lambda c: 0 if c.kind == "bip39" else 1)   # 12 слов в Tonkeeper чаще всего BIP39
    return out


def wallet_data_cell(public_key: bytes, version: str) -> Cell:
    if version == "v5r1":
        return (begin_cell().store_bit(1).store_uint(0, 32).store_uint(V5R1_WALLET_ID, 32)
                .store_bytes(public_key).store_bit(0).end_cell())
    if version == "v4r2":
        return (begin_cell().store_uint(0, 32).store_uint(V4R2_WALLET_ID, 32).store_bytes(public_key)
                .store_bit(0).end_cell())
    raise MnemonicError(f"версия кошелька {version} не поддерживается (нужна v5r1 или v4r2)")


def wallet_code(version: str) -> Cell:
    return Cell.one_from_boc(base64.b64decode(V5R1_CODE_B64 if version == "v5r1" else V4R2_CODE_B64))


def wallet_state_init(public_key: bytes, version: str) -> StateInit:
    return StateInit(code=wallet_code(version), data=wallet_data_cell(public_key, version))


def wallet_address(public_key: bytes, version: str, wc: int = 0) -> Address:
    return Address((wc, wallet_state_init(public_key, version).serialize().hash))


def friendly(addr: Address) -> str:
    return addr.to_str(is_user_friendly=True, is_bounceable=False)


def all_addresses(mnemonic: str | list[str], kind: str = "auto") -> list[dict]:
    """Все адреса, которые может означать фраза: [{kind, version, address}] — для подсказки в панели."""
    out = []
    for c in candidates(mnemonic, kind):
        for v in ("v5r1", "v4r2"):
            out.append({"kind": c.kind, "version": v, "address": friendly(c.address(v))})
    return out


def match_address(mnemonic: str | list[str], address: str, kind: str = "auto") -> tuple[KeyCandidate, str] | None:
    """Ключ и версия кошелька, которым соответствует адрес. None — фраза не от этого адреса."""
    want = Address(address)
    for c in candidates(mnemonic, kind):
        for v in ("v5r1", "v4r2"):
            if c.address(v) == want:
                return c, v
    return None
