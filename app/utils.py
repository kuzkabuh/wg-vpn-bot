import io
import qrcode
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
from .settings import SET

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
    }
}

def make_qr_png(data: str) -> bytes:
    qr = qrcode.QRCode(box_size=8, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    bio = io.BytesIO()
    img.save(bio, format="PNG")
    return bio.getvalue()

def human_dt(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")

def plan_apply(plan: str, from_dt: datetime | None = None):
    from_dt = from_dt or datetime.utcnow()
    spec = PLAN_DEFAULTS.get(plan)
    if not spec:
        raise ValueError("unknown plan")
    end = from_dt + timedelta(days=spec["days"]) if spec["days"] < 36500 else from_dt + relativedelta(years=100)
    return int(end.timestamp()), spec["limit"]

def check_limit(current: int, limit: int) -> bool:
    if limit < 0:
        return True
    return current < limit

# ==== ДОБАВКА В app/utils.py ====
from datetime import datetime, timezone

def human_bytes(n: int) -> str:
    # IEC (KiB/MiB/GiB)
    step = 1024.0
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    x = float(max(0, int(n)))
    for u in units:
        if x < step or u == units[-1]:
            if u == "B":
                return f"{int(x)} {u}"
            return f"{x:.2f} {u}"
        x /= step

def human_ago(ts: int | None) -> str:
    if not ts:
        return "—"
    now = int(datetime.now(tz=timezone.utc).timestamp())
    delta = max(0, now - int(ts))
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        m = delta // 60
        return f"{m}m ago"
    h = delta // 3600
    return f"{h}h ago"

def render_table(headers, rows) -> str:
    # Моноширинная таблица в <pre>
    head = [str(h) for h in headers]
    cols = len(head)
    widths = [len(h) for h in head]
    for r in rows:
        for i in range(cols):
            widths[i] = max(widths[i], len(str(r[i])))

    def fmt_row(r):
        return "  ".join(str(r[i]).ljust(widths[i]) for i in range(cols))

    out = [fmt_row(head), fmt_row(["-" * w for w in widths])]
    for r in rows:
        out.append(fmt_row(r))
    return "<pre>" + "\n".join(out) + "</pre>"
# ==== КОНЕЦ ДОБАВКИ ====
