from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from ..settings import SET
from ..db import get_user_by_tgid, get_user_peers
from ..utils import human_bytes, human_ago, render_table
from ..wgd_api import wgd, WGDError

router = Router()

# ---------- USER STATS ----------

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
    rows = []
    for pr in peers_rows:
        cfg = getattr(pr, "interface", None) or getattr(pr, "wgd_interface", None) or SET.wgd_interface
        peer_id = str(pr.wgd_peer_id)

        p = wgd.find_peer_in_snapshot(snap, cfg, peer_id)
        if not p:
            # не нашли — покажем как «нет данных»
            rows.append([pr.name, "—", "—", "—", cfg])
            continue

        total_rx += p["rx"]
        total_tx += p["tx"]
        status = "🟢" if p["active"] else "⚪️"
        rows.append([
            p["name"],
            human_bytes(p["rx"]),
            human_bytes(p["tx"]),
            human_ago(p["last_handshake"]),
            cfg,
        ])

    table = render_table(["Пир", "RX", "TX", "Последний HS", "CFG"], rows)
    cap = (
        "<b>📊 Ваша статистика</b>\n"
        f"Всего пиров: <b>{len(peers_rows)}</b>\n"
        f"Трафик: ⬇️ {human_bytes(total_rx)}  ⬆️ {human_bytes(total_tx)}\n"
    )
    await to.answer(cap + "\n" + table)


# ---------- ADMIN STATS / BROWSER ----------

@router.callback_query(F.data == "admin:stats")
async def cb_admin_stats(c: CallbackQuery):
    if c.from_user.id not in SET.admin_ids:
        await c.message.answer("Доступ запрещён.")
        return
    try:
        await c.answer()
    except Exception:
        pass
    await _send_admin_stats(c.message)

@router.message(Command("admin_stats"))
async def cmd_admin_stats(m: Message):
    if m.from_user.id not in SET.admin_ids:
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

@router.callback_query(F.data == "admin:cfgs")
async def cb_admin_cfgs(c: CallbackQuery):
    if c.from_user.id not in SET.admin_ids:
        await c.message.answer("Доступ запрещён.")
        return
    try:
        await c.answer()
    except Exception:
        pass
    await _send_admin_cfgs(c.message)

@router.message(Command("admin_cfgs"))
async def cmd_admin_cfgs(m: Message):
    if m.from_user.id not in SET.admin_ids:
        return
    await _send_admin_cfgs(m)

async def _send_admin_cfgs(to: Message):
    try:
        snap = await wgd.snapshot()
    except WGDError as e:
        await to.answer(f"Ошибка: {e}")
        return

    rows = []
    for name, bucket in sorted(snap.items()):
        peers = bucket["peers"]
        active = sum(1 for p in peers if p["active"])
        rx = sum(p["rx"] for p in peers)
        tx = sum(p["tx"] for p in peers)
        rows.append([name, len(peers), active, human_bytes(rx), human_bytes(tx)])

    table = render_table(["CFG", "Пиров", "Актив", "RX", "TX"], rows)
    await to.answer("<b>📋 Конфигурации</b>\n\n" + table)

@router.callback_query(F.data == "admin:peers")
async def cb_admin_peers(c: CallbackQuery):
    if c.from_user.id not in SET.admin_ids:
        await c.message.answer("Доступ запрещён.")
        return
    try:
        await c.answer()
    except Exception:
        pass
    await _send_admin_peers(c.message)

@router.message(Command("admin_peers"))
async def cmd_admin_peers(m: Message):
    if m.from_user.id not in SET.admin_ids:
        return
    await _send_admin_peers(m)

async def _send_admin_peers(to: Message):
    try:
        snap = await wgd.snapshot()
    except WGDError as e:
        await to.answer(f"Ошибка: {e}")
        return

    rows = []
    for name, bucket in sorted(snap.items()):
        for p in bucket["peers"]:
            status = "🟢" if p["active"] else "⚪️"
            rows.append([
                name,
                p["name"],
                human_bytes(p["rx"]),
                human_bytes(p["tx"]),
                human_ago(p["last_handshake"]),
                status,
            ])

    if not rows:
        await to.answer("Пиров нет.")
        return

    # Чтобы не ушло слишком длинным сообщением — ограничим первыми 100.
    head = ["CFG", "Пир", "RX", "TX", "HS", "St"]
    table = render_table(head, rows[:100])
    suffix = "" if len(rows) <= 100 else f"\n…ещё {len(rows) - 100} строк"
    await to.answer("<b>👥 Все пиры</b>\n\n" + table + suffix)
