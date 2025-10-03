from __future__ import annotations

import io
import time
from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence

import qrcode
from dateutil.relativedelta import relativedelta

from .settings import SET


# ───────────────────────── тарифы ─────────────────────────

PLAN_DEFAULTS = {
    "trial": {
        "days": SET.trial_days,
        "limit": SET.trial_device_limit,
    },
    "paid": {
        "days": SET.paid_days,
        "limit": SET.paid_device_limit,
    },
    "unlimited": {
        "days": 36500,   # фактически бессрочно
        "limit": -1,
    },
}


# ───────────────────────── базовые утилиты ─────────────────────────

def now_ts() -> int:
    """Текущий момент в UTC, unix timestamp (int)."""
    return int(time.time())


def to_utc(dt: datetime) -> datetime:
    """Сделать datetime timezone-aware (UTC)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def make_qr_png(data: str) -> bytes:
    """Сгенерировать PNG с QR-кодом и вернуть как bytes."""
    qr = qrcode.QRCode(box_size=8, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    bio = io.BytesIO()
    img.save(bio, format="PNG")
    return bio.getvalue()


def human_dt(ts: int | float | datetime | None) -> str:
    """
    Красивое отображение момента времени в формате DD.MM.YYYY HH:MM (UTC).
    Принимает unix timestamp или datetime.
    """
    if ts is None:
        return "—"
    try:
        if isinstance(ts, datetime):
            dt_utc = to_utc(ts)
        else:
            dt_utc = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        return dt_utc.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return "—"


def plan_apply(plan: str, from_dt: datetime | None = None) -> tuple[int, int]:
    """
    Применить тариф:
      возвращает (expires_at_utc_ts, devices_limit).
    """
    spec = PLAN_DEFAULTS.get(plan)
    if not spec:
        raise ValueError(f"unknown plan: {plan}")

    base = to_utc(from_dt) if from_dt else datetime.now(tz=timezone.utc)
    if spec["days"] < 36500:
        end = base + timedelta(days=int(spec["days"]))
    else:
        end = base + relativedelta(years=100)
    return int(end.timestamp()), int(spec["limit"])


def check_limit(current: int, limit: int) -> bool:
    """True — можно добавить ещё устройство."""
    if limit < 0:
        return True
    return int(current) < int(limit)


# ───────────────────────── человекочитаемые форматы ─────────────────────────

def human_bytes(n: int | float | str | None) -> str:
    """
    Форматирование байт в B/KB/MB/GB/TB с 1 десятичным знаком (кроме B).
    Поддерживает числа в строке, обрезает отрицательные значения до 0.
    """
    try:
        if n is None:
            v = 0.0
        elif isinstance(n, (int, float)):
            v = float(n)
        else:
            # строки вида "0.0032 GB" также терпимы
            parts = str(n).strip().split()
            v = float(parts[0])
            if len(parts) > 1:
                unit = parts[1].upper()
                order = {"B": 0, "KB": 1, "MB": 2, "GB": 3, "TB": 4}.get(unit, 0)
                v *= 1024 ** order
        v = max(0.0, v)
    except Exception:
        v = 0.0

    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while v >= 1024.0 and i < len(units) - 1:
        v /= 1024.0
        i += 1
    if i == 0:
        return f"{int(v)} {units[i]}"
    return f"{v:.1f} {units[i]}"


def human_ago(ts: int | float | None) -> str:
    """
    Сколько времени прошло назад (на русском).
    """
    if not ts:
        return "—"
    try:
        delta = max(0, now_ts() - int(float(ts)))
    except Exception:
        return "—"

    if delta < 120:
        return "только что"
    m = delta // 60
    if m < 60:
        return f"{m} мин назад"
    h = m // 60
    if h < 24:
        return f"{h} ч назад"
    d = h // 24
    return f"{d} дн назад"


def render_table(headers: Sequence, rows: Iterable[Sequence]) -> str:
    """
    Моноширинная таблица под Telegram HTML parse_mode.
    Пример:
      html = render_table(["Колонка", "Знач"], [[1, "ok"], [2, "no"]])
      await m.answer(html, parse_mode="HTML")
    """
    head = [str(h) for h in headers]
    cols = len(head)
    widths = [len(h) for h in head]

    data_rows = []
    for r in rows:
        r = list(r)
        # выравнивание ширин
        for i in range(cols):
            cell = "" if i >= len(r) or r[i] is None else str(r[i])
            r[i] = cell
            if len(cell) > widths[i]:
                widths[i] = len(cell)
        data_rows.append(r)

    def fmt_row(r: Sequence[str]) -> str:
        return "  ".join((r[i] if i < len(r) else "").ljust(widths[i]) for i in range(cols))

    out = [fmt_row(head), fmt_row(["-" * w for w in widths])]
    for r in data_rows:
        out.append(fmt_row(r))
    return "<pre>" + "\n".join(out) + "</pre>"
