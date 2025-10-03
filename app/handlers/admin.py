from __future__ import annotations

import time
from typing import Optional, Tuple

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..settings import SET
from ..wgd_api import wgd, WGDError

router = Router()


# ─── helpers ──────────────────────────────────────────────────────────────────

def _is_admin(tg_id: Optional[int]) -> bool:
    return bool(tg_id) and tg_id in SET.admin_ids

def _fmt_bytes(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    x = float(n or 0)
    i = 0
    while x >= 1024 and i < len(units) - 1:
        x /= 1024.0
        i += 1
    return f"{x:.1f} {units[i]}"

def _fmt_dt(ts: Optional[int]) -> str:
    if not ts:
        return "—"
    try:
        delta = int(time.time()) - int(ts)
        if delta < 0:
            delta = 0
        if delta < 60:
            return f"{delta}s"
        if delta < 3600:
            return f"{delta // 60}m"
        if delta < 86400:
            return f"{delta // 3600}h"
        return f"{delta // 86400}d"
    except Exception:
        return "—"

def _status_dot(active: bool) -> str:
    return "🟢" if active else "⚪️"


# ─── menu ─────────────────────────────────────────────────────────────────────

@router.message(F.text.startswith("/admin"))
async def cmd_admin(m: Message):
    if not _is_admin(getattr(m.from_user, "id", None)):
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Общая статистика", callback_data="admin:stats")
    kb.button(text="🧩 Конфигурации", callback_data="admin:cfgs")
    kb.adjust(1, 1)
    await m.answer("Админ-панель:", reply_markup=kb.as_markup())

@router.callback_query(F.data == "admin:menu")
async def admin_menu(c: CallbackQuery):
    if not _is_admin(getattr(c.from_user, "id", None)):
        await c.message.answer("Доступ запрещён.")
        return
    try:
        await c.answer()
    except Exception:
        pass
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Общая статистика", callback_data="admin:stats")
    kb.button(text="🧩 Конфигурации", callback_data="admin:cfgs")
    kb.adjust(1, 1)
    await c.message.answer("Админ-панель:", reply_markup=kb.as_markup())


# ─── stats ────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:stats")
async def admin_stats(c: CallbackQuery):
    if not _is_admin(getattr(c.from_user, "id", None)):
        await c.message.answer("Доступ запрещён.")
        return
    try:
        await c.answer()
    except Exception:
        pass

    try:
        totals = await wgd.totals()
    except WGDError as e:
        await c.message.answer(f"Ошибка получения статистики: {e}")
        return

    text = (
        "📊 <b>Общая статистика</b>\n"
        f"Конфигураций: <b>{totals['configs']}</b>\n"
        f"Пиров всего: <b>{totals['peers']}</b>\n"
        f"Онлайн: <b>{totals['active_peers']}</b> • "
        f"Оффлайн: <b>{totals['peers'] - totals['active_peers']}</b>\n"
        f"Трафик RX: <b>{_fmt_bytes(totals['rx'])}</b>\n"
        f"Трафик TX: <b>{_fmt_bytes(totals['tx'])}</b>"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Меню", callback_data="admin:menu")
    kb.adjust(1)
    await c.message.answer(text, reply_markup=kb.as_markup())


# ─── config list ──────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:cfgs")
async def admin_cfgs(c: CallbackQuery):
    if not _is_admin(getattr(c.from_user, "id", None)):
        await c.message.answer("Доступ запрещён.")
        return
    try:
        await c.answer()
    except Exception:
        pass

    try:
        snap = await wgd.snapshot()
    except WGDError as e:
        await c.message.answer(f"Ошибка: {e}")
        return

    if not snap:
        await c.message.answer("Конфигурации не найдены.")
        return

    # Сводка в виде компактной таблицы с кнопками «Открыть»
    header = "CFG           Пиров   Актив   RX        TX"
    sep    = "------------  ------  ------  --------  --------"
    rows = []
    kb = InlineKeyboardBuilder()
    for cfg_name, bucket in sorted(snap.items()):
        peers = bucket["peers"]
        active = sum(1 for p in peers if p["active"])
        rx = sum(p["rx"] for p in peers)
        tx = sum(p["tx"] for p in peers)
        rows.append(
            f"{cfg_name:<12}  {len(peers):>6}  {active:>6}  {(_fmt_bytes(rx)):>8}  {(_fmt_bytes(tx)):>8}"
        )
        kb.button(text=f"Открыть {cfg_name}", callback_data=f"admin:cfg:{cfg_name}:0")
    kb.button(text="◀️ Меню", callback_data="admin:menu")
    kb.adjust(1)

    text = "🧩 <b>Конфигурации</b>\n<code>\n" + header + "\n" + sep + "\n" + "\n".join(rows) + "\n</code>"
    await c.message.answer(text, reply_markup=kb.as_markup())


# ─── peers in config (with pagination) ────────────────────────────────────────

def _parse_cfg_req(data: str) -> Tuple[str, int]:
    # data like "admin:cfg:<name>:<offset>"
    payload = data.split("admin:cfg:", 1)[-1]
    if ":" in payload:
        name, off = payload.rsplit(":", 1)
        try:
            return name, max(0, int(off))
        except Exception:
            return payload, 0
    return payload, 0

@router.callback_query(F.data.startswith("admin:cfg:"))
async def admin_cfg_details(c: CallbackQuery):
    if not _is_admin(getattr(c.from_user, "id", None)):
        await c.message.answer("Доступ запрещён.")
        return
    try:
        await c.answer()
    except Exception:
        pass

    cfg_name, offset = _parse_cfg_req(c.data)

    try:
        snap = await wgd.snapshot()
    except WGDError as e:
        await c.message.answer(f"Ошибка: {e}")
        return

    bucket = snap.get(cfg_name)
    if not bucket:
        await c.message.answer(f"Конфигурация <code>{cfg_name}</code> не найдена.")
        return

    peers = bucket["peers"]
    total = len(peers)
    if total == 0:
        await c.message.answer(f"В конфигурации <code>{cfg_name}</code> пиры отсутствуют.")
        return

    # Пагинация по 30 строк
    page_size = 30
    start = min(offset, max(0, total - 1))
    start = (start // page_size) * page_size
    end = min(start + page_size, total)
    part = peers[start:end]

    header = f"🔹 <b>{cfg_name}</b>: {total} пиров (показано {start + 1}-{end})"
    table_h = "Статус  Имя                            RX        TX        HS"
    sep     = "------  ----------------------------  --------  --------  ----"
    rows = []

    # Активные сверху внутри страницы: сортируем по активности и объёму трафика
    part_sorted = sorted(part, key=lambda p: (not p["active"], -(p["rx"] + p["tx"])))
    for p in part_sorted:
        status = _status_dot(p["active"])
        name = (p["name"] or "")[:28]
        rx = _fmt_bytes(p["rx"])
        tx = _fmt_bytes(p["tx"])
        hs = _fmt_dt(p["last_handshake"])
        rows.append(f"{status:<6}  {name:<28}  {rx:>8}  {tx:>8}  {hs:>4}")

    text = header + "\n<code>\n" + table_h + "\n" + sep + "\n" + "\n".join(rows) + "\n</code>"

    # Кнопки навигации
    kb = InlineKeyboardBuilder()
    if start > 0:
        prev_off = max(0, start - page_size)
        kb.button(text="⬅️ Назад", callback_data=f"admin:cfg:{cfg_name}:{prev_off}")
    if end < total:
        next_off = end
        kb.button(text="Вперёд ➡️", callback_data=f"admin:cfg:{cfg_name}:{next_off}")
    kb.button(text="◀️ Меню", callback_data="admin:menu")
    kb.adjust(2 if start > 0 and end < total else 1)
    await c.message.answer(text, reply_markup=kb.as_markup())
