"""Шаблоны сообщений покупателю (ru/en) и распознавание его сообщений.

Правила FunPay, которые учтены в текстах:
  • п. 2.1.2 — подтверждение заказа просим ТОЛЬКО после фактической оплаты звёзд на Fragment;
  • п. 1.1   — username нужен только как данные для зачисления (так устроен раздел «По username»),
    в Telegram мы покупателю не пишем и контактами не обмениваемся, всё общение — в чате FunPay;
  • п. 1.12  — никаких сторонних ссылок в сообщениях;
  • п. 2.1.4 / 3.1.2 — вопросы покупателя не игнорируются: на любое сообщение есть ответ,
    непонятное пересылается продавцу;
  • п. 1.8 / 1.9 — без спама: ограничение частоты сообщений и одно напоминание.
"""
from __future__ import annotations

import re

TEMPLATES: dict[str, dict[str, str]] = {
    "ru": {
        "greeting_need_username": (
            "Здравствуйте, {buyer}! {returning}Спасибо за оплату заказа #{order_id} 🎉\n"
            "⭐ Товар: {stars} Telegram Stars\n"
            "🤖 Выдача автоматическая через официальную платформу Telegram — Fragment, обычно 1–5 минут.\n\n"
            "👉 Пришлите в этот чат ваш username в Telegram, например: @username\n{last_hint}"
            "Найти его: Telegram → Настройки → «Имя пользователя». Звёзды придут на этот аккаунт.\n\n"
            "ℹ️ Username нужен только для зачисления звёзд: мы не пишем вам в Telegram, всё общение — здесь, "
            "в чате FunPay. Пожалуйста, подтверждайте заказ только после того, как звёзды придут."
        ),
        "greeting_with_username": (
            "Здравствуйте, {buyer}! {returning}Спасибо за оплату заказа #{order_id} 🎉\n"
            "⭐ {stars} Telegram Stars → @{username}{name_part}\n"
            "🤖 Отправляю автоматически через официальную платформу Telegram — Fragment, обычно 1–5 минут.\n"
            "❗ Звёзды уходят сразу на username из заказа — после отправки сменить получателя или вернуть их нельзя.\n"
            "Пожалуйста, подтверждайте заказ только после того, как звёзды придут."
        ),
        "greeting_confirm_username": (
            "Здравствуйте, {buyer}! {returning}Спасибо за оплату заказа #{order_id} 🎉\n"
            "{what} → @{username}{name_part}\n"
            "❗ Проверьте получателя: после отправки сменить его или отменить отправку нельзя.\n"
            "👉 Всё верно — ответьте «Да». Аккаунт другой — пришлите правильный @username.{timeout_line}"
        ),
        "confirm_username": (
            "✅ Аккаунт найден: @{username}{name_part}.\n"
            "❗ {what} уйдут на этот аккаунт — после отправки сменить получателя нельзя.\n"
            "👉 Всё верно — ответьте «Да». Аккаунт другой — пришлите правильный @username.{timeout_line}"
        ),
        "username_confirm_thanks": "👍 Спасибо, получатель подтверждён: @{username}. Отправляю {what}…",
        "username_confirm_no": "Хорошо! Пришлите, пожалуйста, правильный username в Telegram (например, @username).",
        "username_confirm_hint": ("Подтвердите, пожалуйста, получателя @{username}: ответьте «Да» или пришлите другой "
                                  "@username."),
        "username_ok": "✅ Аккаунт найден: @{username}{name_part}. Отправляю {stars} ⭐…",
        "bad_username": (
            "❗ {buyer}, {reason}.\n"
            "Пришлите, пожалуйста, ваш username в Telegram ещё раз — латиницей, как в настройках Telegram "
            "(например, @username). Попытка {attempt} из {max_attempts}."
        ),
        "bad_username_final": (
            "⛔ {buyer}, не получилось найти аккаунт для зачисления ({reason}). Я передал заказ #{order_id} "
            "продавцу — он ответит вам здесь, в чате, в ближайшее время. Пожалуйста, не открывайте спор: решим вопрос."
        ),
        "delivered": (
            "🎉 Готово! {stars} ⭐ Telegram Stars отправлены на @{username} (заказ #{order_id}).\n"
            "Звёзды зачисляются в течение пары минут: Telegram → Настройки → «Мои звёзды».\n\n"
            "Огромное спасибо за покупку, {buyer} ❤️\n"
            "Пожалуйста, подтвердите выполнение заказа кнопкой «Подтвердить выполнение заказа» на странице "
            "заказа — и, если не сложно, оставьте отзыв. Это очень помогает нам!\n"
            "Будем рады видеть вас снова ⭐"
        ),
        "slow_network": (
            "⏳ Платёж по заказу #{order_id} уже отправлен, сеть TON подтверждает его чуть дольше обычного. "
            "Звёзды придут автоматически в ближайшие минуты, я напишу."
        ),
        "awaiting_approval": (
            "🔎 Заказ #{order_id} проходит стандартную проверку безопасности — обычно это несколько минут. "
            "Звёзды будут отправлены сразу после неё."
        ),
        "delay": (
            "⚠️ {buyer}, выдача заказа #{order_id} ненадолго задерживается по техническим причинам. Продавец уже "
            "уведомлён и выдаст звёзды в ближайшее время — или оформит возврат, если вы предпочтёте."
        ),
        "reminder": (
            "👋 {buyer}, напоминаю: чтобы получить {stars} ⭐ по заказу #{order_id}, пришлите ваш username в "
            "Telegram (например, @username)."
        ),
        "status": "ℹ️ Заказ #{order_id}: {status_text}",
        "help": (
            "Команды: «!статус» — состояние заказа, «!продавец» — позвать продавца. Чтобы получить звёзды, просто "
            "пришлите ваш username в Telegram (например, @username)."
        ),
        "call_seller": "🙋 Продавец уведомлён и ответит вам здесь, в чате, как можно скорее.",
        "off_platform": (
            "🙏 По правилам FunPay мы не общаемся и не проводим сделки вне площадки и не обмениваемся контактами. "
            "Все вопросы решаем здесь, в чате заказа."
        ),
        "unknown": (
            "Не совсем понял сообщение 🙂 Для выдачи звёзд по заказу #{order_id} нужен ваш username в Telegram "
            "(например, @username). Если есть вопрос — напишите «!продавец», и продавец ответит."
        ),
        "after_delivery": (
            "Звёзды по заказу #{order_id} уже отправлены на @{username} ✅ Если они не появились в течение 10 минут, "
            "откройте Telegram → Настройки → «Мои звёзды» или напишите «!продавец»."
        ),
        "change_username_late": (
            "Звёзды по заказу #{order_id} уже оплачены на @{username} — изменить получателя после отправки нельзя. "
            "Если это ошибка, напишите «!продавец»."
        ),
        "refunded": "💸 По заказу #{order_id} оформлен возврат средств.",
        "completed_thanks": "🙏 Спасибо за подтверждение заказа #{order_id}! Будем рады видеть вас снова ⭐",
        "presale_hello": (
            "Здравствуйте! 👋 Я на связи, отвечаю сразу, 24/7. Если вы за Telegram Stars — выдача автоматическая: "
            "выберите лот нужного номинала ({denominations} ⭐), оплатите и пришлите @username получателя — звёзды "
            "придут за 1–5 минут через Fragment. Есть вопросы — пишите здесь."
        ),
        "presale": (
            "Здравствуйте! 👋 Telegram Stars выдаются автоматически: выберите лот нужного номинала "
            "({denominations} ⭐), оплатите и укажите ваш username — звёзды придут за 1–5 минут через официальную "
            "платформу Telegram (Fragment). Если остались вопросы — напишите, продавец ответит здесь."
        ),
        "faq_gift": (
            "Звёзды зачисляются напрямую на аккаунт по username (не подарком). Можно указать username друга — "
            "звёзды придут ему."
        ),
        "faq_safe": (
            "Это безопасно: звёзды покупаются на официальной платформе Telegram — Fragment. Нам нужен только ваш "
            "username, никакие коды, пароли и вход в аккаунт не нужны. Никому их не сообщайте!"
        ),
        "faq_time": "Обычно звёзды приходят за 1–5 минут после оплаты и получения username. {status_hint}",
        "faq_not_arrived": (
            "Понимаю, проверяю 🙏 {status_hint}\n"
            "Звёзды видны в Telegram → Настройки → «Мои звёзды» на аккаунте @{username}. Иногда баланс обновляется "
            "с задержкой до 10 минут. Я уже сообщил продавцу — если звёзд не будет, он разберётся в этом чате."
        ),
        "faq_refund": (
            "Передал продавцу вашу просьбу по заказу #{order_id}, он ответит в этом чате. Если звёзды ещё не "
            "отправлены, возврат оформляется через FunPay. Пожалуйста, не подтверждайте заказ, пока вопрос не решён."
        ),
        "faq_not_arrived_done": (
            "Звёзды по заказу #{order_id} уже отправлены ✅ {delivered_at} — {stars} ⭐ на @{username}{ref_part}.\n"
            "Проверьте: Telegram → Настройки → «Мои звёзды» → история на аккаунте @{username} (не на другом "
            "аккаунте!). Баланс иногда обновляется до 10 минут. Если звёзд всё равно нет — напишите здесь, "
            "продавец уже видит ваше сообщение и проверит платёж."
        ),
        "faq_premium": ("В этом лоте продаются Telegram Stars (звёзды). Telegram Premium мы продаём отдельными лотами "
                        "в разделе FunPay «Telegram → Premium» — тоже с автовыдачей по username."),
        "faq_where": "Звёзды видны в Telegram → Настройки → «Мои звёзды» (Telegram Stars) на аккаунте получателя.",
        "greeting_need_username_gift": (
            "Здравствуйте, {buyer}! {returning}Спасибо за оплату заказа #{order_id} 🎉\n"
            "🎁 Товар: {stars} ⭐ подарками Telegram\n"
            "🤖 Выдача автоматическая: бот отправит подарки на ваш аккаунт, обычно за 1–5 минут.\n\n"
            "👉 Пришлите в этот чат ваш username в Telegram, например: @username\n{last_hint}"
            "Найти его: Telegram → Настройки → «Имя пользователя».\n\n"
            "ℹ️ Username нужен только для отправки подарков: мы не пишем вам в Telegram, всё общение — здесь, "
            "в чате FunPay. Пожалуйста, подтверждайте заказ только после того, как подарки придут."
        ),
        "greeting_with_username_gift": (
            "Здравствуйте, {buyer}! {returning}Спасибо за оплату заказа #{order_id} 🎉\n"
            "🎁 {stars} ⭐ подарками → @{username}{name_part}\n"
            "🤖 Отправляю автоматически, обычно 1–5 минут. Если username с ошибкой — сразу напишите правильный.\n"
            "Пожалуйста, подтверждайте заказ только после того, как подарки придут."
        ),
        "username_ok_gift": "✅ Аккаунт найден: @{username}{name_part}. Отправляю подарки ({gifts})…",
        "delivered_gift": (
            "🎉 Готово! Подарки отправлены на @{username} (заказ #{order_id}): {gifts}.\n"
            "Получить звёзды: откройте профиль → «Подарки» → нажмите на подарок → «Конвертировать в звёзды» — "
            "всего {value} ⭐.\n\n"
            "Огромное спасибо за покупку, {buyer} ❤️\n"
            "Пожалуйста, подтвердите выполнение заказа кнопкой «Подтвердить выполнение заказа» на странице "
            "заказа — и, если не сложно, оставьте отзыв. Это очень помогает нам!"
        ),
        "faq_gift_gift": (
            "Звёзды приходят подарками Telegram на ваш аккаунт. Каждый подарок можно оставить в профиле или "
            "конвертировать в звёзды: профиль → «Подарки» → подарок → «Конвертировать в звёзды»."
        ),
        "after_delivery_gift": (
            "Подарки по заказу #{order_id} уже отправлены на @{username} ✅ Они в профиле → «Подарки». "
            "Если что-то не так — напишите, продавец ответит здесь."
        ),
        "confirm_reminder": (
            "{buyer}, подскажите, пожалуйста: всё ли пришло по заказу #{order_id}? Если да — подтвердите, "
            "пожалуйста, выполнение заказа на его странице. Если есть проблема — напишите, разберёмся здесь в чате."
        ),
        "review_reply": "Спасибо за отзыв, {buyer}! Рады, что {stars} ⭐ дошли быстро. Будем рады видеть вас снова 🙌",
        "greeting_need_username_premium": (
            "Здравствуйте, {buyer}! {returning}Спасибо за оплату заказа #{order_id} 🎉\n"
            "💎 Товар: Telegram Premium на {months_text}\n"
            "🤖 Выдача автоматическая через официальную платформу Telegram — Fragment, обычно 1–5 минут. "
            "Входить в аккаунт не нужно: Premium приходит подарком от Telegram.\n\n"
            "👉 Пришлите в этот чат username получателя в Telegram, например: @username\n{last_hint}"
            "Найти его: Telegram → Настройки → «Имя пользователя».\n\n"
            "ℹ️ Username нужен только для подарка Premium: мы не пишем вам в Telegram, всё общение — здесь, "
            "в чате FunPay. Пожалуйста, подтверждайте заказ только после того, как Premium активируется."
        ),
        "greeting_with_username_premium": (
            "Здравствуйте, {buyer}! {returning}Спасибо за оплату заказа #{order_id} 🎉\n"
            "💎 Telegram Premium на {months_text} → @{username}{name_part}\n"
            "🤖 Оформляю автоматически через Fragment, обычно 1–5 минут. Входить в аккаунт не нужно.\n"
            "❗ Premium дарится сразу на username из заказа — после оплаты сменить получателя нельзя.\n"
            "Пожалуйста, подтверждайте заказ только после того, как Premium активируется."
        ),
        "username_ok_premium": "✅ Аккаунт найден: @{username}{name_part}. Оформляю Telegram Premium на {months_text}…",
        "delivered_premium": (
            "🎉 Готово! Telegram Premium на {months_text} подарен аккаунту @{username} (заказ #{order_id}).\n"
            "В Telegram придёт сообщение о подарке Premium — он активируется сам, обычно в течение пары минут. "
            "Проверить: Telegram → Настройки → Telegram Premium.\n\n"
            "Огромное спасибо за покупку, {buyer} ❤️\n"
            "Пожалуйста, подтвердите выполнение заказа кнопкой «Подтвердить выполнение заказа» на странице "
            "заказа — и, если не сложно, оставьте отзыв. Это очень помогает нам!"
        ),
        "reminder_premium": (
            "👋 {buyer}, напоминаю: чтобы получить Telegram Premium на {months_text} по заказу #{order_id}, "
            "пришлите username получателя в Telegram (например, @username)."
        ),
        "slow_network_premium": (
            "⏳ Оплата Premium по заказу #{order_id} уже отправлена, сеть TON подтверждает её чуть дольше обычного. "
            "Premium придёт автоматически в ближайшие минуты, я напишу."
        ),
        "awaiting_approval_premium": (
            "🔎 Заказ #{order_id} проходит стандартную проверку безопасности — обычно это несколько минут. "
            "Premium будет оформлен сразу после неё."
        ),
        "delay_premium": (
            "⚠️ {buyer}, выдача заказа #{order_id} ненадолго задерживается по техническим причинам. Продавец уже "
            "уведомлён и оформит Premium в ближайшее время — или вернёт деньги, если вы предпочтёте."
        ),
        "after_delivery_premium": (
            "Telegram Premium по заказу #{order_id} уже подарен аккаунту @{username} ✅ Если он не активировался "
            "за 10 минут, откройте чат «Telegram» в приложении или напишите «!продавец»."
        ),
        "change_username_late_premium": (
            "Premium по заказу #{order_id} уже оплачен на @{username} — сменить получателя после оплаты нельзя. "
            "Если это ошибка, напишите «!продавец»."
        ),
        "faq_not_arrived_done_premium": (
            "Premium по заказу #{order_id} уже оформлен ✅ {delivered_at} — {months_text} на @{username}{ref_part}.\n"
            "Проверьте на аккаунте @{username} (не на другом!): Telegram → Настройки → Telegram Premium, и сообщение "
            "о подарке в чате «Telegram». Если Premium всё равно нет — напишите здесь, продавец проверит платёж."
        ),
        "faq_premium_premium": (
            "Да, это заказ Telegram Premium на {months_text}: он дарится через официальную платформу Fragment по "
            "username, без входа в аккаунт."
        ),
        "faq_gift_premium": (
            "Premium приходит подарком от Telegram на аккаунт по username и активируется сам — ничего нажимать "
            "не нужно."
        ),
        "faq_where_premium": "Проверить Premium: Telegram → Настройки → Telegram Premium на аккаунте получателя.",
        "faq_time_premium": "Обычно Premium оформляется за 1–5 минут после оплаты и получения username. {status_hint}",
        "help_premium": (
            "Команды: «!статус» — состояние заказа, «!продавец» — позвать продавца. Чтобы получить Premium, "
            "просто пришлите username получателя в Telegram."
        ),
        "unknown_premium": (
            "Не совсем понял 🙂 Для выдачи Premium по заказу #{order_id} нужен username получателя в Telegram, "
            "например @username. Если нужен продавец — напишите «!продавец»."
        ),
        "confirm_reminder_premium": (
            "{buyer}, подскажите, пожалуйста: активировался ли Premium по заказу #{order_id}? Если да — подтвердите, "
            "пожалуйста, выполнение заказа на его странице. Если есть проблема — напишите, разберёмся здесь в чате."
        ),
        "review_reply_premium": "Спасибо за отзыв, {buyer}! Рады, что Premium пришёл быстро. Будем рады видеть вас снова 🙌",
        "presale_premium": (
            "Здравствуйте! 👋 Telegram Premium ({premium_terms}) выдаётся автоматически подарком через Fragment, "
            "без входа в аккаунт: оплатите лот и пришлите username получателя — обычно 1–5 минут."
        ),
        # ---- бот TG Gift: продажа подарков Telegram (раздел FunPay «Подарки»). {gift} — подарок, {qty} — сколько штук
        "greeting_need_username_shop": (
            "Здравствуйте, {buyer}! {returning}Спасибо за оплату заказа #{order_id} 🎉\n"
            "🎁 Товар: {gift} × {qty} — подарок Telegram\n"
            "🤖 Выдача автоматическая: бот отправит подарок на ваш аккаунт, обычно за 1–5 минут.\n\n"
            "👉 Пришлите в этот чат username получателя в Telegram, например: @username\n{last_hint}"
            "Найти его: Telegram → Настройки → «Имя пользователя». Можно указать аккаунт друга.\n\n"
            "ℹ️ Username нужен только для отправки подарка: мы не пишем вам в Telegram, всё общение — здесь, "
            "в чате FunPay. Пожалуйста, подтверждайте заказ только после того, как подарок придёт."
        ),
        "greeting_with_username_shop": (
            "Здравствуйте, {buyer}! {returning}Спасибо за оплату заказа #{order_id} 🎉\n"
            "🎁 {gift} × {qty} → @{username}{name_part}\n"
            "🤖 Отправляю автоматически, обычно 1–5 минут. Если username с ошибкой — сразу напишите правильный.\n"
            "Пожалуйста, подтверждайте заказ только после того, как подарок придёт."
        ),
        "username_ok_shop": "✅ Аккаунт найден: @{username}{name_part}. Отправляю {gift} × {qty}…",
        "delivered_shop": (
            "🎉 Готово! {gift} × {qty} отправлен на @{username} (заказ #{order_id}).\n"
            "Подарок виден в профиле получателя → «Подарки». Его можно оставить в профиле или конвертировать в звёзды.\n\n"
            "Огромное спасибо за покупку, {buyer} ❤️\n"
            "Пожалуйста, подтвердите выполнение заказа кнопкой «Подтвердить выполнение заказа» на странице "
            "заказа — и, если не сложно, оставьте отзыв. Это очень помогает нам!"
        ),
        "after_delivery_shop": (
            "Подарок по заказу #{order_id} уже отправлен на @{username} ✅ Он в профиле → «Подарки». "
            "Если что-то не так — напишите, продавец ответит здесь."
        ),
        "reminder_shop": (
            "👋 {buyer}, напоминаю: чтобы получить {gift} по заказу #{order_id}, пришлите username получателя в "
            "Telegram (например, @username)."
        ),
        "unknown_shop": (
            "Не совсем понял сообщение 🙂 Чтобы отправить подарок по заказу #{order_id}, нужен username получателя "
            "в Telegram (например, @username). Если есть вопрос — напишите «!продавец», и продавец ответит."
        ),
        "help_shop": (
            "Команды: «!статус» — состояние заказа, «!продавец» — позвать продавца. Чтобы получить подарок, просто "
            "пришлите username получателя в Telegram (например, @username)."
        ),
        "delay_shop": (
            "⚠️ {buyer}, отправка подарка по заказу #{order_id} ненадолго задерживается по техническим причинам. "
            "Продавец уже уведомлён и отправит подарок в ближайшее время — или оформит возврат, если вы предпочтёте."
        ),
        "awaiting_approval_shop": (
            "🔎 Заказ #{order_id} проходит стандартную проверку — обычно это несколько минут. Подарок будет отправлен "
            "сразу после неё."
        ),
        "change_username_late_shop": (
            "Подарок по заказу #{order_id} уже отправлен на @{username} — изменить получателя после отправки нельзя. "
            "Если это ошибка, напишите «!продавец»."
        ),
        "faq_gift_shop": (
            "Это обычный подарок Telegram: он появится в профиле получателя → «Подарки». Его можно оставить в профиле, "
            "скрыть или конвертировать в звёзды. Вход в аккаунт не нужен — только username."
        ),
        "faq_where_shop": "Подарок виден в Telegram: профиль получателя → вкладка «Подарки».",
        "faq_safe_shop": (
            "Это безопасно: подарок отправляется штатно, через Telegram, на ваш username. Никакие коды, пароли и вход "
            "в аккаунт не нужны. Никому их не сообщайте!"
        ),
        "faq_time_shop": "Обычно подарок приходит за 1–5 минут после оплаты и получения username. {status_hint}",
        "faq_premium_shop": "Здесь продаются подарки Telegram. Telegram Premium в этом лоте не продаётся.",
        "faq_not_arrived_shop": (
            "Понимаю, проверяю 🙏 {status_hint}\n"
            "Подарок виден в профиле @{username} → «Подарки». Я уже сообщил продавцу — если подарка не будет, "
            "он разберётся в этом чате."
        ),
        "faq_not_arrived_done_shop": (
            "Подарок по заказу #{order_id} уже отправлен ✅ {delivered_at} — {gift} × {qty} на @{username}.\n"
            "Проверьте профиль именно этого аккаунта → «Подарки» (если подарок скрыт — в настройках профиля). "
            "Если его всё равно нет — напишите здесь, продавец уже видит ваше сообщение и проверит отправку."
        ),
        "faq_refund_shop": (
            "Передал продавцу вашу просьбу по заказу #{order_id}, он ответит в этом чате. Если подарок ещё не "
            "отправлен, возврат оформляется через FunPay. Пожалуйста, не подтверждайте заказ, пока вопрос не решён."
        ),
        "presale_shop": (
            "Здравствуйте! 👋 Подарки Telegram выдаются автоматически: выберите лот с нужным подарком "
            "({denominations}), оплатите и пришлите @username получателя — подарок придёт за 1–5 минут. "
            "Если остались вопросы — напишите, продавец ответит здесь."
        ),
        "presale_hello_shop": (
            "Здравствуйте! 👋 Я на связи, отвечаю сразу, 24/7. Подарки Telegram ({denominations}) выдаются "
            "автоматически: оплатите лот и пришлите @username получателя — подарок придёт за 1–5 минут."
        ),
        "review_reply_shop": "Спасибо за отзыв, {buyer}! Рады, что подарок дошёл быстро. Будем рады видеть вас снова 🎁",
    },
    "en": {
        "greeting_need_username": (
            "Hi {buyer}! {returning}Thank you for paying for order #{order_id} 🎉\n"
            "⭐ Item: {stars} Telegram Stars\n"
            "🤖 Delivery is automatic via Telegram's official platform Fragment, usually 1–5 minutes.\n\n"
            "👉 Please send your Telegram username to this chat, e.g. @username\n{last_hint}"
            "Find it in Telegram → Settings → Username. The stars will be credited to that account.\n\n"
            "ℹ️ We only need the username to credit the stars; we won't message you in Telegram. Please confirm "
            "the order only after the stars arrive."
        ),
        "greeting_with_username": (
            "Hi {buyer}! {returning}Thank you for paying for order #{order_id} 🎉\n"
            "⭐ {stars} Telegram Stars → @{username}{name_part}\n"
            "🤖 Sending automatically via Fragment, usually 1–5 minutes. ❗ Stars go out right away to the username "
            "from the order; once sent, the recipient can't be changed and the stars can't be returned. Please "
            "confirm the order only after the stars arrive."
        ),
        "greeting_confirm_username": (
            "Hello, {buyer}! {returning}Thank you for order #{order_id} 🎉\n"
            "{what} → @{username}{name_part}\n"
            "❗ Please check the recipient: after sending it can't be changed or refunded.\n"
            "👉 If it's correct, reply \"Yes\". If not, send the right @username.{timeout_line}"
        ),
        "confirm_username": (
            "✅ Account found: @{username}{name_part}.\n"
            "❗ {what} will go to this account — the recipient can't be changed after sending.\n"
            "👉 If it's correct, reply \"Yes\". If not, send the right @username.{timeout_line}"
        ),
        "username_confirm_thanks": "👍 Thanks, recipient confirmed: @{username}. Sending {what}…",
        "username_confirm_no": "OK! Please send the correct Telegram username (e.g. @username).",
        "username_confirm_hint": "Please confirm the recipient @{username}: reply \"Yes\" or send another @username.",
        "username_ok": "✅ Account found: @{username}{name_part}. Sending {stars} ⭐…",
        "bad_username": ("❗ {buyer}, {reason}.\nPlease send your Telegram username again (Latin letters, e.g. "
                         "@username). Attempt {attempt} of {max_attempts}."),
        "bad_username_final": ("⛔ {buyer}, we couldn't find the account ({reason}). The seller has been notified "
                               "and will reply here shortly. Please don't open a dispute — we'll sort it out."),
        "delivered": ("🎉 Done! {stars} ⭐ Telegram Stars have been sent to @{username} (order #{order_id}).\n"
                      "They arrive within a couple of minutes: Telegram → Settings → My Stars.\n\n"
                      "Thank you so much, {buyer} ❤️ Please confirm the order on FunPay and leave a review if you "
                      "can. See you again ⭐"),
        "slow_network": ("⏳ The payment for #{order_id} is sent; the TON network is confirming it a bit slower than "
                         "usual. The stars will arrive shortly."),
        "awaiting_approval": "🔎 Order #{order_id} is passing a standard security check. The stars will follow.",
        "delay": ("⚠️ {buyer}, delivery of #{order_id} is briefly delayed for technical reasons. The seller has been "
                  "notified and will deliver shortly or refund if you prefer."),
        "reminder": "👋 {buyer}, to receive {stars} ⭐ for order #{order_id}, please send your Telegram username.",
        "status": "ℹ️ Order #{order_id}: {status_text}",
        "help": "Commands: \"!status\", \"!seller\". To receive the stars just send your Telegram username.",
        "call_seller": "🙋 The seller has been notified and will reply here as soon as possible.",
        "off_platform": ("🙏 Under FunPay rules we can't communicate or trade outside the platform or exchange "
                         "contacts. Let's sort everything out here."),
        "unknown": ("I didn't quite get that 🙂 To deliver order #{order_id} I need your Telegram username "
                    "(e.g. @username). Type \"!seller\" if you have a question."),
        "after_delivery": ("The stars for #{order_id} have already been sent to @{username} ✅ If they don't show up "
                           "within 10 minutes, type \"!seller\"."),
        "change_username_late": ("The stars for #{order_id} have already been paid to @{username}; the recipient "
                                 "can't be changed now. Type \"!seller\" if this is a mistake."),
        "refunded": "💸 Order #{order_id} has been refunded.",
        "completed_thanks": "🙏 Thank you for confirming order #{order_id}!",
        "presale_hello": ("Hi! 👋 I'm here and reply right away, 24/7. If you need Telegram Stars — delivery is "
                          "automatic: pick the lot ({denominations} ⭐), pay and send the recipient's @username — the "
                          "stars arrive in 1–5 minutes via Fragment. Any questions — write here."),
        "presale": ("Hi! 👋 Telegram Stars are delivered automatically: pick the lot ({denominations} ⭐), pay and "
                    "send your username — the stars arrive in 1–5 minutes via Fragment. Any questions — write here."),
        "faq_gift": "Stars are credited directly to the account by username (not as a gift).",
        "faq_safe": ("It's safe: the stars are bought on Telegram's official platform Fragment. We only need your "
                     "username — never share codes or passwords with anyone."),
        "faq_time": "Usually the stars arrive within 1–5 minutes after payment and username. {status_hint}",
        "faq_not_arrived": ("Checking it 🙏 {status_hint}\nStars are shown in Telegram → Settings → My Stars on "
                            "@{username}; the balance can update with up to 10 minutes delay. I've notified the "
                            "seller — if the stars don't show up, they will sort it out in this chat."),
        "faq_refund": ("I've passed your request for #{order_id} to the seller, they'll reply in this chat. Please "
                       "don't confirm the order until it's resolved."),
        "faq_not_arrived_done": (
            "The stars for order #{order_id} have already been sent ✅ {delivered_at} — {stars} ⭐ to "
            "@{username}{ref_part}.\nCheck Telegram → Settings → My Stars → history on @{username} (not another "
            "account!). The balance can take up to 10 minutes to update. If they're still missing, write here — the "
            "seller already sees your message and will check the payment."
        ),
        "faq_premium": ("This listing sells Telegram Stars. Telegram Premium is sold as separate offers in the "
                        "FunPay section Telegram → Premium, also delivered automatically by username."),
        "faq_where": "Stars are shown in Telegram → Settings → My Stars on the recipient account.",
        "greeting_need_username_gift": ("Hi {buyer}! {returning}Thanks for paying for order #{order_id} 🎉\n"
                                        "🎁 {stars} ⭐ as Telegram gifts. Delivery is automatic, 1–5 minutes.\n"
                                        "👉 Please send your Telegram username here, e.g. @username\n{last_hint}"
                                        "Please confirm the order only after the gifts arrive."),
        "greeting_with_username_gift": ("Hi {buyer}! {returning}Thanks for paying for order #{order_id} 🎉\n"
                                        "🎁 {stars} ⭐ as gifts → @{username}{name_part}. Sending automatically.\n"
                                        "Please confirm the order only after the gifts arrive."),
        "username_ok_gift": "✅ Account found: @{username}{name_part}. Sending gifts ({gifts})…",
        "delivered_gift": ("🎉 Done! Gifts sent to @{username} (order #{order_id}): {gifts}.\n"
                           "To get stars: profile → Gifts → tap a gift → Convert to Stars — {value} ⭐ in total.\n"
                           "Please confirm the order on its page and leave a review if you can. Thank you ❤️"),
        "faq_gift_gift": "Stars arrive as Telegram gifts; each gift can be converted to stars in your profile → Gifts.",
        "after_delivery_gift": "The gifts for #{order_id} were sent to @{username} ✅ See profile → Gifts.",
        "confirm_reminder": ("{buyer}, did everything arrive for order #{order_id}? If yes, please confirm the order "
                             "on its page. If there's a problem, write here and we'll sort it out."),
        "review_reply": "Thank you for the review, {buyer}! Glad your {stars} ⭐ arrived quickly. See you again 🙌",
        "greeting_need_username_premium": ("Hi {buyer}! {returning}Thanks for paying for order #{order_id} 🎉\n"
                                           "💎 Telegram Premium for {months_text}, gifted automatically via Fragment, "
                                           "no account login needed.\n"
                                           "👉 Please send the recipient's Telegram username here, e.g. @username\n"
                                           "{last_hint}Please confirm the order only after Premium is active."),
        "greeting_with_username_premium": ("Hi {buyer}! {returning}Thanks for paying for order #{order_id} 🎉\n"
                                           "💎 Telegram Premium for {months_text} → @{username}{name_part}. Gifting "
                                           "automatically via Fragment.\nPlease confirm the order only after Premium "
                                           "is active."),
        "username_ok_premium": "✅ Account found: @{username}{name_part}. Gifting Telegram Premium for {months_text}…",
        "delivered_premium": ("🎉 Done! Telegram Premium for {months_text} has been gifted to @{username} (order "
                              "#{order_id}). It activates by itself within a couple of minutes: Settings → Telegram "
                              "Premium.\nPlease confirm the order on its page and leave a review if you can. Thank you ❤️"),
        "reminder_premium": "👋 {buyer}, to receive Telegram Premium for order #{order_id}, please send the username.",
        "slow_network_premium": ("⏳ The Premium payment for #{order_id} is sent; the TON network is confirming it a "
                                 "bit slower than usual. I'll write when it's done."),
        "awaiting_approval_premium": "🔎 Order #{order_id} is passing a standard security check. Premium will follow.",
        "delay_premium": ("⚠️ {buyer}, delivery of #{order_id} is briefly delayed for technical reasons. The seller "
                          "has been notified and will gift Premium shortly, or refund if you prefer."),
        "after_delivery_premium": "Premium for #{order_id} was gifted to @{username} ✅ Check Settings → Telegram Premium.",
        "change_username_late_premium": ("Premium for #{order_id} has already been paid to @{username}; the recipient "
                                         "can't be changed. If it's a mistake, write \"!seller\"."),
        "faq_not_arrived_done_premium": ("Premium for #{order_id} was gifted ✅ {delivered_at} — {months_text} to "
                                         "@{username}{ref_part}. Check Settings → Telegram Premium on @{username}. If "
                                         "it's still missing, write here and the seller will check the payment."),
        "faq_premium_premium": "Yes, this is a Telegram Premium order for {months_text}, gifted via Fragment by username.",
        "faq_gift_premium": "Premium arrives as a gift from Telegram and activates by itself.",
        "faq_where_premium": "Check Premium in Telegram → Settings → Telegram Premium on the recipient account.",
        "faq_time_premium": "Premium is usually gifted within 1–5 minutes after payment and username. {status_hint}",
        "help_premium": "Commands: \"!status\", \"!seller\". To receive Premium just send the recipient's username.",
        "unknown_premium": ("I didn't quite get that 🙂 To deliver Premium for #{order_id} I need the recipient's "
                            "Telegram username, e.g. @username."),
        "confirm_reminder_premium": ("{buyer}, is Premium active for order #{order_id}? If yes, please confirm the "
                                     "order on its page. If there's a problem, write here."),
        "review_reply_premium": "Thank you for the review, {buyer}! Glad Premium arrived quickly. See you again 🙌",
        "presale_premium": ("Hi! 👋 Telegram Premium ({premium_terms}) is gifted automatically via Fragment, no login "
                            "needed: pay for the offer and send the recipient's username — usually 1–5 minutes."),
        "greeting_need_username_shop": ("Hi {buyer}! {returning}Thanks for paying for order #{order_id} 🎉\n"
                                        "🎁 {gift} × {qty} — a Telegram gift. Delivery is automatic, 1–5 minutes.\n"
                                        "👉 Send the recipient's Telegram username here, e.g. @username\n{last_hint}"
                                        "Please confirm the order only after the gift arrives."),
        "greeting_with_username_shop": ("Hi {buyer}! {returning}Thanks for paying for order #{order_id} 🎉\n"
                                        "🎁 {gift} × {qty} → @{username}{name_part}. Sending automatically.\n"
                                        "Please confirm the order only after the gift arrives."),
        "username_ok_shop": "✅ Account found: @{username}{name_part}. Sending {gift} × {qty}…",
        "delivered_shop": ("🎉 Done! {gift} × {qty} was sent to @{username} (order #{order_id}).\n"
                           "It is in the recipient's profile → Gifts.\nThank you, {buyer}! Please confirm the order "
                           "on its page and leave a review if you can."),
        "after_delivery_shop": "The gift for #{order_id} was sent to @{username} ✅ See profile → Gifts.",
        "reminder_shop": "👋 {buyer}, to get {gift} for order #{order_id}, please send the recipient's Telegram username.",
        "unknown_shop": ("To send the gift for order #{order_id} I need the recipient's Telegram username (e.g. "
                         "@username). Questions — type «!seller»."),
        "faq_gift_shop": "It's a regular Telegram gift: it appears in the recipient's profile → Gifts.",
        "faq_not_arrived_done_shop": ("The gift for #{order_id} was sent ✅ {delivered_at} — {gift} × {qty} to "
                                      "@{username}. Check that account's profile → Gifts."),
        "presale_shop": ("Hi! 👋 Telegram gifts ({denominations}) are delivered automatically: pay for the offer and "
                         "send the recipient's @username — the gift arrives in 1–5 minutes."),
        "presale_hello_shop": ("Hi! 👋 I'm here 24/7. Telegram gifts ({denominations}) are delivered automatically "
                               "by username in 1–5 minutes."),
        "review_reply_shop": "Thank you for the review, {buyer}! Glad the gift arrived quickly 🎁",
    },
}

