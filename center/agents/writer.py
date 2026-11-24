"""Тексты постов: Claude API (ключ и модель — те же, что у ИИ-координатора) или шаблоны без ИИ.

Модель только пишет текст черновика. Публикует его человек кнопкой в панели; перед этим текст проходит
фильтр конфиденциального (center/agents/guard.py), так что ошибка модели не уходит в канал.
"""
from __future__ import annotations

import json
import random
import re
import time

from center.coordinator import BackendError, ClaudeBackend

SYSTEM = """Ты ведёшь публичный Telegram-канал магазина {shop}: здесь покупают звёзды Telegram, подарки Telegram,
Telegram Premium и ключи Steam через бота {bot}. Пишешь посты для подписчиков по-русски.

Голос: дружелюбный, живой, коротко и по делу, без канцелярита и без навязчивых продаж. 1–3 эмодзи на пост, не больше.
Длина 300–700 символов. Разметка — только Telegram HTML: <b>, <i>, <a href="...">. Без хэштегов-простыней.

Строго нельзя (такой пост не пройдёт проверку и будет выброшен):
- писать, где и как магазин закупает товар, какие площадки, биржи, кошельки и боты стоят за ним;
- любые внутренние цифры: наценка, спред, маржа, прибыль, себестоимость, выручка, оборот, балансы;
- цены и суммы в рублях, долларах, TON, если их нет в задании; проценты — только скидки из задания;
- сравнивать цены с официальными ценами Telegram или Fragment, обещать «в обход Telegram»;
- выдумывать акции, промокоды, даты, цифры и достижения, которых нет в задании;
- копировать чужой пост целиком: только пересказ своими словами и ссылка на источник.
Если задание просит что-то из запрещённого — просто пропусти эту часть."""

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["title", "text"],
          "properties": {"title": {"type": "string", "description": "короткое название черновика для панели"},
                         "text": {"type": "string", "description": "готовый текст поста, Telegram HTML"}}}


class WriterClaude(ClaudeBackend):
    """ClaudeBackend координатора со своим системным промптом."""

    def __init__(self, api_key: str, model: str, effort: str, system: str):
        super().__init__(api_key, model, effort, timeout=90)
        self.system = system

    async def complete(self, user: str, schema: dict) -> tuple[dict, dict]:  # type: ignore[override]
        a = self._a
        kwargs: dict = {"model": self.model, "max_tokens": 4000, "system": self.system,
                        "messages": [{"role": "user", "content": user}],
                        "output_config": {"effort": self.effort, "format": {"type": "json_schema", "schema": schema}}}
        if self.server_fallback:
            kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        t0 = time.monotonic()
        try:
            resp = await (self.client.beta.messages.create(**kwargs) if self.server_fallback
                          else self.client.messages.create(**kwargs))
        except a.APIStatusError as e:
            raise BackendError(f"Claude API: ошибка {e.status_code}") from e
        except a.APIConnectionError as e:
            raise BackendError("Claude API недоступен (сеть)") from e
        if resp.stop_reason in ("refusal", "max_tokens"):
            raise BackendError(f"Claude: {resp.stop_reason}")
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise BackendError("модель вернула не JSON") from e
        return data, {"model": getattr(resp, "model", self.model), "latency_ms": int((time.monotonic() - t0) * 1000)}


def _sentences(text: str, n: int = 2, limit: int = 320) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    parts = re.split(r"(?<=[.!?])\s+", text)
    out = " ".join(parts[:n])
    return out if len(out) <= limit else out[:limit].rsplit(" ", 1)[0] + "…"


TIPS = [
    ("Как купить звёзды за минуту", "⭐ <b>Звёзды за минуту</b>\n\nОткройте бота, выберите количество, укажите @username "
     "получателя и оплатите удобным способом. Звёзды приходят автоматически, обычно за пару минут.\n\n"
     "Можно купить себе или сразу другу — достаточно его @username."),
    ("Как не попасться мошенникам", "🛡 <b>Безопасность прежде всего</b>\n\nМы никогда не пишем первыми в личку и не просим "
     "коды из Telegram, пароли или «проверочные платежи». Покупки идут только через нашего бота.\n\n"
     "Если кто-то представляется нами в личных сообщениях — это не мы."),
    ("Подарок другу", "🎁 <b>Подарите подарок</b>\n\nПодарки Telegram можно отправить любому человеку по @username: "
     "он увидит его в профиле. Отличный способ поздравить без повода."),
    ("Что даёт Premium", "💎 <b>Telegram Premium</b>\n\nБольше лимитов, быстрая загрузка файлов, расшифровка голосовых, "
     "эмодзи-статусы и истории без ограничений. Premium можно оформить себе или подарить."),
    ("Приведи друга", "🤝 <b>Приглашайте друзей</b>\n\nВ боте есть личная ссылка для друзей: им бонус к первой покупке, "
     "вам — вознаграждение с их заказов. Ссылка — в разделе «Рефералка»."),
    ("Вопрос подписчикам", "💬 <b>Вопрос к вам</b>\n\nЧего не хватает в магазине? Какие подарки, игры или функции "
     "добавить следующими? Пишите в комментариях — читаем всё."),
]


