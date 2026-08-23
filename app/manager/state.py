"""Estado en memoria de cada slot del pool."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from app.utils import utc_now


class SlotStatus(str, Enum):
    FREE = "free"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"
    DISCONNECTED = "disconnected"


class SlotState(BaseModel):
    """Un slot = una carpeta de terminal portable + un proceso Worker."""

    slot_id: str
    status: SlotStatus = SlotStatus.FREE
    account_id: str | None = None
    mt5_login: int | None = None
    mt5_server: str | None = None
    investor: bool = True
    # Password cifrada con Fernet; nunca se serializa a logs.
    password_encrypted: str | None = Field(default=None, repr=False)
    worker_pid: int | None = None
    terminal_pid: int | None = None
    last_error: str | None = None
    restart_count: int = 0
    updated_at: datetime = Field(default_factory=utc_now)
    # Canal Worker → Core (lo publica el Worker en slot_runtime.json).
    # Sin esto, un Core que rechaza los eventos es invisible desde la API.
    emit: dict[str, Any] = Field(default_factory=dict)

    def touch(self) -> None:
        self.updated_at = utc_now()

    def public_dict(self) -> dict[str, Any]:
        """Vista segura para la API (sin credenciales)."""
        return {
            "slot_id": self.slot_id,
            "status": self.status.value,
            "account_id": self.account_id,
            "mt5_login": self.mt5_login,
            "mt5_server": self.mt5_server,
            "investor": self.investor,
            "worker_pid": self.worker_pid,
            "terminal_pid": self.terminal_pid,
            "last_error": self.last_error,
            "restart_count": self.restart_count,
            "updated_at": self.updated_at.isoformat(),
            "emit": self.emit,
        }

    def reset_assignment(self) -> None:
        self.status = SlotStatus.FREE
        self.account_id = None
        self.mt5_login = None
        self.mt5_server = None
        self.password_encrypted = None
        self.worker_pid = None
        self.terminal_pid = None
        self.last_error = None
        self.restart_count = 0
        self.emit = {}
        self.touch()
