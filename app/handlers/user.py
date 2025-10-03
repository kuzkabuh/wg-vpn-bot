from __future__ import annotations

from datetime import datetime, timezone
import time
from typing import Tuple, Optional, List

from aiogram import Router, F
from aiogram.types import (
    CallbackQuery,
    Message,
    BufferedInputFile,
    ForceReply,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..db import (
    get_user_by_tgid,
    count_user_peers,
    add_peer_row,
    get_user_peers,
    update_user,          # оставим на будущее
    revoke_peer_row,
)

# мягкая зависимость — если нет функции, отключим переименование
try:
    from ..db import rename_peer_row  # def rename_peer_row(peer_row_id: int, new_name: str) -> None
except Exception:
    rename_peer_row = None  # type: ignore

from ..settings import SET
from ..keyboards import kb_user_main
from ..utils import check_limit, human_dt, make_qr_png
from ..wgd_api import wgd, WGDError

__all__ = ["router"]

router = Router()


# -------------------------- утилиты форматирования --------------------------

def _main_menu_for(c: CallbackQuery):
    is_admin = bool(c.from_user and c.from_user.id in SET.admin_ids)
    return kb_user_main(is_admin=is_admin)

def _fmt_bytes(n: int | float | None) -> str:
    if not n or n <= 0:
        return "0 B"
    n = float(n)
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while n >= 1024.0 and i < len(units) - 1:
        n /= 1024.0
        i += 1
    if n >= 100 or i == 0:
        return f"{int(n)} {units[i]}"
    return f"{n:.1f} {units[i]}"

def _fmt_hs(ts: Optional[int]) -> str:
    if not ts:
        return "—"
    try:
        diff = int(time.time() - int(ts))
    except Exception:
        return "—"
    if diff < 0:
        diff = 0
    if diff < 120:
        return "только что"
    mins = diff // 60
    if mins < 60:
        return f"{mins} мин назад"
    hours = mins // 60
    if hours < 24:
        return f"{hours} ч назад"
    days = hours // 24
    return f"{days} дн назад"

def _status_dot(active: bool) -> str:
    return "🟢" if active else "⚫️"


# -------------------------- персональная конфигурация --------------------------

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


# -------------------------- клавиатуры --------------------------

def _kb_peers_list(items: List[tuple[str, str, str]]):
    """
    items: (title, cfg, pid) -> кнопки для открытия карточки пира
    callback: up:s|<cfg>|<pid>
    """
    kb = InlineKeyboardBuilder()
    for title, cfg, pid in items:
        kb.button(text=f"🔹 {title}", callback_data=f"up:s|{cfg}|{pid}")
    kb.adjust(1)
    kb.button(text="➕ Новое подключение", callback_data="user:newpeer")
    kb.button(text="⬅️ Главное меню", callback_data="up:main")
    kb.adjust(1)
    return kb.as_markup()

def _kb_peer_actions(cfg: str, pid: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="📥 Конфиг", callback_data=f"up:d|{cfg}|{pid}")
    kb.button(text="🗑 Удалить", callback_data=f"up:x|{cfg}|{pid}")
    kb.adjust(2)
    kb.button(text="✏️ Имя", callback_data=f"up:r|{cfg}|{pid}")
    kb.button(text="🔄 Обновить", callback_data=f"up:s|{cfg}|{pid}")
    kb.adjust(2)
    kb.button(text="⬅️ К списку", callback_data="user:peers")
    kb.adjust(1)
    return kb.as_markup()


# -------------------------- команды и колбэки --------------------------

@router.callback_query(F.data == "up:main")
async def back_to_main(c: CallbackQuery):
    try:
        await c.answer()
    except Exception:
        pass
    await c.message.answer("Главное меню:", reply_markup=_main_menu_for(c))


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

    rows = get_user_peers(u.id)
    if not rows:
        await c.message.answer("У вас нет активных подключений.", reply_markup=_main_menu_for(c))
        return

    snap = await wgd.snapshot()

    items_for_kb: List[tuple[str, str, str]] = []
    lines: List[str] = []
    lines.append("🧩 *Ваши подключения*")
    lines.append("```")
    lines.append(f"{'Пир':28} {'RX':>8} {'TX':>8} {'HS':>12} {'CFG':>12} {'St':>3}")
    lines.append(f"{'-'*28} {'-'*8} {'-'*8} {'-'*12} {'-'*12} {'-'*3}")

    total_rx = 0
    total_tx = 0

    for r in rows:
        cfg = getattr(r, "interface", None) or getattr(r, "wgd_interface", None) or f"wg{c.from_user.id}"
        pid = str(r.wgd_peer_id)
        title = r.name or pid[:10]

        p = wgd.find_peer_in_snapshot(snap, cfg, pid) or {}
        rx = int(p.get("rx", 0) or 0)
        tx = int(p.get("tx", 0) or 0)
        hs = _fmt_hs(p.get("last_handshake"))
        st = _status_dot(bool(p.get("active", False)))

        total_rx += rx
        total_tx += tx

        lines.append(f"{title[:28]:28} {_fmt_bytes(rx):>8} {_fmt_bytes(tx):>8} {hs:>12} {cfg[-12:]:>12} {st:>3}")
        items_for_kb.append((title, cfg, pid))

    lines.append("```")
    lines.append(f"Всего пиров: {len(rows)} | Трафик: ⬇ {_fmt_bytes(total_rx)} ⬆ {_fmt_bytes(total_tx)}")

    await c.message.answer(
        "\n".join(lines),
        reply_markup=_kb_peers_list(items_for_kb),
        parse_mode="Markdown",
    )


@router.callback_query(F.data.startswith("up:s|"))
async def peer_show(c: CallbackQuery):
    """Карточка одного пира + действия."""
    try:
        await c.answer()
    except Exception:
        pass

    try:
        _, rest = c.data.split("up:s|", 1)
        cfg, pid = rest.split("|", 1)
    except Exception:
        await c.message.answer("Некорректные данные кнопки.")
        return

    u = get_user_by_tgid(c.from_user.id) if c.from_user else None
    row = None
    if u:
        for r in get_user_peers(u.id):
            if str(r.wgd_peer_id) == pid:
                row = r
                break

    snap = await wgd.snapshot()
    p = wgd.find_peer_in_snapshot(snap, cfg, pid) or {}
    title = (row.name if row else p.get("name")) or pid[:12]

    rx = _fmt_bytes(p.get("rx", 0))
    tx = _fmt_bytes(p.get("tx", 0))
    hs = _fmt_hs(p.get("last_handshake"))
    st = _status_dot(bool(p.get("active", False)))

    text = (
        f"🎛 *Подключение*\n"
        f"`{title}`\n\n"
        f"*CFG:* `{cfg}`\n"
        f"*ID:* `{pid}`\n"
        f"*Статус:* {st}\n"
        f"*Последний HS:* {hs}\n"
        f"*Трафик:* ⬇ {rx} ⬆ {tx}"
    )

    await c.message.answer(
        text,
        reply_markup=_kb_peer_actions(cfg, pid),
        parse_mode="Markdown",
    )


@router.callback_query(F.data.startswith("up:d|"))
async def peer_download(c: CallbackQuery):
    """Скачать .conf + QR."""
    try:
        await c.answer()
    except Exception:
        pass

    try:
        _, rest = c.data.split("up:d|", 1)
        cfg, pid = rest.split("|", 1)
    except Exception:
        await c.message.answer("Некорректные данные кнопки.")
        return

    try:
        conf_text = await wgd.get_peer_config(cfg, pid)
    except WGDError as e:
        await c.message.answer(f"Не удалось скачать конфиг: {e}")
        return

    fname = f"{cfg}-{pid[:8]}.conf"
    cfg_bytes = conf_text.encode("utf-8")
    qr_bytes = make_qr_png(conf_text)

    await c.message.answer_document(BufferedInputFile(cfg_bytes, filename=fname))
    await c.message.answer_photo(BufferedInputFile(qr_bytes, filename=f"{fname[:-5]}.png"))


@router.callback_query(F.data.startswith("up:x|"))
async def peer_delete(c: CallbackQuery):
    """Удалить пир из WGDashboard и из локальной БД."""
    try:
        await c.answer()
    except Exception:
        pass

    try:
        _, rest = c.data.split("up:x|", 1)
        cfg, pid = rest.split("|", 1)
    except Exception:
        await c.message.answer("Некорректные данные кнопки.")
        return

    u = get_user_by_tgid(c.from_user.id) if c.from_user else None
    row_id = None
    row_name = None
    if u:
        for r in get_user_peers(u.id):
            if str(r.wgd_peer_id) == pid:
                row_id = r.id
                row_name = r.name
                break

    try:
        await wgd.delete_peer(cfg, pid)
    except WGDError as e:
        await c.message.answer(f"Ошибка удаления в WGDashboard: {e}")
        return

    if row_id is not None:
        try:
            revoke_peer_row(row_id)
        except Exception:
            pass

    await c.message.answer(f"Подключение `{row_name or pid[:12]}` удалено.", parse_mode="Markdown")
    await user_peers(c)


# -------------------------- переименование (локально) --------------------------

_RENAME_WAIT: dict[int, tuple[str, str]] = {}

@router.callback_query(F.data.startswith("up:r|"))
async def peer_rename_start(c: CallbackQuery):
    try:
        await c.answer()
    except Exception:
        pass

    if rename_peer_row is None:
        await c.message.answer("Переименование пока недоступно (нет функции rename_peer_row в БД).")
        return

    try:
        _, rest = c.data.split("up:r|", 1)
        cfg, pid = rest.split("|", 1)
    except Exception:
        await c.message.answer("Некорректные данные кнопки.")
        return

    if not c.from_user:
        await c.message.answer("Не удалось определить пользователя.")
        return

    _RENAME_WAIT[c.from_user.id] = (cfg, pid)
    await c.message.answer("Введите новое имя для подключения:", reply_markup=ForceReply(selective=True))

@router.message(F.reply_to_message, F.text)
async def peer_rename_finish(m: Message):
    if not m.from_user:
        return
    key = m.from_user.id
    if key not in _RENAME_WAIT:
        return

    cfg, pid = _RENAME_WAIT.pop(key)
    new_name = (m.text or "").strip()
    if not new_name:
        await m.answer("Имя не должно быть пустым.")
        return

    u = get_user_by_tgid(m.from_user.id)
    if not u:
        await m.answer("Пользователь не найден. Повторите /start.")
        return

    target = None
    for r in get_user_peers(u.id):
        if str(r.wgd_peer_id) == pid:
            target = r
            break

    if not target:
        await m.answer("Подключение не найдено.")
        return

    if rename_peer_row is None:
        await m.answer("Переименование пока недоступно.")
        return

    try:
        rename_peer_row(target.id, new_name)
    except Exception as e:
        await m.answer(f"Не удалось переименовать: {e}")
        return

    await m.answer(f"Готово. Новое имя: `{new_name}`", parse_mode="Markdown")
    fake_cb = CallbackQuery(id="0", from_user=m.from_user, chat_instance="", message=m, data=f"up:s|{cfg}|{pid}")
    await peer_show(fake_cb)


# -------------------------- создание и удаление последнего --------------------------

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

    if u.plan != "unlimited" and (not u.expires_at or now > u.expires_at):
        await c.message.answer(
            "Срок действия вашего тарифа истёк. Обратитесь к администратору.",
            reply_markup=_main_menu_for(c),
        )
        return

    cur = count_user_peers(u.id)
    if not check_limit(cur, u.devices_limit):
        await c.message.answer("Достигнут лимит устройств для вашего тарифа.", reply_markup=_main_menu_for(c))
        return

    cfg_name, cfg_addr, cfg_port = _user_config_params(c.from_user.id)

    name = f"{c.from_user.username or 'user'}-{c.from_user.id}-{now}"
    try:
        await wgd.ensure_config(cfg_name, address=cfg_addr, listen_port=cfg_port, protocol="wg")
        peer_pubkey_or_id = await wgd.create_peer(cfg_name, name)
        config_text = await wgd.get_peer_config(cfg_name, peer_pubkey_or_id)
    except WGDError as e:
        await c.message.answer(f"Ошибка создания подключения: {e}", reply_markup=_main_menu_for(c))
        return
    except Exception as e:
        await c.message.answer(f"Непредвиденная ошибка при создании подключения: {e}", reply_markup=_main_menu_for(c))
        return

    add_peer_row(u.id, cfg_name, peer_pubkey_or_id, name)

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

    rows = get_user_peers(u.id)
    if not rows:
        await c.message.answer("У вас нет активных подключений.", reply_markup=_main_menu_for(c))
        return

    try:
        target = sorted(rows, key=lambda x: getattr(x, "created_at", 0) or getattr(x, "id", 0))[-1]
    except Exception:
        target = rows[-1]

    cfg_for_target = getattr(target, "interface", None) or getattr(target, "wgd_interface", None) or f"wg{c.from_user.id}"

    try:
        await wgd.delete_peer(cfg_for_target, str(target.wgd_peer_id))
    except WGDError as e:
        await c.message.answer(f"Ошибка удаления в WGDashboard: {e}", reply_markup=_main_menu_for(c))
        return
    except Exception as e:
        await c.message.answer(f"Непредвиденная ошибка при удалении: {e}", reply_markup=_main_menu_for(c))
        return

    revoke_peer_row(target.id)
    await c.message.answer(f"Подключение {target.name} удалено.", reply_markup=_main_menu_for(c))
