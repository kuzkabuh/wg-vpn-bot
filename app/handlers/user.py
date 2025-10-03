from datetime import datetime, timezone
from typing import Tuple

from aiogram import Router, F
from aiogram.types import CallbackQuery, BufferedInputFile

from ..db import (
    get_user_by_tgid,
    count_user_peers,
    add_peer_row,
    get_user_peers,
    update_user,          # пока не используется, оставим
    revoke_peer_row,
)
from ..settings import SET
from ..keyboards import kb_user_main
from ..utils import check_limit, human_dt, make_qr_png
from ..wgd_api import wgd, WGDError

__all__ = ["router"]

router = Router()


def _main_menu_for(c: CallbackQuery):
    is_admin = bool(c.from_user and c.from_user.id in SET.admin_ids)
    return kb_user_main(is_admin=is_admin)


def _user_config_params(tg_id: int) -> Tuple[str, str, int]:
    """
    Имя, адрес и порт для персональной конфигурации.
    - Имя:   wg<tg_id>
    - Адрес: 10.88.<20..219>.1/24 (зависит от tg_id)
    - Порт:  20000..49999 (детерминированно по tg_id)
    """
    cfg_name = f"wg{tg_id}"
    third_octet = 20 + (tg_id % 200)  # 20..219
    address = f"10.88.{third_octet}.1/24"
    listen_port = 20000 + (tg_id % 30000)  # 20000..49999
    return cfg_name, address, listen_port


@router.callback_query(F.data == "user:plan")
async def user_plan(c: CallbackQuery) -> None:
    try:
        await c.answer()
    except Exception:
        pass

    if not c.from_user:
        await c.message.answer("Не удалось определить пользователя. Повторите /start")
        return

    u = get_user_by_tgid(c.from_user.id)
    if not u or u.status != "approved":
        await c.message.answer("Недоступно. Дождитесь одобрения администратора.", reply_markup=_main_menu_for(c))
        return

    exp = human_dt(u.expires_at) if u.expires_at else "∞"
    limit = "безлимит" if (u.devices_limit is not None and u.devices_limit < 0) else str(u.devices_limit or 0)

    await c.message.answer(
        f"Ваш план: {u.plan}\nЛимит устройств: {limit}\nДействует до: {exp}",
        reply_markup=_main_menu_for(c),
    )


@router.callback_query(F.data == "user:peers")
async def user_peers(c: CallbackQuery) -> None:
    try:
        await c.answer()
    except Exception:
        pass

    if not c.from_user:
        await c.message.answer("Не удалось определить пользователя. Повторите /start")
        return

    u = get_user_by_tgid(c.from_user.id)
    if not u or u.status != "approved":
        await c.message.answer("Недоступно. Дождитесь одобрения администратора.", reply_markup=_main_menu_for(c))
        return

    peers = get_user_peers(u.id)
    if not peers:
        await c.message.answer("У вас нет активных подключений.", reply_markup=_main_menu_for(c))
        return

    lines = ["Ваши подключения:"]
    for p in peers:
        # если в модели есть поле interface — это имя WG-конфигурации
        iface = getattr(p, "interface", None)
        suffix = f" (cfg={iface})" if iface else ""
        lines.append(f"• {p.name} (id={p.wgd_peer_id}){suffix}")
    await c.message.answer("\n".join(lines), reply_markup=_main_menu_for(c))


