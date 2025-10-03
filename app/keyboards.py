# app/keyboards.py
from __future__ import annotations

from typing import Iterable, List, Sequence, Tuple

from aiogram.types import InlineKeyboardButton as K
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


# ───────────────────────── low-level helpers ─────────────────────────

def _kb_from_rows(rows: Sequence[Sequence[Tuple[str, str]]]) -> InlineKeyboardMarkup:
    """
    Построить InlineKeyboardMarkup из списка строк,
    где каждая строка — последовательность пар (текст, callback_data).
    """
    b = InlineKeyboardBuilder()
    for row in rows:
        b.row(*(K(text=text, callback_data=data) for text, data in row))
    return b.as_markup()


# ───────────────────────── public factories ─────────────────────────

def kb_register() -> InlineKeyboardMarkup:
    """Кнопка регистрации для пользователей со статусом pending."""
    return _kb_from_rows([[("Регистрация", "reg:start")]])


def kb_user_main(is_admin: bool = False) -> InlineKeyboardMarkup:
    """
    Главное меню пользователя. Для админа добавляем блок административных кнопок.
    """
    rows: List[List[Tuple[str, str]]] = [
        [("📊 Статистика", "user:stats")],
        [("🔌 Мои подключения", "user:peers")],
        [("➕ Новое подключение", "user:newpeer")],
        [("🗑 Удалить последнее", "user:delpeer")],
    ]

    if is_admin:
        rows += [
            [("📈 Общая статистика", "admin:stats")],
            [("🧩 Конфигурации", "admin:cfgs")],
            [("👥 Все пиры", "admin:peers")],
        ]

    return _kb_from_rows(rows)


def kb_admin_main() -> InlineKeyboardMarkup:
    """Главное меню админа (если хочется вызывать напрямую)."""
    rows = [
        [("📈 Общая статистика", "admin:stats")],
        [("🧩 Конфигурации", "admin:cfgs")],
        [("👥 Все пиры", "admin:peers")],
        [("⬅️ Меню", "up:main")],
    ]
    return _kb_from_rows(rows)


def kb_back_to_menu(label: str = "⬅️ Меню") -> InlineKeyboardMarkup:
    """Одна кнопка «Назад в меню» (callback up:main). Удобно переиспользовать в карточках."""
    return _kb_from_rows([[(label, "up:main")]])