REASONS = {
    "ru": {
        "bad_format": "это не похоже на username Telegram",
        "not_found": "аккаунт с таким username не найден в Telegram",
        "cannot_receive": "на этот аккаунт нельзя отправить звёзды (например, это бот или канал)",
        "blacklisted": "этот аккаунт не может быть использован для получения заказа",
        "no_username": "в сообщении нет username",
        "unknown": "Fragment не принял username",
    },
    "en": {
        "bad_format": "that doesn't look like a Telegram username",
        "not_found": "no Telegram account with this username",
        "cannot_receive": "stars can't be sent to this account (a bot or channel?)",
        "blacklisted": "this account can't be used for this order",
        "no_username": "there is no username in your message",
        "unknown": "Fragment rejected the username",
    },
}

STATUS_TEXT = {
    "ru": {
        "new": "обрабатывается", "awaiting_username": "ждём ваш username в Telegram",
        "pending_approval": "проходит проверку безопасности", "ready": "в очереди на отправку, это 1–2 минуты",
        "purchasing": "звёзды покупаются на Fragment", "sent": "платёж отправлен, сеть подтверждает",
        "delivered": "звёзды отправлены ✅", "completed": "завершён ✅", "refunded": "оформлен возврат",
        "manual": "передан продавцу, он ответит здесь", "blocked": "передан продавцу",
    },
    "en": {
        "new": "processing", "awaiting_username": "waiting for your Telegram username",
        "pending_approval": "security check", "ready": "queued, 1–2 minutes", "purchasing": "buying on Fragment",
        "sent": "payment sent, confirming", "delivered": "stars sent ✅", "completed": "completed ✅",
        "refunded": "refunded", "manual": "handed to the seller", "blocked": "handed to the seller",
    },
}

