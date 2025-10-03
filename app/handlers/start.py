# app/handlers/start.py
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import CommandStart, Command

from ..db import get_or_create_user, get_user_by_tgid
from ..settings import SET
from ..keyboards import kb_register, kb_user_main

__all__ = ["router"]

router = Router()


@router.message(CommandStart())
async def cmd_start(m: Message) -> None:
    # На всякий случай: в редких типах апдейтов from_user может быть None
    if not m.from_user:
        await m.answer("Не удалось определить пользователя. Попробуйте ещё раз.")
        return

    u = get_or_create_user(
        m.from_user.id,
        m.from_user.username,
        m.from_user.first_name,
        m.from_user.last_name,
    )
    is_admin = m.from_user.id in SET.admin_ids

    # Обычным пользователям — регистрация, админам — сразу главное меню
    if getattr(u, "status", None) == "pending" and not is_admin:
        await m.answer(
            "Привет! 👋\n\n"
            "Вы ещё не одобрены. Нажмите «Регистрация», чтобы отправить заявку администратору.",
            reply_markup=kb_register(),
        )
        return

    await m.answer("Главное меню:", reply_markup=kb_user_main(is_admin=is_admin))


@router.message(Command("admin"))
async def cmd_admin(m: Message) -> None:
    if not m.from_user or m.from_user.id not in SET.admin_ids:
        # Тихо игнорируем для не-админов
        return

    # Используем HTML + <code> для безопасного отображения угловых скобок
    await m.answer(
        "<b>Админ-панель</b>\n"
        "• <code>/pending</code> — заявки\n"
        "• <code>/grant_trial &lt;tg_id&gt;</code>\n"
        "• <code>/grant_paid &lt;tg_id&gt;</code>\n"
        "• <code>/grant_unlim &lt;tg_id&gt;</code>"
    )


@router.callback_query(F.data == "reg:start")
async def reg_start(c: CallbackQuery) -> None:
    # Быстрый ACK, чтобы не ловить timeout в callback (до 3 секунд)
    try:
        await c.answer()
    except Exception:
        # Даже если ответить не смогли, продолжаем обработку
        pass

    if not c.from_user:
        await c.message.answer("Ошибка: не удалось определить пользователя. Повторите /start")
        return

    u = get_user_by_tgid(c.from_user.id)
    if not u:
        await c.message.answer("Ошибка: пользователь не найден. Повторите /start")
        return

    if getattr(u, "status", None) != "pending":
        await c.message.answer("Заявка уже обработана. Нажмите /start")
        return

    username = f"@{c.from_user.username}" if c.from_user.username else "(нет username)"
    text = (
        "🆕 Заявка на доступ\n"
        f"tg_id={c.from_user.id}\n"
        f"username={username}"
    )

    for admin_id in SET.admin_ids:
        try:
            await c.bot.send_message(admin_id, text)
            await c.bot.send_message(admin_id, "Откройте Админ-панель → Заявки")
        except Exception:
            # Не валим обработку, если одному из админов не доставилось
            pass

    await c.message.answer("Заявка отправлена. Ожидайте решения администратора.")
