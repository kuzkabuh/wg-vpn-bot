# app/wgd_webhook.py
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request

from .settings import SET
from .wgd_api import wgd  # используем по duck-typing: хуки вызываем, если существуют

logger = logging.getLogger("wgd.webhook")
router = APIRouter()


# ───────────────────────── helpers ─────────────────────────

def _expected_secret() -> Optional[str]:
    """Что считаем валидным секретом для приёма вебхуков."""
    return getattr(SET, "wgd_webhook_secret", None) or getattr(SET, "webhook_secret", None)


def _pick_secret_from_headers(request: Request, x_wgd_secret_hdr: Optional[str]) -> Optional[str]:
    """
    Достаём секрет из разных вариантов заголовка.
    FastAPI уже даёт нам x_wgd_secret_hdr = 'x-wgd-secret',
    но на всякий случай посмотрим и в другие ключи.
    """
    if x_wgd_secret_hdr:
        return x_wgd_secret_hdr

    # доп. алиасы, встречающиеся в форках/реверс-прокси
    # (регистр для headers в Starlette нечувствителен)
    for key in (
        "x-wgd-secret",
        "x-wgdashboard-secret",
        "x-wg-dashboard-secret",
        "x-wg-secret",
    ):
        val = request.headers.get(key)
        if val:
            return val
    return None


async def _read_payload(request: Request) -> Dict[str, Any]:
    """
    Считываем тело вебхука как JSON. Если не получилось — пробуем form/urlencoded,
    затем plain text -> JSON.
    """
    # 1) Прямая попытка JSON
    try:
        data = await request.json()
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            # оборачиваем список в {'items': [...]}, чтобы не упасть
            return {"items": data}
    except Exception:
        pass

    # 2) Попытка прочитать как form
    try:
        form = await request.form()
        # конвертнём в обычный dict
        data = {k: v for k, v in form.items()}
        # иногда поле 'payload' само содержит JSON
        for k in ("payload", "data", "event"):
            if k in data and isinstance(data[k], str):
                try:
                    inner = json.loads(data[k])
                    if isinstance(inner, dict):
                        data.update(inner)
                except Exception:
                    pass
        return data
    except Exception:
        pass

    # 3) Plain text -> попробовать json.loads
    try:
        body = await request.body()
        if body:
            txt = body.decode("utf-8", errors="replace")
            try:
                loaded = json.loads(txt)
                if isinstance(loaded, dict):
                    return loaded
                return {"items": loaded}
            except Exception:
                return {"raw": txt}
    except Exception:
        pass

    return {}


def _norm_event(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Нормализуем полезную часть события:
      - тип/действие
      - имя конфигурации
      - публичный ключ/ID пира
      - rx/tx и последний handshake (если есть)
    Возвращаем небольшой dict, пригодный для хэндлеров.
    """
    evt_type = (
        payload.get("event")
        or payload.get("type")
        or payload.get("action")
        or payload.get("Event")
        or "unknown"
    )

    # разные варианты поля конфигурации
    cfg = (
        payload.get("config")
        or payload.get("configuration")
        or payload.get("ConfigurationName")
        or payload.get("configName")
        or payload.get("ConfigName")
        or payload.get("interface")
        or payload.get("Interface")
    )

    # сам пир может лежать в payload['peer'] / ['data'] / корне
    peer = payload.get("peer") or payload.get("data") or payload

    # public key / id
    public_key = (
        peer.get("publicKey")
        or peer.get("public_key")
        or peer.get("PublicKey")
    )
    peer_id = (
        peer.get("id")
        or peer.get("peer_id")
        or peer.get("Id")
    )

    # трафик
    rx = (
        peer.get("rx")
        or peer.get("receive")
        or peer.get("transferRx")
        or peer.get("TransferRx")
        or peer.get("ReceiveBytes")
        or 0
    )
    tx = (
        peer.get("tx")
        or peer.get("sent")
        or peer.get("transferTx")
        or peer.get("TransferTx")
        or peer.get("TransmitBytes")
        or 0
    )

    # рукопожатие/время
    hs = (
        peer.get("LatestHandshake")
        or peer.get("latestHandshake")
        or peer.get("latest_handshake")
        or peer.get("LastHandshake")
        or peer.get("Handshake")
        or peer.get("handshake")
    )

    return {
        "event": str(evt_type),
        "config": cfg and str(cfg),
        "public_key": public_key and str(public_key),
        "peer_id": peer_id and str(peer_id),
        "rx": rx,
        "tx": tx,
        "last_handshake": hs,
        "raw": payload,
    }


async def _dispatch_to_wgd(event: Dict[str, Any]) -> None:
    """
    Передаём событие в WGDAPI, если в нём реализованы соответствующие хуки.
    Делаем это best-effort: метод может отсутствовать — тогда просто логируем.
    """
    # 1) Универсальный хук: on_webhook(event_dict)
    hook = getattr(wgd, "on_webhook", None)
    if callable(hook):
        try:
            await hook(event)  # type: ignore[arg-type]
            return
        except Exception as e:
            logger.debug("on_webhook failed: %r", e)

    # 2) Попытка применить дельту (словарь с peer/config)
    apply_delta = getattr(wgd, "apply_webhook_delta", None)
    if callable(apply_delta):
        try:
            await apply_delta(event)  # type: ignore[arg-type]
            return
        except Exception as e:
            logger.debug("apply_webhook_delta failed: %r", e)

    # 3) Точечное обновление/инвалидция (часто достаточно)
    touch = getattr(wgd, "_webhook_touch", None)
    if callable(touch):
        try:
            await touch(
                event.get("config"),
                event.get("public_key") or event.get("peer_id"),
                event.get("rx"),
                event.get("tx"),
                event.get("last_handshake"),
                event.get("event"),
            )
            return
        except Exception as e:
            logger.debug("_webhook_touch failed: %r", e)

    invalidate = getattr(wgd, "invalidate_cache", None)
    if callable(invalidate):
        try:
            invalidate()  # sync допустим
            return
        except Exception as e:
            logger.debug("invalidate_cache failed: %r", e)

    # если до сюда дошли — хендлеров нет, ограничимся логом
    logger.debug("No webhook hooks on WGDAPI; event ignored at runtime.")


# ───────────────────────── route ─────────────────────────

@router.post("/wgd/webhook")
async def wgd_webhook(
    request: Request,
    background: BackgroundTasks,
    x_wgd_secret: str | None = Header(default=None),
):
    """
    Приём вебхуков от WGDashboard (и форков).
    Авторизация через секрет в заголовке (см. настройки).
    """
    expected = _expected_secret()
    got = _pick_secret_from_headers(request, x_wgd_secret)

    if not expected:
        logger.warning("WGD webhook rejected: secret not configured in settings")
        raise HTTPException(status_code=503, detail="webhook secret not configured")

    if got != expected:
        logger.info("WGD webhook unauthorized: invalid secret")
        raise HTTPException(status_code=401, detail="unauthorized")

    payload = await _read_payload(request)
    if not payload:
        # Пусть будет 200, чтобы WGDashboard не ретраил бесконечно, но залогируем
        logger.warning("WGD webhook: empty/invalid payload")
        return {"ok": True, "note": "empty payload"}

    # Нормализуем и отправим обработку в фон, чтобы быстро ответить на запрос
    event = _norm_event(payload)
    logger.info("WGD webhook: %s", json.dumps(event, ensure_ascii=False))

    background.add_task(_dispatch_to_wgd, event)

    # быстрый ACK
    return {"ok": True}