# Пользовательские тексты из настроек приложения: {lang: {key: text}}
OVERRIDES: dict[str, dict[str, str]] = {}

# Переменные, доступные в шаблонах (для редактора сообщений)
TEMPLATE_VARS = {
    "what": "100 ⭐ Telegram Stars", "timeout_line": "\nБез ответа отправлю на @durov через 15 мин.",
    "order_id": "ABCD1234", "buyer": "Покупатель", "stars": 100, "username": "durov", "name_part": " (Pavel)",
    "reason": "аккаунт не найден", "attempt": 1, "max_attempts": 3, "status_text": "ждём username",
    "denominations": "50, 75, 100", "status_hint": "", "returning": "Рады видеть вас снова 🙌 ",
    "last_hint": "Прошлый заказ вы получали на @durov — если аккаунт тот же, просто пришлите его ещё раз.\n",
    "gifts": "2×🏆100⭐", "value": 170, "gift": "🧸 Мишка", "qty": 1, "delivered_at": "20.11 05:20", "ref_part": " (платёж Fragment Ref#AbC123)", "months": 3, "months_text": "3 месяца",
    "premium_terms": "3, 6, 12 мес.",
}


def set_overrides(data: dict | None) -> None:
    OVERRIDES.clear()
    for lang, items in (data or {}).items():
        if isinstance(items, dict):
            OVERRIDES[lang] = {k: str(v) for k, v in items.items() if k in TEMPLATES.get(lang, {}) and str(v).strip()}


