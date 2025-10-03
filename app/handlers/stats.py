from datetime import datetime, timezone
from typing import Tuple, List

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from ..settings import SET
from ..db import (
    get_user_by_tgid,
    get_user_peers,
    count_user_peers,
    add_peer_row,
    update_user,          # может пригодиться позже
    revoke_peer_row,
)
from ..utils import human_bytes, human_ago, render_table
from ..wgd_api import wgd, WGDError

router = Router()

# =========================
# Вспомогательные функции
# =========================

def _safe_human_ago(ts) -> str:
    """Безопасное форматирование 'последнего handshake'."""
    if not ts or ts <= 0:
        return "—"
    try:
        return human_ago(int(ts))
    except Exception:
        return "—"

async def _safe_answer(to: Message, text: str) -> None:
    """
    Отправка длинного текста порциями, чтобы уложиться в лимит Telegram (~4096).
    """
    if not text:
        return
    max_len = 3800  # небольшой запас под HTML/markdown
    if len(text) <= max_len:
        await to.answer(text)
        return
    # режем по строкам
    lines = text.splitlines(True)
    buf = ""
    for line in lines:
        if len(buf) + len(line) > max_len:
            await to.answer(buf)
            buf = ""
        buf += line
    if buf:
        await to.answer(buf)

def _is_admin(tg_id: int) -> bool:
    return tg_id in SET.admin_ids

# =========================
# USER: статистика
# =========================

@router.callback_query(F.data == "user:stats")
async def cb_user_stats(c: CallbackQuery):
    try:
        await c.answer()
    except Exception:
        pass
    await _send_user_stats(to=c.message, tg_id=c.from_user.id)

@router.message(Command("stats"))
async def cmd_user_stats(m: Message):
    await _send_user_stats(to=m, tg_id=m.from_user.id)

async def _send_user_stats(to: Message, tg_id: int):
    u = get_user_by_tgid(tg_id)
    if not u or u.status != "approved":
        await to.answer("Недоступно. Дождитесь одобрения администратора.")
        return

    peers_rows = get_user_peers(u.id)
    if not peers_rows:
        await to.answer("У вас нет активных подключений.")
        return

    try:
        snap = await wgd.snapshot()
    except WGDError as e:
        await to.answer(f"Ошибка получения статистики: {e}")
        return

    total_rx = total_tx = 0
    rows: List[List[str]] = []
    for pr in peers_rows:
        cfg = getattr(pr, "interface", None) or getattr(pr, "wgd_interface", None) or SET.wgd_interface
        peer_id = str(pr.wgd_peer_id)

        p = wgd.find_peer_in_snapshot(snap, cfg, peer_id)
        if not p:
            # Не нашли — покажем «нет данных»
            rows.append([pr.name, "—", "—", "—", cfg, "⚪️"])
            continue

        total_rx += p["rx"]
        total_tx += p["tx"]
        status = "🟢" if p["active"] else "⚪️"
        rows.append([
            p["name"],
            human_bytes(p["rx"]),
            human_bytes(p["tx"]),
            _safe_human_ago(p["last_handshake"]),
            cfg,
            status,
        ])

    header = ["Пир", "RX", "TX", "Последний HS", "CFG", "St"]
    table = render_table(header, rows)

    cap = (
        "<b>📊 Ваша статистика</b>\n"
        f"Всего пиров: <b>{len(peers_rows)}</b>\n"
        f"Трафик: ⬇️ {human_bytes(total_rx)}  ⬆️ {human_bytes(total_tx)}\n"
    )
    await _safe_answer(to, cap + "\n" + table)

# =========================
# ADMIN: суммарка и обзоры
# =========================

@router.callback_query(F.data == "admin:stats")
async def cb_admin_stats(c: CallbackQuery):
    if not _is_admin(c.from_user.id):
        await c.message.answer("Доступ запрещён.")
        return
    try:
        await c.answer()
    except Exception:
        pass
    await _send_admin_stats(c.message)

