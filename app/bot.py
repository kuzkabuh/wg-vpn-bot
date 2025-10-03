from __future__ import annotations

from aiogram import Dispatcher

# Подключаем ваши роутеры
from .handlers.start import router as start_router
from .handlers.user import router as user_router
from .handlers.admin import router as admin_router

# Никаких импортов FastAPI и никакого импорта из .webhook!
dp = Dispatcher()

# Порядок не критичен, но оставим понятным
dp.include_router(start_router)
dp.include_router(user_router)
dp.include_router(admin_router)
