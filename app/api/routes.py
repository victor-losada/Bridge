"""API del Bridge Manager. El Core llama aquí; no es público."""

from __future__ import annotations

import json
import logging

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request

from app.config import Settings, get_settings
from app.manager.slot_manager import SlotManager, SlotManagerError
from app.models.commands import ConnectAccountRequest, DisconnectAccountRequest
from app.models.events import BridgeEvent, ConnectionStatusData
from app.utils import to_iso_z

router = APIRouter()
logger = logging.getLogger(__name__)


def _manager(request: Request) -> SlotManager:
    mgr = getattr(request.app.state, "slot_manager", None)
    if mgr is None:
        raise HTTPException(503, "SlotManager no inicializado")
    return mgr


def require_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    token = x_api_key
    if not token and authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1]
    if token != settings.bridge_api_key:
        raise HTTPException(status_code=401, detail="unauthorized")


def require_core_or_bridge_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    """El Worker manda CORE_API_KEY; el sink local acepta esa o la del Manager."""
    token = x_api_key
    if not token and authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1]
    if token not in {settings.bridge_api_key, settings.core_api_key}:
        raise HTTPException(status_code=401, detail="unauthorized")


@router.get("/health")
def health() -> dict:
    return {"ok": True, "service": "mt5-bridge"}


@router.get("/slots", dependencies=[Depends(require_key)])
def list_slots(request: Request) -> dict:
    mgr = _manager(request)
    return {"slots": mgr.list_slots()}


@router.get("/slots/{slot_id}", dependencies=[Depends(require_key)])
def get_slot(slot_id: str, request: Request) -> dict:
    mgr = _manager(request)
    try:
        state = mgr.get_slot(slot_id)
    except SlotManagerError as exc:
        raise HTTPException(404, str(exc)) from exc
    data = state.public_dict()
    data["terminal_ready"] = mgr.terminal_ready(slot_id)
    return data


@router.post("/accounts/connect", dependencies=[Depends(require_key)])
async def connect_account(body: ConnectAccountRequest, request: Request) -> dict:
    mgr = _manager(request)
    try:
        state = await mgr.connect(
            account_id=body.account_id,
            mt5_login=body.mt5_login,
            mt5_password=body.mt5_password,
            mt5_server=body.mt5_server,
            investor=body.investor,
            slot_id=body.slot_id,
        )
    except SlotManagerError as exc:
        msg = str(exc)
        code = 409 if "ya está" in msg or "no está libre" in msg or "no hay slots" in msg else 400
        raise HTTPException(code, msg) from exc
    return {"ok": True, "slot": state.public_dict()}


@router.post("/accounts/disconnect", dependencies=[Depends(require_key)])
async def disconnect_account(body: DisconnectAccountRequest, request: Request) -> dict:
    mgr = _manager(request)
    try:
        state = await mgr.disconnect(account_id=body.account_id, slot_id=body.slot_id)
    except SlotManagerError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"ok": True, "slot": state.public_dict()}


@router.post("/slots/{slot_id}/restart", dependencies=[Depends(require_key)])
async def restart_slot(slot_id: str, request: Request) -> dict:
    mgr = _manager(request)
    try:
        state = await mgr.restart(slot_id)
    except SlotManagerError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "slot": state.public_dict()}


@router.post("/core-ping", dependencies=[Depends(require_key)])
async def core_ping(request: Request) -> dict:
    """Comprueba el canal Bridge → Core con un evento real de prueba.

    Responde con el status y el cuerpo EXACTOS que devuelve el Core. Es el
    primer paso cuando una cuenta "conecta" pero no llegan datos:

      - error de red/TLS  → el Worker tampoco puede emitir (proxy, firewall).
      - 401/403           → CORE_API_KEY no coincide con la del Core.
      - 404               → CORE_EVENTS_URL apunta a una ruta que no existe.
      - 2xx               → el transporte va bien; el problema es el mapeo
                            de `data` en el Core.

    Emite `connection.status` (nunca stats falsos), así que no ensucia datos.
    """
    settings = get_settings()
    mgr = _manager(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    account_id = str(body.get("account_id") or "bridge-core-ping")

    slot_id = "Slot-00"
    state = mgr.find_by_account(account_id)
    if state is not None:
        slot_id = state.slot_id

    event = BridgeEvent.make(
        event="connection.status",
        account_id=account_id,
        mt5_login=int(body.get("mt5_login") or 0),
        timestamp=to_iso_z(),
        data=ConnectionStatusData(
            status="connecting",
            slot_id=slot_id,
            message="core-ping del Bridge (diagnóstico, no es una conexión real)",
        ).to_payload(),
    )

    result: dict = {"core_events_url": settings.core_events_url, "ok": False}
    try:
        async with httpx.AsyncClient(
            timeout=15.0, trust_env=settings.core_http_trust_env
        ) as client:
            response = await client.post(
                settings.core_events_url,
                json=event.model_dump(),
                headers={
                    "Content-Type": "application/json",
                    "X-API-Key": settings.core_api_key,
                    "Authorization": f"Bearer {settings.core_api_key}",
                },
            )
    except httpx.HTTPError as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["hint"] = (
            "El Core no es alcanzable desde esta máquina. Revisa DNS, firewall, "
            "certificado TLS o pon CORE_HTTP_TRUST_ENV=true si hay proxy."
        )
        logger.error("core-ping falló: %s", result["error"])
        return result

    result["status_code"] = response.status_code
    result["response_body"] = response.text[:1000]
    result["ok"] = response.status_code < 400
    if response.status_code in (401, 403):
        result["hint"] = "CORE_API_KEY no coincide con la clave que espera el Core."
    elif response.status_code == 404:
        result["hint"] = "CORE_EVENTS_URL no existe en el Core (revisa la ruta)."
    elif result["ok"]:
        result["hint"] = (
            "Transporte OK: el Core acepta eventos. Si aun así no cargan los "
            "stats, el fallo está en cómo el Core mapea el campo `data` de "
            "account.snapshot."
        )
    logger.info("core-ping status=%s", response.status_code)
    return result


@router.post("/debug/events", dependencies=[Depends(require_core_or_bridge_key)])
async def debug_events(request: Request) -> dict:
    """Receptor local: el Worker POST aquí en vez del Core.

    CORE_EVENTS_URL=http://127.0.0.1:8088/api/v1/debug/events
    """
    payload = await request.json()
    event_name = payload.get("event")
    logger.info("debug event=%s account_id=%s", event_name, payload.get("account_id"))
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    sink = settings.data_dir / "events.jsonl"
    with sink.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return {"ok": True, "stored": event_name}
