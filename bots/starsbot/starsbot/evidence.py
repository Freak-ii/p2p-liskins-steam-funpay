"""Доказательства выдачи по заказу и пакет для поддержки/арбитража FunPay.

Зачем: покупатель может получить звёзды и потом требовать «отмену» (пример — заказ #LQN29K99: 1000 ⭐ оплачены на
Fragment за 14 секунд, через 5 дней — «Не пришло» и арбитраж). FunPay решает спор по доказательствам продавца,
поэтому по каждому заказу бот сам хранит:

  * откуда взят username: поле заказа, которое покупатель заполнил на FunPay при оплате, или его сообщение в чате
    (цитата и время), и подтверждение получателя покупателем до выдачи, если оно запрашивалось;
  * что ответил Fragment при проверке получателя (имя аккаунта, id получателя);
  * оплату на Fragment: транзакция TON (hex-хэш и ссылка tonviewer), кошелёк отправителя, адрес Fragment, сумма,
    комментарий платежа с Ref#…, seqno;
  * хронологию заказа и всю переписку в чате заказа (покупатель / бот / поддержка FunPay);
  * прошлые заказы покупателя на тот же аккаунт (подтверждённые — сильный довод, что аккаунт его).

Снимок сохраняется в data/evidence/<заказ>.json сразу после выдачи; пакет (zip: обращение, доказательства, чат,
html-квитанция, json) собирается одной кнопкой в панели или автоматически, когда в чат пишет арбитраж FunPay.
"""
from __future__ import annotations

import base64
import binascii
import html
import json
import re
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
# сообщение арбитража/поддержки FunPay или прямое требование доказательств
STAFF_RE = re.compile(r"предостав\w*\s+доказательств|подтверждающ\w*\s+выполнение|осуществите\s+возврат|"
                      r"арбитраж|служб\w*\s+поддержки|модератор|видео,?\s+подтверждающ|provide\s+(the\s+)?proof", re.I)


