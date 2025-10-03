# app/handlers/stats.py
from __future__ import annotations

from typing import List

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from ..settings import SET
from ..db import get_user_by_tgid, get_user_peers
from ..utils import human_bytes, human_ago, render_table
from ..wgd_api import wgd, WGDError

router = Router()

# =========================
# Вспомогательные функции
# =========================

def _is_admin(tg_id: int | None) -> bool:
    return bool(tg_id) and tg_id in SET.admin_ids

def _safe_human_ago(ts) -> str:
    """Безопасное форматирование 'последний handshake'."""
    try:
        if not ts or int(ts) <= 0:
            return "—"
        return human_ago(int(ts))
    except Exception:
        return "—"

def _esc(val: object) -> str:
    """Экранируем для HTML <pre> таблицы."""
    s = str(val) if val is not None else ""
    # минимальное экранирование, достаточное для <pre>
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
    )

async def _safe_answer(to: Message, text: str, *, parse_mode: str = "HTML") -> None:
    """
    Отправка длинного текста порциями, чтобы уложиться в лимит Telegram (~4096).
    Разбиваем по строкам с запасом под HTML.
    """
    if not text:
        return
    max_len = 3800  # запас под служебные символы
    if len(text) <= max_len:
        await to.answer(text, parse_mode=parse_mode, disable_web_page_preview=True)
        return

    buf = ""
    for line in text.splitlines(keepends=True):
        if len(buf) + len(line) > max_len:
            await to.answer(buf, parse_mode=parse_mode, disable_web_page_preview=True)
            buf = ""
        buf += line
    if buf:
        await to.answer(buf, parse_mode=parse_mode, disable_web_page_preview=True)

# =========================
# USER: статистика
# =========================

@router.callback_query(F.data == "user:stats")
async def cb_user_stats(c: CallbackQuery):
    try:
        await c.answer()
    except Exception:
        pass
    await _send_user_stats(to=c.message, tg_id=c.from_user.id if c.from_user else 0)

@router.message(Command("stats"))
async def cmd_user_stats(m: Message):
    await _send_user_stats(to=m, tg_id=m.from_user.id if m.from_user else 0)

async def _send_user_stats(to: Message, tg_id: int):
    u = get_user_by_tgid(tg_id)
    if not u or u.status != "approved":
        await _safe_answer(to, "Недоступно. Дождитесь одобрения администратора.")
        return

    peer_rows = get_user_peers(u.id)
    if not peer_rows:
        await _safe_answer(to, "У вас нет активных подключений.")
        return

    try:
        snap = await wgd.snapshot()
    except WGDError as e:
        await _safe_answer(to, f"Ошибка получения статистики: {e}")
        return

    total_rx = 0
    total_tx = 0
    rows: List[List[str]] = []

    for pr in peer_rows:
        # В приоритете сохраняем тот CFG, что хранится у нас в БД; если нет — используем дефолтный
        cfg = getattr(pr, "interface", None) or getattr(pr, "wgd_interface", None) or SET.wgd_interface
        pid = str(pr.wgd_peer_id)

        p = wgd.find_peer_in_snapshot(snap, cfg, pid)
        if not p:
            rows.append([_esc(pr.name), "—", "—", "—", _esc(cfg), "⚪️"])
            continue

        total_rx += int(p["rx"])
        total_tx += int(p["tx"])
        status = "🟢" if p["active"] else "⚪️"

        rows.append([
            _esc(p["name"]),
            _esc(human_bytes(p["rx"])),
            _esc(human_bytes(p["tx"])),
            _esc(_safe_human_ago(p["last_handshake"])),
            _esc(cfg),
            status,
        ])

    header = ["Пир", "RX", "TX", "Последний HS", "CFG", "St"]
    table = render_table(header, rows)

    cap = (
        "<b>📊 Ваша статистика</b>\n"
        f"Всего пиров: <b>{len(peer_rows)}</b>\n"
        f"Трафик: ⬇️ {human_bytes(total_rx)}  ⬆️ {human_bytes(total_tx)}\n"
    )
    await _safe_answer(to, cap + "\n" + table)

# =========================
# ADMIN: суммарка и обзоры
# =========================

@router.callback_query(F.data == "admin:stats")
async def cb_admin_stats(c: CallbackQuery):
    if not _is_admin(getattr(c.from_user, "id", None)):
        await _safe_answer(c.message, "Доступ запрещён.")
        return
    try:
        await c.answer()
    except Exception:
        pass
    await _send_admin_stats(c.message)