def template(lang: str, key: str) -> str:
    lang = lang if lang in TEMPLATES else "ru"
    return OVERRIDES.get(lang, {}).get(key) or TEMPLATES[lang][key]


def validate_template(text: str) -> str | None:
    """None — шаблон корректен, иначе текст ошибки."""
    try:
        text.format(**TEMPLATE_VARS)
    except KeyError as e:
        return f"неизвестная переменная {{{e.args[0]}}}"
    except (ValueError, IndexError) as e:
        return f"ошибка в фигурных скобках: {e}"
    if contains_forbidden_link(text):
        return "в тексте есть ссылка на сторонний ресурс — по правилам FunPay (п. 1.12, 2.1.13) так нельзя"
    return None


def render(lang: str, key: str, **kw) -> str:
    lang = lang if lang in TEMPLATES else "ru"
    try:
        return template(lang, key).format(**kw)
    except (KeyError, ValueError, IndexError):
        return TEMPLATES[lang][key].format(**kw)


def reason(lang: str, code: str, fallback: str = "") -> str:
    lang = lang if lang in REASONS else "ru"
    return REASONS[lang].get(code) or fallback or REASONS[lang]["unknown"]


def status_text(lang: str, status: str) -> str:
    lang = lang if lang in STATUS_TEXT else "ru"
    return STATUS_TEXT[lang].get(status, status)


