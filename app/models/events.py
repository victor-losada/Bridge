"""Eventos normalizados que el Worker emite hacia el Core."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ConnectionStatusData(BaseModel):
    status: Literal[
        "connecting",
        "connected",
        "disconnected",
        "error",
        "reconnecting",
    ]
    slot_id: str
    message: str | None = None

    def to_payload(self) -> dict[str, Any]:
        """snake_case + camelCase: el Core puede leer cualquiera de las dos."""
        data = self.model_dump()
        data["slotId"] = self.slot_id
        return data


class AccountSnapshotData(BaseModel):
    """Stats de la cuenta.

    Histórico: este evento salía en snake_case mientras `position.*` y
    `trade.closed` ya viajaban en camelCase. `to_payload()` emite AMBAS
    grafías para que el Core mapee los stats sea cual sea la que espere.
    """

    balance: float
    equity: float
    margin: float
    free_margin: float
    margin_level: float | None = None
    profit: float
    currency: str
    leverage: int
    name: str | None = None
    server: str | None = None

    def to_payload(self) -> dict[str, Any]:
        data = self.model_dump()
        data["freeMargin"] = self.free_margin
        data["marginLevel"] = self.margin_level
        return data


class PositionData(BaseModel):
    positionId: str
    symbol: str
    type: Literal["buy", "sell"]
    volume: float
    openPrice: float
    currentPrice: float | None = None
    profit: float = 0.0
    swap: float = 0.0
    commission: float = 0.0
    stopLoss: float | None = None
    takeProfit: float | None = None
    openTime: str
    comment: str | None = None


class ClosedTradeData(BaseModel):
    """Payload de trade.closed — contrato estable con el Core."""

    positionId: str
    dealId: str
    symbol: str
    type: Literal["buy", "sell"]
    volume: float
    openPrice: float
    closePrice: float
    openTime: str
    closeTime: str
    profit: float
    swap: float = 0.0
    commission: float = 0.0
    stopLoss: float | None = None
    takeProfit: float | None = None


class BridgeEvent(BaseModel):
    event: Literal[
        "connection.status",
        "account.snapshot",
        "position.opened",
        "position.updated",
        "position.closed",
        "trade.closed",
    ]
    account_id: str
    mt5_login: int
    timestamp: str
    data: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def make(
        cls,
        event: str,
        account_id: str,
        mt5_login: int,
        timestamp: str,
        data: BaseModel | dict[str, Any],
    ) -> BridgeEvent:
        payload = data.model_dump() if isinstance(data, BaseModel) else data
        return cls(
            event=event,  # type: ignore[arg-type]
            account_id=account_id,
            mt5_login=mt5_login,
            timestamp=timestamp,
            data=payload,
        )
