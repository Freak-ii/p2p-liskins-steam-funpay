"""Доказательства по заказам: снимок после выдачи, ответ арбитражу, пакет zip, подтверждение получателя до выдачи."""
import asyncio
import json
import time
import zipfile

from starsbot import evidence as EV
from starsbot.core import messages as M
from starsbot.core.models import ChatMessage, OrderStatus as S
from tests.test_engine import boot, drain, make


def test_tx_hex_and_tonviewer():
    b64 = "p4Xch39GDundnX+HsYOxEVycJx+nsJL2EOJDbDxFAPQ="          # хэш из заказа #LQN29K99 (toncenter, base64)
    hx = "a785dc877f460ee9dd9d7f87b183b1115c9c271fa7b092f610e2436c3c4500f4"
    assert EV.tx_hex(b64) == hx and EV.tx_hex(hx.upper()) == hx
    assert EV.tonviewer_tx(b64) == f"https://tonviewer.com/transaction/{hx}"
    assert EV.tx_hex("dry-run") == "" and EV.tonviewer_tx("") == ""


def test_detect_cancel_and_yes_no():
    assert M.detect_faq("Отмени") == "faq_refund"
    assert M.is_yes("Да") and M.is_yes("да, всё верно") and M.is_yes("+") and M.is_yes("ok")
    assert M.is_no("нет, не тот") and not M.is_yes("не верно") and not M.is_yes("нет")


def _deliver(tmp_path, buyer_id=71, username="dddeqj", **over):
    over.setdefault("deal", {"confirm_username": "off"})
    sup, e, fp, wallet = make(tmp_path, **over)
    e.fragment.users.update({"dddeqj": "@dddeqj", "other_acc": "Other"})

    async def go():
        await boot(e)
        oid = fp.buyer_pays("danil", buyer_id, 100, username=username)
        await drain(e, wallet)
        return oid
    oid = asyncio.run(go())
    return sup, e, fp, wallet, oid


def test_snapshot_saved_on_delivery(tmp_path):
    sup, e, fp, wallet, oid = _deliver(tmp_path)
    o = sup.db.get_order(oid)
    assert o["status"] == S.DELIVERED.value
    snap = e.evidence_folder() / f"{oid}.json"
    assert snap.exists()
    ev = json.loads(snap.read_text(encoding="utf-8"))
    assert ev["recipient"]["source"] == "order" and ev["recipient"]["order_field"] == "dddeqj"
    assert ev["recipient"]["order_params"]["Username в Telegram"] == "dddeqj"
    assert ev["payment"]["purchases"] and ev["payment"]["purchases"][0]["state"] == "confirmed"
    lines = EV.proof_lines(ev)
    assert any("сам указал в поле заказа" in x for x in lines)
    assert any(e2["kind"] == "evidence_saved" for e2 in (dict(r) for r in sup.db.events(oid)))


def test_arbitrator_gets_evidence_not_faq(tmp_path):
    sup, e, fp, wallet, oid = _deliver(tmp_path, 72)
    chat = "572"
    n = len(fp.outbox)
    arb = ChatMessage(9_000_001, chat, 777, "Арбитраж", f"Jojk, пожалуйста, предоставьте доказательства, "
                      f"подтверждающие выполнение заказа #{oid} или осуществите возврат средств покупателю.")
    asyncio.run(e.process_chat(chat, [arb]))
    out = [t for c, t in fp.outbox[n:] if c == chat]
    assert len(out) == 1 and "Доказательства выполнения заказа" in out[0]
    assert "сам указал в поле заказа" in out[0] and "Мои звёзды" in out[0]
    assert "уже отправлены ✅" not in out[0]                     # не автоответ покупателю
    kinds = [r["kind"] for r in sup.db.events(oid)]
    assert "arbitration" in kinds and "evidence_posted" in kinds and "support_pack" in kinds
    assert sup.db.scalar("SELECT COUNT(*) FROM messages WHERE order_id=? AND direction='staff'", (oid,)) == 1
    assert any(lvl == "CRITICAL" and "Арбитраж" in t for lvl, t in e.notifier.sent)
    # пакет для поддержки
    pack = sorted(e.evidence_folder().glob(f"evidence_{oid}_*.zip"))[-1]
    with zipfile.ZipFile(pack) as z:
        names = z.namelist()
        assert "00_обращение_в_поддержку.txt" in names and f"{oid}/доказательства.txt" in names
        assert f"{oid}/чат.txt" in names and f"{oid}/квитанция.html" in names
        text = z.read(f"{oid}/доказательства.txt").decode()
        assert "Поддержка FunPay" in text and "Хронология" in text
    # повтор арбитража в течение 30 мин — без второго сообщения, но с новым алертом
    asyncio.run(e.process_chat(chat, [ChatMessage(9_000_002, chat, 777, "Арбитраж", "Жду доказательства")]))
    assert len([t for c, t in fp.outbox[n:] if c == chat]) == 1
    # кнопка «Доказательства в чат» — отправляет всегда
    res = asyncio.run(e.handle_command("evidence", [oid]))
    assert "отправлены в чат" in res and len([t for c, t in fp.outbox[n:] if c == chat]) == 2