# --- распознавание команд, вопросов и опасных тем
COMMANDS = {
    "status": ("!статус", "!status"),
    "help": ("!помощь", "!help", "!команды"),
    "seller": ("!продавец", "!seller", "!admin"),
}

# Слово «Telegram» здесь — название товара, поэтому само по себе не считается уводом с площадки.
OFF_PLATFORM_HINTS = ("discord", "дискорд", "whatsapp", "ватсап", "vk.com", "напрямую", "без фанпей", "без funpay",
                      "мимо фанпей", "в обход", "скинь контакт", "номер телефона", "добавь в друзья", "напиши мне в",
                      "пиши в лс", "пиши в тг", "напиши в тг", "свяжись со мной", "для связи", "дешевле напрямую")

FAQ = [
    ("faq_not_arrived", re.compile(r"не\s*(при|за)шл|не\s*получил|не\s*вижу|не\s*начисл|не\s*зачисл|где\s+(мои\s+)?зв[её]зд|"
                                  r"not\s+(received|arrived)|didn.?t\s+(get|receive|arrive)|haven.?t\s+(got|received)",
                                  re.I)),
    ("faq_refund", re.compile(r"возвр|верни(те)?\s+деньги|отмен(а|ить|ите)\s+заказ|^\s*отмен(и|ите|а|яй)\b|refund|cancel",
                              re.I)),
    ("faq_premium", re.compile(r"преми?ум|premium", re.I)),
    ("faq_where", re.compile(r"где\s+(посмотреть|увидеть|проверить)|как\s+(посмотреть|проверить)|where\s+(can|do)\s+i\s+see",
                             re.I)),
    ("faq_gift", re.compile(r"подар|gift", re.I)),
    ("faq_safe", re.compile(r"безопасн|не скам|не обман|scam|safe|законн|бан", re.I)),
    ("faq_time", re.compile(r"когда|скоро|долго|сколько ждать|how long|when", re.I)),
]

