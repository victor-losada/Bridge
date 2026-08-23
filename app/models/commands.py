"""Comandos que el Core envía al Bridge Manager."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ConnectAccountRequest(BaseModel):
    """Conectar una cuenta MT5 a un slot libre (o al indicado)."""

    account_id: str = Field(..., description="UUID interno del Core")
    mt5_login: int
    mt5_password: str
    mt5_server: str
    investor: bool = True
    slot_id: str | None = Field(
        default=None,
        description="Opcional. Si no se indica, se asigna el primer slot free.",
    )


class DisconnectAccountRequest(BaseModel):
    account_id: str | None = None
    slot_id: str | None = None
