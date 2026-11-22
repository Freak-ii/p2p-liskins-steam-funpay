"""Чаттинг FunPay: без повторов «звёзды уже отправлены», одно напоминание о подтверждении, один шаблон без заказа."""
import asyncio
import time

from starsbot.core.models import ChatMessage, OrderStatus as S
from tests.test_engine import boot, drain, make


def _delivered(tmp_path, buyer_id=61):
    sup, e, fp, wallet = make(tmp_path)

    async def go():
        await boot(e)
        oid = fp.buyer_pays("Alice", buyer_id, 100, username="alice_tg")
        await drain(e, wallet)
        return oid
    oid = asyncio.run(go())
    assert sup.db.get_order(oid)["status"] == S.DELIVERED.value
    return sup, e, fp, wallet, oid


def _say(e, fp, buyer_id, text):
    fp.buyer_says(buyer_id, text)
    e._runner_bm_at = e._page_bm_at = 0
    asyncio.run(e.process_chat(str(500 + buyer_id), [fp.chats[str(500 + buyer_id)][-1]]))


def test_no_repeated_already_sent(tmp_path):
    sup, e, fp, wallet, oid = _delivered(tmp_path)
    n = len(fp.outbox)
    _say(e, fp, 61, "спасибо!")
    _say(e, fp, 61, "ок")
    assert len(fp.outbox) == n                          # благодарность — без ответа «уже отправлены»
    _say(e, fp, 61, "а когда придут?")
    assert len(fp.outbox) == n + 1                      # на первый вопрос после выдачи — один ответ
    _say(e, fp, 61, "а точно придут?")
    _say(e, fp, 61, "не пришли звезды")
    assert len(fp.outbox) == n + 1                      # дальше молчим, отвечает продавец
    assert sup.db.get_order(oid)["confirm_reminded"] == 1   # жалоба — «подтвердите» уже не напоминаем


def test_confirm_reminder_once_after_10_minutes(tmp_path):
    sup, e, fp, wallet, oid = _delivered(tmp_path, 62)
    assert asyncio.run(e.confirm_reminders()) == 0      # только что выдали — рано
    sup.db.update_order(oid, delivered_at=time.time() - 11 * 60)
    n = len(fp.outbox)
    assert asyncio.run(e.confirm_reminders()) == 1
    assert len(fp.outbox) == n + 1 and "подтвердите" in fp.outbox[-1][1] and fp.outbox[-1][0] == "562"
    assert asyncio.run(e.confirm_reminders()) == 0      # второе не шлём
    asyncio.run(e.timers())
    assert len(fp.outbox) == n + 1


def test_confirm_reminder_skips_confirmed_and_old(tmp_path):
    sup, e, fp, wallet, oid = _delivered(tmp_path, 63)
    sup.db.update_order(oid, delivered_at=time.time() - 8 * 3600)
    assert asyncio.run(e.confirm_reminders()) == 0      # старые выдачи после обновления не засыпаем напоминаниями
    sup.db.update_order(oid, delivered_at=time.time() - 11 * 60, fp_status="closed")
    assert asyncio.run(e.confirm_reminders()) == 0
    e.cfg.deal["confirm_reminder_minutes"] = 0
    sup.db.update_order(oid, fp_status="paid")
    assert asyncio.run(e.confirm_reminders()) == 0


def test_presale_template_once_per_chat(tmp_path):
    sup, e, fp, wallet = make(tmp_path)
    e.coord.chat_is_foreign = lambda chat_id: False
    n0 = len(fp.outbox)
    asyncio.run(e.on_chat_without_order("c9", ChatMessage(1, "c9", 7, "ivan", "хм")))
    assert len(fp.outbox) == n0 + 1
    sup.db.exec("UPDATE chat_state SET presale_at=? WHERE chat_id='c9'", (time.time() - 7 * 3600,))
    asyncio.run(e.on_chat_without_order("c9", ChatMessage(2, "c9", 7, "ivan", "привет, есть звёзды?")))
    assert len(fp.outbox) == n0 + 1                     # и через 7 часов не повторяем
    asyncio.run(e.on_chat_without_order("c8", ChatMessage(3, "c8", 8, "olga", "хочу подарок мишку")))
    assert len(fp.outbox) == n0 + 1                     # подарки — бот TG Gift


def test_same_auto_text_not_repeated(tmp_path):
    sup, e, fp, wallet = make(tmp_path)
    n = len(fp.outbox)
    asyncio.run(e.send_chat("c7", "текст"))
    asyncio.run(e.send_chat("c7", "текст"))
    assert len(fp.outbox) == n + 1
    asyncio.run(e.send_chat("c7", "текст", dedupe=False))
    assert len(fp.outbox) == n + 2
