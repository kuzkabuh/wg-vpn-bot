from __future__ import annotations

import logging
from fastapi import APIRouter, Request, Header, HTTPException
from .settings import SET

logger = logging.getLogger("wgd.webhook")

router = APIRouter()


@router.post("/wgd/webhook")
async def wgd_webhook(
    request: Request,
    x_wgd_secret: str | None = Header(default=None),
):
    """
    Приём вебхуков от WGDashboard.

    Авторизация: заголовок X-WGD-Secret должен совпадать с секретом.
    Если в settings есть SET.wgd_webhook_secret — используем его,
    иначе используем SET.webhook_secret.
    """
    expected = getattr(SET, "wgd_webhook_secret", None) or SET.webhook_secret
    if not expected or x_wgd_secret != expected:
        raise HTTPException(status_code=401, detail="unauthorized")

    try:
        payload = await request.json()
    except Exception:
        payload = None

    logger.info("WGD webhook received: %s", payload)
    # тут можно повесить вашу бизнес-логику
    return {"ok": True}