@router.callback_query(F.data == "user:newpeer")
async def user_newpeer(c: CallbackQuery) -> None:
    try:
        await c.answer()
    except Exception:
        pass

    if not c.from_user:
        await c.message.answer("Не удалось определить пользователя. Повторите /start")
        return

    u = get_user_by_tgid(c.from_user.id)
    if not u or u.status != "approved":
        await c.message.answer("Недоступно. Дождитесь одобрения администратора.", reply_markup=_main_menu_for(c))
        return

    now = int(datetime.now(tz=timezone.utc).timestamp())

    # Проверка срока действия тарифа (для не-unlimited)
    if u.plan != "unlimited" and (not u.expires_at or now > u.expires_at):
        await c.message.answer(
            "Срок действия вашего тарифа истёк. Обратитесь к администратору.",
            reply_markup=_main_menu_for(c),
        )
        return

    # Проверка лимита устройств
    cur = count_user_peers(u.id)
    if not check_limit(cur, u.devices_limit):
        await c.message.answer("Достигнут лимит устройств для вашего тарифа.", reply_markup=_main_menu_for(c))
        return

    # Персональная WG-конфигурация пользователя
    cfg_name, cfg_addr, cfg_port = _user_config_params(c.from_user.id)

    # Создание peer в WGDashboard
    name = f"{c.from_user.username or 'user'}-{c.from_user.id}-{now}"
    try:
        # гарантия существования конфигурации для пользователя
        await wgd.ensure_config(cfg_name, address=cfg_addr, listen_port=cfg_port, protocol="wg")

        # создаём пир внутри этой конфигурации
        peer_pubkey_or_id = await wgd.create_peer(cfg_name, name)

        # скачиваем конфиг именно из пользовательской конфигурации
        config_text = await wgd.get_peer_config(cfg_name, peer_pubkey_or_id)

    except WGDError as e:
        await c.message.answer(f"Ошибка создания подключения: {e}", reply_markup=_main_menu_for(c))
        return
    except Exception as e:
        await c.message.answer(f"Непредвиденная ошибка при создании подключения: {e}", reply_markup=_main_menu_for(c))
        return

    # Сохраняем в БД (вместо глобального интерфейса — имя конфигурации пользователя)
    add_peer_row(u.id, cfg_name, peer_pubkey_or_id, name)

    # Готовим файлы
    cfg_bytes = config_text.encode("utf-8")
    qr_bytes = make_qr_png(config_text)

    await c.message.answer("Подключение создано. Скачайте конфигурацию или отсканируйте QR-код.")
    await c.message.answer_document(BufferedInputFile(cfg_bytes, filename=f"{name}.conf"))
    await c.message.answer_photo(BufferedInputFile(qr_bytes, filename=f"{name}.png"), reply_markup=_main_menu_for(c))


@router.callback_query(F.data == "user:delpeer")
async def user_delpeer(c: CallbackQuery) -> None:
    try:
        await c.answer()
    except Exception:
        pass

    if not c.from_user:
        await c.message.answer("Не удалось определить пользователя. Повторите /start")
        return

    u = get_user_by_tgid(c.from_user.id)
    if not u or u.status != "approved":
        await c.message.answer("Недоступно.", reply_markup=_main_menu_for(c))
        return

    peers = get_user_peers(u.id)
    if not peers:
        await c.message.answer("У вас нет активных подключений.", reply_markup=_main_menu_for(c))
        return

    # Удаляем последний по времени создания (или по id, если нет created_at)
    try:
        target = sorted(peers, key=lambda x: getattr(x, "created_at", 0) or getattr(x, "id", 0))[-1]
    except Exception:
        target = peers[-1]

    # Попробуем удалить, зная конфигурацию; если в записи её нет — откатимся к старому способу
    cfg_for_target = getattr(target, "interface", None) or getattr(target, "wgd_interface", None)

    try:
        if cfg_for_target:
            await wgd.delete_peer(cfg_for_target, target.wgd_peer_id)
        else:
            await wgd.delete_peer(target.wgd_peer_id)
    except WGDError as e:
        # запасной дубль старым способом
        if cfg_for_target:
            try:
                await wgd.delete_peer(target.wgd_peer_id)
            except Exception:
                await c.message.answer(f"Ошибка удаления в WGDashboard: {e}", reply_markup=_main_menu_for(c))
                return
        else:
            await c.message.answer(f"Ошибка удаления в WGDashboard: {e}", reply_markup=_main_menu_for(c))
            return
    except Exception as e:
        await c.message.answer(f"Непредвиденная ошибка при удалении: {e}", reply_markup=_main_menu_for(c))
        return

    revoke_peer_row(target.id)
    await c.message.answer(f"Подключение {target.name} удалено.", reply_markup=_main_menu_for(c))
