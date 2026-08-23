"""API del Bridge Manager. El Core llama aquí; no es público."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from app.config import Settings, get_settings
from app.manager.slot_manager import SlotManager, SlotManagerError
from app.models.commands import ConnectAccountRequest, DisconnectAccountRequest

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