QUESTION_RE = re.compile(r"\?|^(как|когда|что|почему|зачем|сколько|где|можно|а если|how|when|what|why|can)\b", re.I)
STARS_TOPIC_RE = re.compile(r"зв[её]зд|stars?\b|⭐|старс", re.I)
# «есть кто?», «здравствуйте», «актуально?» — человек проверяет, живой ли продавец, прежде чем платить
HELLO_RE = re.compile(r"привет|здравств|здрасьт|добр(ый|ое|ого|ой)\s|доброй|ты\s+тут|вы\s+тут|на\s+месте|есть\s+кто|"
                      r"кто[-\s]?нибудь|продавец|актуальн|в\s+наличии|можно\s+(купить|оплат)|ответ(ь|ьте)|алло|ау\b|"
                      r"^\s*(hi|hello|hey)\b|are\s+you\s+there|is\s+anyone", re.I)
# не наш товар — на такие сообщения бот звёзд не отвечает (на аккаунте могут продаваться и другие товары)
OTHER_GOODS_RE = re.compile(r"steam|стим|скин|skin|кс\b|cs2|csgo|dota|дота|ключ|аккаунт\s+(steam|стим)|робукс|"
                            r"robux|premium|премиум|прем\b|nft|юзернейм\s+прода", re.I)
