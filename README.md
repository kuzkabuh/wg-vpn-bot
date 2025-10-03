# WG VPN Telegram Bot (aiogram 3 + WGDashboard)

## Быстрый старт

1. Скопируйте проект в `/opt/wg-vpn-bot`, создайте `.env` из `.env.example`.
2. Создайте venv и установите зависимости: `pip install -r requirements.txt`.
3. Проверьте доступность WGDashboard API по `WGD_API_BASE` и токен.
4. Настройте Nginx `nginx/wg-vpn-bot.conf` (домен и пути к SSL).
5. Установите systemd‑сервис `systemctl enable --now wg-vpn-bot`.
6. Установите вебхук: 
   ```py
   from app.bot import set_webhook
   import asyncio
   asyncio.run(set_webhook())
   ```
7. В Telegram: `/start` → Регистрация → админ выдает `/grant_trial <tg_id>` и т. п.

## Примечания по WGDashboard
- Эндпоинты в `app/wgd_api.py` могут отличаться у вашей сборки. При ошибках 404/400 обновите пути в `EndpointMap`.
- Бот создаёт пир на интерфейсе `WGD_INTERFACE` и скачивает конфиг через API.

## Безопасность
- Храните токены в `.env` и ограничьте доступ к серверу.
- Вебхук закрыт секретным сегментом пути (`WEBHOOK_SECRET`).
- Есть простой rate‑limit на пользователя.

## Отладка (без вебхука)
- Можно запустить polling: `python -m app.bot` из venv. (Не одновременно с вебхуком.)
# wg-vpn-bot