def test_buyer_cancel_after_delivery_is_not_arbitration(tmp_path):
    sup, e, fp, wallet, oid = _deliver(tmp_path, 73)
    fp.buyer_says(73, "Отмени")
    asyncio.run(e.process_chat("573", [fp.chats["573"][-1]]))
    kinds = [r["kind"] for r in sup.db.events(oid)]
    assert "arbitration" not in kinds
    assert any("просит возврат" in t or "не получил" in t for _, t in e.notifier.sent)


def test_confirm_username_yes(tmp_path):
    sup, e, fp, wallet, oid = _deliver(tmp_path, 74, deal={"confirm_username": "always"})
    o = sup.db.get_order(oid)
    assert o["status"] == S.AWAITING_USERNAME.value and o["tg_recipient"]
    assert "ответьте «Да»" in fp.outbox[-1][1] and "@dddeqj" in fp.outbox[-1][1]
    fp.buyer_says(74, "да")
    asyncio.run(e.process_chat("574", [fp.chats["574"][-1]]))
    asyncio.run(drain(e, wallet))
    o = sup.db.get_order(oid)
    assert o["status"] == S.DELIVERED.value
    ev = e.evidence(o)
    assert ev["recipient"]["confirmation"]["kind"] == "username_confirmed"
    assert any("подтвердил получателя" in x for x in EV.proof_lines(ev))


def test_confirm_username_change_and_no(tmp_path):
    sup, e, fp, wallet, oid = _deliver(tmp_path, 75, deal={"confirm_username": "always"})
    fp.buyer_says(75, "нет")
    asyncio.run(e.process_chat("575", [fp.chats["575"][-1]]))
    o = sup.db.get_order(oid)
    assert o["status"] == S.AWAITING_USERNAME.value and not o["tg_username"]
    assert "правильный username" in fp.outbox[-1][1]
    fp.buyer_says(75, "@other_acc")
    asyncio.run(e.process_chat("575", [fp.chats["575"][-1]]))
    o = sup.db.get_order(oid)
    assert o["tg_username"] == "other_acc" and e.confirm_pending(o)      # новый получатель — снова «Да?»
    fp.buyer_says(75, "@other_acc")                                      # повтор того же ника = подтверждение
    asyncio.run(e.process_chat("575", [fp.chats["575"][-1]]))
    asyncio.run(drain(e, wallet))
    assert sup.db.get_order(oid)["status"] == S.DELIVERED.value


def test_confirm_username_timeout_delivers(tmp_path):
    sup, e, fp, wallet, oid = _deliver(tmp_path, 76, deal={"confirm_username": "always"})
    asyncio.run(e.timers())
    assert sup.db.get_order(oid)["status"] == S.AWAITING_USERNAME.value     # ещё рано
    sup.db.kv_set(f"ucfm:{oid}", time.time() - 16 * 60)
    asyncio.run(e.timers())
    asyncio.run(drain(e, wallet))
    o = sup.db.get_order(oid)
    assert o["status"] == S.DELIVERED.value
    assert e.evidence(o)["recipient"]["confirmation"]["kind"] == "username_confirm_timeout"


def test_confirm_new_or_big(tmp_path):
    sup, e, fp, wallet, oid = _deliver(tmp_path, 77, deal={"confirm_username": "new_or_big",
                                                           "confirm_username_min_stars": 1000})
    assert sup.db.get_order(oid)["status"] == S.AWAITING_USERNAME.value      # новый покупатель — спрашиваем
    sup.db.update_order(oid, status="completed", fp_status="closed", delivered_at=time.time())
    o2 = fp.buyer_pays("danil", 77, 100, username="dddeqj")
    asyncio.run(drain(e, wallet))
    assert sup.db.get_order(o2)["status"] == S.DELIVERED.value               # тот же аккаунт уже подтверждал