_LINK_RE = re.compile(r"(https?://|www\.)\S+", re.I)


def detect_command(text: str) -> str | None:
    t = (text or "").strip().lower()
    for cmd, aliases in COMMANDS.items():
        if any(t.startswith(a) for a in aliases):
            return cmd
    return None


def detect_faq(text: str) -> str | None:
    for key, rx in FAQ:
        if rx.search(text or ""):
            return key
    return None


NO_RE = re.compile(r"^\s*(нет|не\s+тот|не\s+та|не\s+мой|не\s*верно|неправильно|ошиб|no\b|nope|wrong|not\s+mine)", re.I)
YES_RE = re.compile(r"^\s*(да+\b|ага|угу|верно|вс[её]\s+(верно|так|правильно)|подтвержда|правильно|так\b|ок\b|окей|"
                    r"ok\b|okay|yes|yep|yeah|correct|confirm|\+|👍|✅)", re.I)


def is_yes(text: str) -> bool:
    return bool(YES_RE.search(text or "")) and not is_no(text)


def is_no(text: str) -> bool:
    return bool(NO_RE.search(text or ""))


def is_question(text: str) -> bool:
    return bool(QUESTION_RE.search((text or "").strip()))


def is_off_platform_request(text: str) -> bool:
    t = f" {(text or '').lower()} "
    return any(h in t for h in OFF_PLATFORM_HINTS)


def contains_forbidden_link(text: str) -> bool:
    for m in _LINK_RE.finditer(text or ""):
        if "funpay.com" not in m.group(0).lower():
            return True
    return False


def detect_lang(text: str, default: str = "ru") -> str:
    if not text:
        return default
    cyr = sum(1 for ch in text if "а" <= ch.lower() <= "я" or ch in "ёЁ")
    lat = sum(1 for ch in text if "a" <= ch.lower() <= "z")
    if cyr == 0 and lat > 12 and "@" not in text:
        return "en"
    return default
