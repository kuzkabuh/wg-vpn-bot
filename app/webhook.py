# app/webhook.py
from fastapi import FastAPI, Request, Header, HTTPException
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Update

from .settings import SET
from .handlers import start, user, admin, stats as h_stats

app = FastAPI(title="WG VPN Bot")

# aiogram 3.13+: parse_mode передаём через DefaultBotProperties
bot = Bot(
    token=SET.bot_token,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()

# Подключаем роутеры — иначе хендлеры не сработают.
dp.include_router(start.router)
dp.include_router(user.router)
dp.include_router(admin.router)
dp.include_router(h_stats.router)


@app.get("/health")
async def health():
    return {"ok": True}


# Telegram webhook: /tg/<WEBHOOK_SECRET>
@app.post(f"/tg/{SET.webhook_secret}")
async def tg_webhook(request: Request):
    payload = await request.json()
    update = Update.model_validate(payload)
    await dp.feed_update(bot, update)
    return {"ok": True}


# WGDashboard webhook с проверкой секрета в заголовке
@app.post("/wgd/webhook")
async def wgd_webhook(request: Request, x_wgd_secret: str | None = Header(default=None)):
    if SET.wgd_webhook_secret and (x_wgd_secret != SET.wgd_webhook_secret):
        raise HTTPException(status_code=403, detail="invalid secret")
    # если нужно что-то делать с событием — разбери JSON ниже
    _ = await request.json()
    return {"ok": True}
