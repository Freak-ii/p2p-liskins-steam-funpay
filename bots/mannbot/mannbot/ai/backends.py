"""Генераторы ответа: Claude API (боевой) и детерминированная заглушка (тесты, демо, без ключа)."""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

TOPICS = ("order_status", "trade_link_help", "delivery_help", "stock_question", "payment_or_refund",
          "complaint", "off_platform", "smalltalk", "other")

REPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string", "description": "Текст ответа покупателю"},
        "topic": {"type": "string", "enum": list(TOPICS)},
        "confidence": {"type": "number", "description": "0..1 — насколько ответ точно закрывает вопрос"},
        "needs_seller": {"type": "boolean", "description": "true — нужен живой продавец"},
        "reason": {"type": "string", "description": "Коротко для продавца: почему такой ответ"},
    },
    "required": ["reply", "topic", "confidence", "needs_seller", "reason"],
    "additionalProperties": False,
}

# Модели, для которых включаем серверный fallback при отказе классификатора безопасности
FALLBACK_MODELS = ("claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5")


class BackendError(Exception):
    pass


@dataclass
class Draft:
    reply: str
    topic: str
    confidence: float
    needs_seller: bool
    reason: str
    model: str = ""
    latency_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    raw: dict = field(default_factory=dict)


def _parse(data: dict) -> tuple[str, str, float, bool, str]:
    topic = data.get("topic") if data.get("topic") in TOPICS else "other"
    try:
        conf = max(0.0, min(1.0, float(data.get("confidence", 0))))
    except (TypeError, ValueError):
        conf = 0.0
    return (str(data.get("reply", "")).strip(), topic, conf, bool(data.get("needs_seller", True)),
            str(data.get("reason", ""))[:500])


class ClaudeBackend:
    """Один запрос Messages API на сообщение покупателя, ответ — JSON по схеме REPLY_SCHEMA."""

    def __init__(self, api_key: str, model: str, effort: str = "low", timeout: float = 40,
                 server_fallback: bool = True):
        try:
            import anthropic
        except ImportError as e:  # pragma: no cover - зависит от окружения
            raise BackendError("не установлен пакет anthropic: pip install -r requirements.txt") from e
        self._anthropic = anthropic
        self.client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout, max_retries=2)
        self.model = model
        self.effort = effort
        self.server_fallback = server_fallback and model in FALLBACK_MODELS

    async def generate(self, system: str, user: str) -> Draft:
        a = self._anthropic
        kwargs: dict = {
            "model": self.model, "max_tokens": 4000, "system": system,
            "messages": [{"role": "user", "content": user}],
            "output_config": {"effort": self.effort, "format": {"type": "json_schema", "schema": REPLY_SCHEMA}},
        }
        if self.server_fallback:
            kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        t0 = time.monotonic()
        try:
            resp = await (self.client.beta.messages.create(**kwargs) if self.server_fallback
                          else self.client.messages.create(**kwargs))
        except a.AuthenticationError as e:
            raise BackendError("Claude API: неверный API-ключ") from e
        except a.PermissionDeniedError as e:
            raise BackendError("Claude API: у ключа нет доступа к модели") from e
        except a.NotFoundError as e:
            raise BackendError(f"Claude API: модель {self.model} не найдена") from e
        except a.RateLimitError as e:
            raise BackendError("Claude API: превышен лимит запросов") from e
        except a.APIStatusError as e:
            raise BackendError(f"Claude API: ошибка {e.status_code}: {getattr(e, 'message', e)}") from e
        except a.APIConnectionError as e:
            raise BackendError("Claude API недоступен (сеть)") from e
        latency = int((time.monotonic() - t0) * 1000)
        if resp.stop_reason == "refusal":
            raise BackendError("Claude отказался отвечать на это сообщение")
        if resp.stop_reason == "max_tokens":
            raise BackendError("ответ модели обрезан (max_tokens)")
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise BackendError("модель вернула не JSON") from e
        reply, topic, conf, needs, reason = _parse(data)
        usage = resp.usage
        return Draft(reply, topic, conf, needs, reason, model=getattr(resp, "model", self.model),
                     latency_ms=latency, tokens_in=int(getattr(usage, "input_tokens", 0) or 0),
                     tokens_out=int(getattr(usage, "output_tokens", 0) or 0), raw=data)


class StubBackend:
    """Заглушка без сети: темы по ключевым словам, ответы из фактов заказа. Для тестов и демо."""

    model = "stub"

    async def generate(self, system: str, user: str) -> Draft:
        facts = json.loads(user.split("<facts>", 1)[1].split("</facts>", 1)[0]) if "<facts>" in user else {}
        msg = user.split("<new_message>", 1)[1].split("</new_message>", 1)[0] if "<new_message>" in user else ""
        t = msg.lower()
        o = facts.get("order") or {}
        oid = o.get("order_id", "")
        if re.search(r"верн|возврат|refund|деньги|money", t):
            return Draft("Передал ваш вопрос продавцу, он ответит здесь, в чате заказа.", "payment_or_refund", 0.9,
                         True, "возврат решает продавец", model=self.model)
        if re.search(r"телег|telegram|discord|дискорд|вк|контакт", t):
            return Draft("По правилам FunPay всё общение и сделки только здесь, в чате заказа.", "off_platform",
                         0.9, False, "просьба о контактах", model=self.model)
        if re.search(r"ссылк|трейд|обмен|trade|link", t):
            return Draft("Ссылку на обмен можно взять в Steam: Инвентарь → Предложения обмена → «Кто может "
                         "отправлять мне предложения обмена?» или на странице "
                         "https://steamcommunity.com/my/tradeoffers/privacy. Пришлите её сюда, в чат"
                         + (f" заказа #{oid}." if oid else "."), "trade_link_help", 0.92, False,
                         "инструкция по трейд-ссылке", model=self.model)
        if oid and re.search(r"где|когда|скоро|долго|статус|status|when|where", t):
            return Draft(f"Заказ #{oid}: {o.get('status_text', 'в работе')}.", "order_status", 0.9, False,
                         "статус из базы", model=self.model)
        if re.search(r"налич|есть ли|сколько ключ|in stock", t):
            stock = facts.get("stock")
            return Draft(f"Сейчас в наличии {stock} ключей." if stock else "Наличие уточнит продавец.",
                         "stock_question", 0.7 if stock else 0.3, not stock, "наличие из склада", model=self.model)
        return Draft("Передал ваше сообщение продавцу, он скоро ответит здесь, в чате.", "other", 0.3, True,
                     "непонятный вопрос", model=self.model)