def _dt(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%d.%m.%Y %H:%M:%S") if ts else "—"
    except (TypeError, ValueError, OSError):
        return "—"


def tx_hex(h: str | None) -> str:
    """Хэш транзакции TON в hex (toncenter отдаёт base64, tonviewer открывает hex)."""
    h = str(h or "").strip()
    if not h or h.startswith("dry"):
        return ""
    if HEX64.match(h):
        return h.lower()
    for dec in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            raw = dec(h + "=" * (-len(h) % 4))
        except (binascii.Error, ValueError):
            continue
        if len(raw) == 32:
            return raw.hex()
    return ""


def tonviewer_tx(h: str | None) -> str:
    x = tx_hex(h)
    return f"https://tonviewer.com/transaction/{x}" if x else ""


def tonviewer_addr(addr: str | None) -> str:
    return f"https://tonviewer.com/{addr}" if addr else ""


def _row(r) -> dict:
    return {k: r[k] for k in r.keys()} if r is not None else {}


def _events(db, oid: str) -> list[dict]:
    out = []
    for e in db.events(oid):
        try:
            data = json.loads(e["data"] or "{}")
        except ValueError:
            data = {}
        out.append({"ts": e["ts"], "kind": e["kind"], "data": data})
    return out


EVENT_TITLES = {
    "created": "заказ оплачен на FunPay", "username_received": "получен username получателя",
    "username_ok": "Fragment нашёл аккаунт получателя", "greeting_with_username": "покупателю отправлено "
    "сообщение с получателем и предупреждением, что после отправки сменить получателя нельзя",
    "username_confirm_asked": "у покупателя запрошено подтверждение получателя",
    "username_confirmed": "покупатель подтвердил получателя", "username_confirm_timeout": "покупатель не "
    "возразил против получателя за отведённое время", "username_changed": "покупатель сменил получателя",
    "ready": "заказ в очереди на оплату", "purchase_started": "создана оплата на Fragment",
    "tx_sent": "транзакция TON отправлена", "delivered": "транзакция подтверждена в блокчейне — товар выдан",
    "confirm_reminder": "покупателю отправлено напоминание подтвердить заказ", "evidence_saved": "сохранён снимок "
    "доказательств", "arbitration": "в чат написала поддержка/арбитраж FunPay", "support_file": "подготовлен файл "
    "для поддержки", "support_pack": "подготовлен пакет доказательств", "evidence_posted": "доказательства "
    "отправлены в чат заказа", "risk_review": "заказ на проверке безопасности", "approved": "выдача одобрена продавцом",
}


def collect(db, o, *, wallet_address: str = "", seller: str = "", chat_limit: int = 200) -> dict:
    """Все доказательства по заказу из базы бота (без сети)."""
    o = _row(o)
    oid = o.get("order_id", "")
    events = _events(db, oid)
    created = next((e["data"] for e in events if e["kind"] == "created"), {})
    chat = [_row(m) for m in db.q("SELECT ts, direction, text, order_id FROM messages WHERE chat_id=? ORDER BY ts, id",
                                  (o.get("chat_id") or "",))][-chat_limit:] if o.get("chat_id") else []
    user = str(o.get("tg_username") or "")
    # откуда взят username
    src = str(o.get("username_source") or "")
    origin: dict = {"source": src, "username": user}
    if src == "order":
        origin["how"] = "покупатель сам указал username в поле заказа на FunPay при оплате"
        origin["order_field"] = created.get("tg_from_order") or user
        if created.get("params"):
            origin["order_params"] = created["params"]
    elif src.startswith("chat") and user:
        quote = next((m for m in chat if m["direction"] == "in" and user.lower() in str(m["text"] or "").lower()
                      and (m.get("order_id") in (None, oid))), None)
        origin["how"] = "покупатель прислал username в чат заказа"
        if quote:
            origin["quote"] = {"ts": quote["ts"], "text": quote["text"]}
    elif src.startswith("admin"):
        origin["how"] = "username указан продавцом вручную"
    confirm = next((e for e in reversed(events) if e["kind"] in ("username_confirmed", "username_confirm_timeout")),
                   None)
    notice = next((e for e in events if e["kind"] in ("greeting_with_username", "username_confirm_asked")), None)
    origin["confirmation"] = ({"kind": confirm["kind"], "ts": confirm["ts"], **confirm["data"]} if confirm else None)
    origin["notice_ts"] = notice["ts"] if notice else None
    # оплата
    pays = []
    for p in db.q("SELECT * FROM purchases WHERE order_id=? ORDER BY id", (oid,)):
        p = _row(p)
        pays.append({"state": p.get("state"), "stars": p.get("stars"), "dest": p.get("dest"),
                     "amount_ton": round(int(p.get("amount_nano") or 0) / 1e9, 6), "comment": p.get("comment"),
                     "seqno": p.get("seqno"), "msg_hash": p.get("msg_hash"), "tx_hash": p.get("tx_hash"),
                     "tx_hex": tx_hex(p.get("tx_hash")), "tonviewer": tonviewer_tx(p.get("tx_hash")),
                     "created_at": p.get("created_at"), "updated_at": p.get("updated_at")})
    gifts = [_row(g) for g in db.q("SELECT * FROM gift_sends WHERE order_id=? ORDER BY seq", (oid,))] \
        if o.get("delivery") == "gift" else []
    # прошлые заказы покупателя
    hist = []
    if o.get("buyer_id"):
        for r in db.q("SELECT order_id, stars, tg_username, status, fp_status, delivered_at FROM orders WHERE "
                      "buyer_id=? AND order_id<>? ORDER BY created_at", (o["buyer_id"], oid)):
            hist.append(_row(r))
    same = [h for h in hist if user and str(h.get("tg_username") or "").lower() == user.lower()]
    return {
        "generated_at": time.time(), "seller": seller, "wallet": wallet_address,
        "order": {k: o.get(k) for k in ("order_id", "buyer_username", "buyer_id", "chat_id", "description", "stars",
                                        "premium_months", "quantity", "price", "currency", "status", "fp_status",
                                        "delivery", "tg_username", "tg_name", "tg_recipient", "paid_at",
                                        "delivered_at", "completed_at", "fragment_ref", "ton_amount", "tx_hash")},
        "funpay_url": f"https://funpay.com/orders/{oid}/",
        "recipient": origin,
        "payment": {"tx_hash": o.get("tx_hash"), "tx_hex": tx_hex(o.get("tx_hash")),
                    "tonviewer": tonviewer_tx(o.get("tx_hash")), "fragment_ref": o.get("fragment_ref"),
                    "ton_amount": o.get("ton_amount"), "wallet": wallet_address,
                    "wallet_link": tonviewer_addr(wallet_address), "purchases": pays},
        "gifts": gifts,
        "timeline": [{"ts": e["ts"], "kind": e["kind"], "title": EVENT_TITLES.get(e["kind"], e["kind"])}
                     for e in events],
        "chat": chat,
        "history": {"orders": hist, "same_recipient": same,
                    "same_recipient_confirmed": [h for h in same if h.get("fp_status") == "closed"
                                                 or h.get("status") == "completed"]},
    }


def _what(ev: dict) -> str:
    o = ev["order"]
    if o.get("delivery") == "premium":
        return f"Telegram Premium на {o.get('premium_months')} мес."
    if o.get("delivery") == "gift":
        return f"подарки Telegram на {o.get('stars')} ⭐"
    return f"{o.get('stars')} ⭐ Telegram Stars"


def proof_lines(ev: dict) -> list[str]:
    """Короткий список доводов — для чата арбитража и для обращения в поддержку."""
    o, r, p = ev["order"], ev["recipient"], ev["payment"]
    out = [f"Заказ #{o['order_id']}: {_what(ev)} на Telegram-аккаунт @{o.get('tg_username')}, выдано "
           f"{_dt(o.get('delivered_at'))} (оплата заказа {_dt(o.get('paid_at'))})."]
    if r.get("source") == "order":
        out.append(f"Получателя @{r.get('order_field') or o.get('tg_username')} покупатель сам указал в поле заказа "
                   "на FunPay при оплате.")
    elif r.get("quote"):
        out.append(f"Получателя покупатель прислал в этот чат {_dt(r['quote']['ts'])}: "
                   f"«{' '.join(str(r['quote']['text']).split())[:120]}».")
    c = r.get("confirmation")
    if c and c.get("kind") == "username_confirmed":
        out.append(f"Перед выдачей покупатель подтвердил получателя {_dt(c['ts'])}: «{str(c.get('text') or '')[:60]}».")
    elif r.get("notice_ts"):
        out.append(f"До отправки ({_dt(r['notice_ts'])}) покупатель получил в чате сообщение с получателем и "
                   "предупреждением, что после отправки сменить получателя нельзя; возражений не было.")
    if o.get("delivery") == "gift":
        out.append(f"Отправлено подарков: {len(ev['gifts'])} (история звёзд аккаунта продавца).")
    else:
        out.append("Оплачено через официальный сервис Telegram fragment.com" +
                   (f", платёж {p['fragment_ref']}" if p.get("fragment_ref") else "") +
                   (f", {float(p['ton_amount']):.4f} TON" if p.get("ton_amount") else "") + ".")
        if p.get("tonviewer"):
            out.append(f"Транзакция в блокчейне TON: {p['tonviewer']}")
        if p.get("wallet"):
            out.append(f"Кошелёк отправителя: {p['wallet']}")
    same = ev["history"]["same_recipient_confirmed"]
    if same:
        out.append(f"Этот же покупатель ранее подтвердил {len(same)} заказ(а) на тот же аккаунт @{o.get('tg_username')}: "
                   + ", ".join(f"#{h['order_id']}" for h in same[:5]) + ".")
    if o.get("delivery") != "gift":
        out.append("Звёзды через Fragment приходят на аккаунт сразу и отображаются в Telegram → Настройки → «Мои "
                   "звёзды» → история (зачисление от Fragment). Вернуть или отменить их после зачисления невозможно.")
    return out


def arbitration_message(ev: dict) -> str:
    """Ответ арбитражу FunPay в чат заказа (без ссылок, кроме tonviewer/fragment — это доказательства оплаты)."""
    lines = ["Здравствуйте! Доказательства выполнения заказа:"] + [f"• {x}" for x in proof_lines(ev)]
    lines.append("Полный пакет (хронология, переписка, данные транзакции) готов, пришлю в поддержку по запросу.")
    return "\n".join(lines)[:1900]


def render_text(ev: dict) -> str:
    o, p = ev["order"], ev["payment"]
    out = [f"ДОКАЗАТЕЛЬСТВА ВЫПОЛНЕНИЯ ЗАКАЗА #{o['order_id']}", f"Сформировано {_dt(ev['generated_at'])}"
           + (f" · продавец {ev['seller']}" if ev.get("seller") else ""), ev["funpay_url"], ""]
    out += proof_lines(ev) + [""]
    out += ["— Заказ —", f"Покупатель: {o.get('buyer_username')} (id {o.get('buyer_id')})",
            f"Товар: {o.get('description')} · {o.get('price') or 0:g} {o.get('currency') or '₽'}",
            f"Получатель: @{o.get('tg_username')} · Fragment: {o.get('tg_name') or '—'} · id получателя "
            f"{o.get('tg_recipient') or '—'}", f"Статус: {o.get('status')} / FunPay: {o.get('fp_status')}", ""]
    if p["purchases"]:
        out.append("— Оплата на Fragment (сеть TON) —")
        for x in p["purchases"]:
            out += [f"[{x['state']}] {x['stars'] or ''} · {x['amount_ton']} TON → {x['dest']}",
                    f"  комментарий: {' '.join(str(x['comment'] or '').split())}",
                    f"  seqno {x['seqno']} · хэш сообщения {x['msg_hash'] or '—'}",
                    f"  хэш транзакции: {x['tx_hex'] or x['tx_hash'] or '—'}" + (f" ({x['tx_hash']})" if x['tx_hex'] else ""),
                    f"  {x['tonviewer'] or ''}"]
        if p.get("wallet"):
            out.append(f"Кошелёк отправителя: {p['wallet']} — {p['wallet_link']}")
        out.append("")
    if ev["gifts"]:
        out.append("— Подарки —")
        for g in ev["gifts"]:
            out.append(f"  подарок {g.get('gift_id')} · {g.get('stars')} ⭐ · {g.get('state')} · {_dt(g.get('updated_at'))}")
        out.append("")
    out.append("— Хронология —")
    out += [f"  {_dt(e['ts'])}  {e['title']}" for e in ev["timeline"]]
    out += ["", "— Переписка в чате заказа —"]
    out += [f"  {line}" for line in chat_lines(ev)]
    if ev["history"]["orders"]:
        out += ["", "— Другие заказы покупателя —"]
        out += [f"  #{h['order_id']} · {h['stars']} ⭐ → @{h['tg_username']} · {h['status']}/{h['fp_status']} · "
                f"выдан {_dt(h['delivered_at'])}" for h in ev["history"]["orders"][-20:]]
    return "\n".join(out) + "\n"


WHO = {"in": "Покупатель", "out": "Продавец (бот)", "staff": "Поддержка FunPay", "seller": "Продавец"}


def chat_lines(ev: dict) -> list[str]:
    return [f"[{_dt(m['ts'])}] {WHO.get(m['direction'], m['direction'])}: {' '.join(str(m['text'] or '').split())}"
            for m in ev["chat"]]


def render_html(ev: dict) -> str:
    e = html.escape
    o = ev["order"]
    items = "".join(f"<li>{e(x)}</li>" for x in proof_lines(ev))
    tl = "".join(f"<tr><td>{e(_dt(t['ts']))}</td><td>{e(t['title'])}</td></tr>" for t in ev["timeline"])
    chat = "".join(f"<tr><td>{e(_dt(m['ts']))}</td><td>{e(WHO.get(m['direction'], m['direction']))}</td>"
                   f"<td>{e(str(m['text'] or ''))}</td></tr>" for m in ev["chat"])
    return (f"<!doctype html><meta charset=utf-8><title>Заказ #{e(o['order_id'])}</title><style>body{{font:14px "
            "system-ui;max-width:900px;margin:24px auto;padding:0 16px;color:#111}}td{{border-top:1px solid #ddd;"
            "padding:4px 8px;vertical-align:top}}table{{border-collapse:collapse;width:100%}}h1{{font-size:20px}}"
            f"</style><h1>Доказательства выполнения заказа #{e(o['order_id'])}</h1><p>{e(ev['funpay_url'])}</p>"
            f"<ul>{items}</ul><h2>Хронология</h2><table>{tl}</table><h2>Переписка</h2><table>{chat}</table>")


def ticket_text(evs: list[dict], seller: str = "") -> str:
    ids = ", ".join(f"#{ev['order']['order_id']}" for ev in evs)
    out = [f"Обращение в поддержку FunPay по заказ{'у' if len(evs) == 1 else 'ам'} {ids}",
           f"Продавец: {seller or '—'} · {_dt(time.time())}", "",
           "Здравствуйте! Товар по заказу выдан полностью и в срок. Доказательства ниже, подробности — в файлах "
           "пакета (хронология, переписка, данные транзакции в блокчейне TON).", ""]
    for ev in evs:
        out += proof_lines(ev) + [""]
    out.append("Прошу подтвердить выполнение заказа. Спасибо!")
    return "\n".join(out) + "\n"


def save_snapshot(db, o, folder: str | Path, **kw) -> Path:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    ev = collect(db, o, **kw)
    path = folder / f"{ev['order']['order_id']}.json"
    path.write_text(json.dumps(ev, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return path


def make_pack(db, orders, folder: str | Path, **kw) -> Path:
    """Zip для поддержки/арбитража: обращение + по каждому заказу txt, html, чат и json."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    evs = [collect(db, o, **kw) for o in orders]
    ids = "_".join(ev["order"]["order_id"] for ev in evs[:3]) + ("_etc" if len(evs) > 3 else "")
    path = folder / f"evidence_{ids}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("00_обращение_в_поддержку.txt", ticket_text(evs, kw.get("seller", "")))
        for ev in evs:
            oid = ev["order"]["order_id"]
            z.writestr(f"{oid}/доказательства.txt", render_text(ev))
            z.writestr(f"{oid}/квитанция.html", render_html(ev))
            z.writestr(f"{oid}/чат.txt", "\n".join(chat_lines(ev)) + "\n")
            z.writestr(f"{oid}/evidence.json", json.dumps(ev, ensure_ascii=False, indent=1, default=str))
    return path