@router.message(Command("admin_stats"))
async def cmd_admin_stats(m: Message):
    if not _is_admin(getattr(m.from_user, "id", None)):
        return
    await _send_admin_stats(m)

async def _send_admin_stats(to: Message):
    try:
        totals = await wgd.totals()
    except WGDError as e:
        await _safe_answer(to, f"Ошибка: {e}")
        return

    text = (
        "<b>📈 Общая статистика</b>\n"
        f"Конфигураций: <b>{totals['configs']}</b>\n"
        f"Пиров: <b>{totals['peers']}</b>\n"
        f"Активных: <b>{totals['active_peers']}</b>\n"
        f"Трафик: ⬇️ {human_bytes(totals['rx'])}  ⬆️ {human_bytes(totals['tx'])}\n"
    )
    await _safe_answer(to, text)

# --- список конфигураций с краткой статистикой ---

@router.callback_query(F.data == "admin:cfgs")
async def cb_admin_cfgs(c: CallbackQuery):
    if not _is_admin(getattr(c.from_user, "id", None)):
        await _safe_answer(c.message, "Доступ запрещён.")
        return
    try:
        await c.answer()
    except Exception:
        pass
    await _send_admin_cfgs(c.message)

@router.message(Command("admin_cfgs"))
async def cmd_admin_cfgs(m: Message):
    if not _is_admin(getattr(m.from_user, "id", None)):
        return
    await _send_admin_cfgs(m)

async def _send_admin_cfgs(to: Message):
    try:
        snap = await wgd.snapshot()
    except WGDError as e:
        await _safe_answer(to, f"Ошибка: {e}")
        return

    if not snap:
        await _safe_answer(to, "Конфигурации не найдены.")
        return

    rows: List[List[str]] = []
    for name in sorted(snap.keys()):
        peers = snap[name]["peers"]
        active = sum(1 for p in peers if p["active"])
        rx = sum(p["rx"] for p in peers)
        tx = sum(p["tx"] for p in peers)
        rows.append([_esc(name), str(len(peers)), str(active), human_bytes(rx), human_bytes(tx)])

    table = render_table(["CFG", "Пиров", "Актив", "RX", "TX"], rows)
    await _safe_answer(to, "<b>🧩 Конфигурации</b>\n\n" + table)

# --- все пиры (укороченный список, чтобы не упереться в лимит Telegram) ---

@router.callback_query(F.data == "admin:peers")
async def cb_admin_peers(c: CallbackQuery):
    if not _is_admin(getattr(c.from_user, "id", None)):
        await _safe_answer(c.message, "Доступ запрещён.")
        return
    try:
        await c.answer()
    except Exception:
        pass
    await _send_admin_peers(c.message)

@router.message(Command("admin_peers"))
async def cmd_admin_peers(m: Message):
    if not _is_admin(getattr(m.from_user, "id", None)):
        return
    await _send_admin_peers(m)

async def _send_admin_peers(to: Message):
    try:
        snap = await wgd.snapshot()
    except WGDError as e:
        await _safe_answer(to, f"Ошибка: {e}")
        return

    rows: List[List[str]] = []
    # Порядок: активные сверху, затем по суммарному трафику
    for name in sorted(snap.keys()):
        peers = sorted(
            snap[name]["peers"],
            key=lambda p: (not p["active"], -(int(p["rx"]) + int(p["tx"]))),
        )
        for p in peers:
            status = "🟢" if p["active"] else "⚪️"
            rows.append([
                _esc(name),
                _esc(p["name"]),
                _esc(human_bytes(p["rx"])),
                _esc(human_bytes(p["tx"])),
                _esc(_safe_human_ago(p["last_handshake"])),
                status,
            ])

    if not rows:
        await _safe_answer(to, "Пиров нет.")
        return

    head = ["CFG", "Пир", "RX", "TX", "HS", "St"]
    # Разобьём на порции, чтобы гарантированно влезть в лимит
    chunk = 80
    total = len(rows)
    sent = 0
    idx = 0

    while sent < total:
        part = rows[sent:sent + chunk]
        table = render_table(head, part)
        idx += 1
        sent += len(part)
        suffix = "" if sent >= total else f"\n…ещё {total - sent} строк"
        title = "<b>👥 Все пиры</b>\n\n" if idx == 1 else ""
        await _safe_answer(to, title + table + suffix)
