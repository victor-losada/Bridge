"""Configuración del Bridge a partir de variables de entorno."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Manager HTTP
    bridge_host: str = "0.0.0.0"
    bridge_port: int = 8088
    bridge_api_key: str = Field(..., min_length=16)

    # Core
    core_events_url: str = "http://localhost:8000/internal/bridge/events"
    core_api_key: str = Field(..., min_length=8)
    # Reintentos del POST de eventos al Core (por evento).
    core_retry_max: int = 3
    # True = usar HTTP(S)_PROXY del sistema para llegar al Core.
    core_http_trust_env: bool = False

    # Cifrado en reposo (Fernet.urlsafe_b64)
    fernet_key: str = Field(..., min_length=32)

    # Pool
    slot_count: int = 15
    terminals_root: Path = Path("./terminals")
    data_dir: Path = Path("./data")

    # Polling (segundos)
    poll_account_sec: float = 9.0
    poll_positions_sec: float = 6.0
    poll_history_sec: float = 7.0
    history_lookback_days: int = 7
    # Holgura del tope de la ventana de historial: MT5 filtra por hora del
    # servidor del bróker, no por UTC.
    history_forward_buffer_hours: float = 24.0

    # Si True, al conectar se reenvían trade.closed del lookback.
    # Por defecto False: se marcan como ya vistos para no inundar al Core.
    replay_history_on_connect: bool = False

    # Lifecycle
    worker_restart_max: int = 5
    worker_restart_backoff_sec: float = 5.0
    login_retry_max: int = 4
    login_retry_backoff_sec: float = 8.0
    shutdown_timeout_sec: float = 12.0
    mt5_init_timeout_ms: int = 60_000

    @field_validator("terminals_root", "data_dir", mode="before")
    @classmethod
    def _as_path(cls, value: str | Path) -> Path:
        return Path(value).resolve()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