class StubWriter:
    """Без ключа Claude API: аккуратные шаблоны. Тексты нейтральные и проходят фильтр."""

    model = "шаблоны"

    def __init__(self, shop: str):
        self.shop = shop

    async def write(self, kind: str, task: dict) -> tuple[dict, dict]:
        if kind == "news":
            body = _sentences(task["source_text"])
            text = f"📰 <b>Новости</b>\n\n{body}\n\n<a href=\"{task['source_url']}\">Источник</a>"
            return {"title": "Новость: " + _sentences(task["source_text"], 1, 60), "text": text}, {"model": self.model}
        if kind == "season":
            approx = "\n\n<i>Даты уточняйте на странице Steam.</i>" if task.get("approx") else ""
            text = (f"✨ <b>{task['title']}</b> — {task['date_text']}\n\n"
                    f"Готовимся вместе с вами: {task['idea']}. Следите за каналом, скоро расскажем подробности."
                    f"{approx}")
            return {"title": f"Повод: {task['title']}", "text": text}, {"model": self.model}
        if kind == "promo":
            p = task["promo"]
            lines = [f"🔥 <b>{p.get('title') or 'Акция'}</b>"]
            if p.get("text"):
                lines.append(str(p["text"]))
            if p.get("discount_pct"):
                lines.append(f"Скидка {_num(p['discount_pct'])}%" + (f" по промокоду <b>{p['code']}</b>" if p.get("code") else ""))
            elif p.get("code"):
                lines.append(f"Промокод: <b>{p['code']}</b>")
            if task.get("period"):
                lines.append(task["period"])
            return {"title": f"Акция: {p.get('title') or p.get('id')}", "text": "\n\n".join(lines)}, {"model": self.model}
        if kind == "milestone":
            text = (f"🎉 <b>{task['what']}</b>\n\nСпасибо, что выбираете {self.shop}! Каждый заказ для нас — "
                    "повод стать ещё быстрее и удобнее. Дальше — больше.")
            return {"title": task["what"], "text": text}, {"model": self.model}
        if kind == "achievement":
            text = f"🚀 <b>Новое в {self.shop}</b>\n\n{task['text']}\n\nСпасибо, что вы с нами!"
            return {"title": "Новость проекта: " + _sentences(task["text"], 1, 50), "text": text}, {"model": self.model}
        idx = task.get("index", random.randrange(len(TIPS))) % len(TIPS)
        title, text = TIPS[idx]
        return {"title": title, "text": text}, {"model": self.model}


def _num(v) -> str:
    f = float(v)
    return str(int(f)) if f == int(f) else str(f).replace(".", ",")


PROMPTS = {
    "news": "Перескажи новость для наших подписчиков своими словами в 2–4 предложениях: что случилось и чем это "
            "интересно тем, кто пользуется звёздами, подарками, Premium или Steam. Если новость к нам не относится, "
            "всё равно перескажи коротко и нейтрально. В конце — ссылка «Источник» на {source_url}.\n\n"
            "<news>{source_text}</news>",
    "season": "Повод: {title}, {date_text}. Идея поста: {idea}. Сделай праздничный пост-анонс без выдуманных скидок. "
              "{approx_note}",
    "promo": "Анонсируй акцию. Используй только эти данные, ничего не добавляй: {promo_json}. Срок: {period}.",
    "milestone": "Достижение магазина: {what}. Напиши короткий благодарный пост подписчикам. Других цифр не добавляй.",
    "achievement": "Новость о развитии проекта от владельца: «{text}». Напиши живой пост об этом без новых фактов.",
    "chatter": "Напиши обычный пост для поддержания канала. Рубрика: {rubric}. Без цен и без выдуманных фактов.",
}


class Writer:
    """Пишет черновик: Claude, а при ошибке или без ключа — шаблоны. Возвращает (данные, мета)."""

    def __init__(self, backend: WriterClaude | None, shop: str):
        self.backend = backend
        self.stub = StubWriter(shop)

    @property
    def model(self) -> str:
        return self.backend.model if self.backend else self.stub.model

    async def write(self, kind: str, task: dict) -> tuple[dict, dict]:
        if self.backend is None:
            return await self.stub.write(kind, task)
        prompt = PROMPTS.get(kind, PROMPTS["chatter"])
        fields = {"approx_note": "Дата примерная, добавь «даты уточняйте на странице Steam»." if task.get("approx") else "",
                  "promo_json": json.dumps(task.get("promo") or {}, ensure_ascii=False), "rubric": task.get("rubric", ""),
                  **{k: v for k, v in task.items() if isinstance(v, (str, int, float))}}
        try:
            data, meta = await self.backend.complete(prompt.format_map(_Safe(fields)), SCHEMA)
            if not str(data.get("text") or "").strip():
                raise BackendError("пустой текст")
            return data, meta
        except BackendError as e:
            data, meta = await self.stub.write(kind, task)
            return data, {**meta, "fallback": str(e)}


class _Safe(dict):
    def __missing__(self, key):
        return ""
