from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton as K

def kb_register() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Регистрация", callback_data="reg:start")
    return b.as_markup()

def kb_user_main(is_admin: bool = False) -> InlineKeyboardMarkup:
    kb = [
        [K(text="📊 Статистика", callback_data="user:stats")],
        [K(text="🔌 Мои подключения", callback_data="user:peers")],
        [K(text="➕ Новое подключение", callback_data="user:newpeer")],
        [K(text="🗑 Удалить последнее", callback_data="user:delpeer")],
    ]
    if is_admin:
        kb.append([K(text="🛠 Админ: статистика", callback_data="admin:stats")])
        kb.append([K(text="📋 Конфигурации", callback_data="admin:cfgs")])
        kb.append([K(text="👥 Все пиры", callback_data="admin:peers")])
    return InlineKeyboardMarkup(inline_keyboard=kb)
