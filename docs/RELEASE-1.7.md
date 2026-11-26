# Орбита 1.7 (StarsCenter 1.7.0)

Одна ветка вместо семи открытых PR. Слито поверх 1.6 (PR #41) без изменения логики, конфликт был один (`center/web.py`: маршруты rollout и orbit, оставлены оба).

| PR | Что | Ветка |
|---|---|---|
| #42 | StarsBot: доказательства по заказам, ответ арбитражу FunPay, подтверждение username | feature/order-evidence |
| #44 | ShopBot: P2P-шлюзы CrystalPAY / AAIO (выключены до KYC) | feature/p2p-gateways |
| #43 | ShopBot: VPN, подписки, App Store, пополнение Steam | shopbot-services |
| #45 | ShopBot: Яндекс Плюс — свои семьи и перепродажа | shopbot-yandex-plus |
| #46 | Центр: раздел «Экономика» | economy-report |
| #47 | Орбита: сервер 24/7 и обновление на ходу | server-rolling |
| #48 | Орбита: единый пульт позиций и круги | orbit-positions |

Правила не изменились: боты не стартуют сами при запуске Центра; новые каналы и шлюзы выключены по умолчанию.

Проверка: Центр 185 + MannBot 62 + StarsBot 173 + ShopBot 47 + LisBot 67 тестов.
