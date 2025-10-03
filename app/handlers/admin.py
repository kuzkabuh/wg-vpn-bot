# app/handlers/admin.py
from datetime import datetime, timezone

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message

from ..settings import SET
from ..db import list_pending, get_user_by_tgid, update_user
from ..utils import plan_apply, human_dt

__all__ = ["router"]

router = Router()


def _is_admin(user_id: int) -> bool:
    return user_id in SET.admin_ids


@router.callback_query(F.data == "admin:menu")
async def admin_menu(c: CallbackQuery) -> None:
    # быстрый ACK, чтобы не висел "часик"
    try:
        await c.answer()
    except Exception:
        pass

    if not c.from_user or not _is_admin(c.from_user.id):
        await c.message.answer("Доступ запрещён.")
        return

    await c.message.answer(
        "Админ-панель:\n"
        "• /pending — заявки\n"
        "• /grant_trial &lt;tg_id&gt;\n"
        "• /grant_paid &lt;tg_id&gt;\n"
        "• /grant_unlim &lt;tg_id&gt;"
    )


@router.message(F.text.startswith("/pending"))
async def admin_pending(m: Message) -> None:
    if not m.from_user or not _is_admin(m.from_user.id):
        return

    pend = list_pending()
    if not pend:
        await m.answer("Нет заявок.")
        return

    lines = ["Заявки:"]
    for u in pend:
        uname = f"@{u.username}" if getattr(u, "username", None) else "(нет username)"
        lines.append(f"• tg_id={getattr(u, 'tg_id', '?')} {uname}")
    await m.answer("\n".join(lines))


def _parse_tg_id(arg: str | None) -> int | None:
    if not arg:
        return None
    try:
        return int(arg.strip())
    except Exception:
        return None


async def _apply_and_notify(m: Message, tg_id: int, plan: str, ok_text: str) -> None:
    u = get_user_by_tgid(tg_id)
    if not u:
        await m.answer("Пользователь не найден")
        return

    # используем UTC-наивную совместимость через aware-дату
    now = datetime.now(timezone.utc)
    expires_at, limit = plan_apply(plan, now)

    # Обновляем юзера
    update_user(u, status="approved", plan=plan, devices_limit=limit, expires_at=expires_at)

    # Ответ админу
    exp_human = human_dt(expires_at) if expires_at else "∞"
    await m.answer(f"{ok_text} До: {exp_human} (unix={expires_at or '∞'}).")

    # Пытаемся оповестить пользователя в ЛС
    try:
        await m.bot.send_message(
            tg_id,
            (
                "✅ Доступ активирован.\n"
                f"Тариф: {plan}\n"
                f"Лимит устройств: {'безлимит' if (limit is not None and limit < 0) else limit}\n"
                f"Действует до: {exp_human}"
            ),
        )
    except Exception:
        # молча игнорируем, если нельзя написать
        pass


@router.message(F.text.startswith("/grant_trial"))
async def grant_trial(m: Message) -> None:
    if not m.from_user or not _is_admin(m.from_user.id):
        return

    parts = (m.text or "").split(maxsplit=1)
    tg_id = _parse_tg_id(parts[1] if len(parts) > 1 else None)
    if tg_id is None:
        await m.answer("Формат: /grant_trial &lt;tg_id&gt;")
        return

    await _apply_and_notify(m, tg_id, plan="trial", ok_text="OK. Выдан trial.")


@router.message(F.text.startswith("/grant_paid"))
async def grant_paid(m: Message) -> None:
    if not m.from_user or not _is_admin(m.from_user.id):
        return

    parts = (m.text or "").split(maxsplit=1)
    tg_id = _parse_tg_id(parts[1] if len(parts) > 1 else None)
    if tg_id is None:
        await m.answer("Формат: /grant_paid &lt;tg_id&gt;")
        return

    await _apply_and_notify(m, tg_id, plan="paid", ok_text="OK. Выдан paid.")


@router.message(F.text.startswith("/grant_unlim"))
async def grant_unlim(m: Message) -> None:
    if not m.from_user or not _is_admin(m.from_user.id):
        return

    parts = (m.text or "").split(maxsplit=1)
    tg_id = _parse_tg_id(parts[1] if len(parts) > 1 else None)
    if tg_id is None:
        await m.answer("Формат: /grant_unlim &lt;tg_id&gt;")
        return

    await _apply_and_notify(m, tg_id, plan="unlimited", ok_text="OK. Выдан unlimited.")