@router.message(Command("admin_stats"))
async def cmd_admin_stats(m: Message):
    if not _is_admin(m.from_user.id):
        return
    await _send_admin_stats(m)

async def _send_admin_stats(to: Message):
    try:
        totals = await wgd.totals()
    except WGDError as e:
        await to.answer(f"Ошибка: {e}")
        return

    text = (
        "<b>📈 Общая статистика</b>\n"
        f"Конфигураций: <b>{totals['configs']}</b>\n"
        f"Пиров: <b>{totals['peers']}</b>\n"
        f"Активных: <b>{totals['active_peers']}</b>\n"
        f"Трафик: ⬇️ {human_bytes(totals['rx'])}  ⬆️ {human_bytes(totals['tx'])}\n"
    )
    await to.answer(text)

# --- список конфигураций с краткой статистикой ---

@router.callback_query(F.data == "admin:cfgs")
async def cb_admin_cfgs(c: CallbackQuery):
    if not _is_admin(c.from_user.id):
        await c.message.answer("Доступ запрещён.")
        return
    try:
        await c.answer()
    except Exception:
        pass
    await _send_admin_cfgs(c.message)

@router.message(Command("admin_cfgs"))
async def cmd_admin_cfgs(m: Message):
    if not _is_admin(m.from_user.id):
        return
    await _send_admin_cfgs(m)

async def _send_admin_cfgs(to: Message):
    try:
        snap = await wgd.snapshot()
    except WGDError as e:
        await to.answer(f"Ошибка: {e}")
        return

    if not snap:
        await to.answer("Конфигурации не найдены.")
        return

    rows: List[List[str]] = []
    for name in sorted(snap.keys()):
        peers = snap[name]["peers"]
        active = sum(1 for p in peers if p["active"])
        rx = sum(p["rx"] for p in peers)
        tx = sum(p["tx"] for p in peers)
        rows.append([name, str(len(peers)), str(active), human_bytes(rx), human_bytes(tx)])

    table = render_table(["CFG", "Пиров", "Актив", "RX", "TX"], rows)
    await _safe_answer(to, "<b>🧩 Конфигурации</b>\n\n" + table)

# --- все пиры (укороченный список, чтобы не упереться в лимит Telegram) ---

@router.callback_query(F.data == "admin:peers")
async def cb_admin_peers(c: CallbackQuery):
    if not _is_admin(c.from_user.id):
        await c.message.answer("Доступ запрещён.")
        return
    try:
        await c.answer()
    except Exception:
        pass
    await _send_admin_peers(c.message)

@router.message(Command("admin_peers"))
async def cmd_admin_peers(m: Message):
    if not _is_admin(m.from_user.id):
        return
    await _send_admin_peers(m)

async def _send_admin_peers(to: Message):
    try:
        snap = await wgd.snapshot()
    except WGDError as e:
        await to.answer(f"Ошибка: {e}")
        return

    rows: List[List[str]] = []
    # Порядок: активные сверху
    for name in sorted(snap.keys()):
        peers = sorted(snap[name]["peers"], key=lambda p: (not p["active"], -(p["rx"] + p["tx"])))
        for p in peers:
            status = "🟢" if p["active"] else "⚪️"
            rows.append([
                name,
                p["name"],
                human_bytes(p["rx"]),
                human_bytes(p["tx"]),
                _safe_human_ago(p["last_handshake"]),
                status,
            ])

    if not rows:
        await to.answer("Пиров нет.")
        return

    head = ["CFG", "Пир", "RX", "TX", "HS", "St"]
    # ограничим 100 строк на одно сообщение, чтобы точно поместилось
    chunk = 100
    for i in range(0, len(rows), chunk):
        part = rows[i:i + chunk]
        suffix = "" if i + chunk >= len(rows) else f"\n…ещё {len(rows) - (i + chunk)} строк"
        table = render_table(head, part)
        await _safe_answer(to, "<b>👥 Все пиры</b>\n\n" + table + suffix)
        # только первый блок с заголовком «Все пиры», дальше — без него
        head = head
