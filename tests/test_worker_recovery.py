"""Reconexión del Worker y ventana de historial.

Los dos fallos que dejaban a una cuenta "conectada" sin datos:
  - el password se borraba tras el primer login → ningún re-login funcionaba,
  - la ventana de historial se cortaba en UTC → los cierres recientes de un
    bróker en hora adelantada nunca se emitían.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.worker import mt5_client as mt5_module
from app.worker.mt5_client import MT5Client
from app.worker.worker import Worker


class _FakeEmitter:
    url = "http://core.test/events"

    def __init__(self) -> None:
        self.events: list[str] = []

    def emit(self, event) -> bool:  # noqa: ANN001
        self.events.append(event.event)
        return True

    def stats(self) -> dict:
        return {"sent": len(self.events)}

    def close(self) -> None:
        pass


class _FakeClient:
    def __init__(self, fail_first: bool = False) -> None:
        self.passwords: list[str] = []
        self.fail_first = fail_first

    def initialize_and_login(self, login: int, password: str, server: str) -> None:
        self.passwords.append(password)
        if self.fail_first and len(self.passwords) == 1:
            raise RuntimeError("IPC timeout")

    def shutdown(self) -> None:
        pass


def _worker(tmp_path: Path, client: _FakeClient) -> Worker:
    worker = Worker(
        slot_id="Slot-01",
        account_id="acc-1",
        mt5_login=999,
        mt5_password="investor-secreta",
        mt5_server="Broker-Server",
        terminal_path=tmp_path,
        core_events_url="http://core.test/events",
        core_api_key="k" * 8,
        login_retry_max=2,
        login_retry_backoff_sec=0.0,
    )
    worker.client = client  # type: ignore[assignment]
    worker.emitter = _FakeEmitter()  # type: ignore[assignment]
    return worker


def test_reconexion_reutiliza_el_password(tmp_path: Path) -> None:
    """El segundo login (reconexión tras caída de MT5) debe llevar el password."""
    client = _FakeClient()
    worker = _worker(tmp_path, client)

    assert worker._login_with_retries() is True
    assert worker._login_with_retries() is True

    assert client.passwords == ["investor-secreta", "investor-secreta"]


def test_reintento_de_login_conserva_el_password(tmp_path: Path) -> None:
    client = _FakeClient(fail_first=True)
    worker = _worker(tmp_path, client)

    assert worker._login_with_retries() is True
    assert client.passwords == ["investor-secreta", "investor-secreta"]


def test_runtime_json_publica_el_estado_del_canal(tmp_path: Path) -> None:
    import json

    worker = _worker(tmp_path, _FakeClient())
    worker._write_runtime("connected")

    payload = json.loads((tmp_path / "slot_runtime.json").read_text(encoding="utf-8"))
    assert payload["status"] == "connected"
    assert payload["core_events_url"] == "http://core.test/events"
    assert payload["emit"] == {"sent": 0}


def test_ventana_de_historial_cubre_la_hora_del_broker(monkeypatch, tmp_path: Path) -> None:
    """El tope va por delante de UTC: el bróker marca los deals en su propia hora."""
    capturado: dict[str, datetime] = {}

    class _FakeMT5:
        @staticmethod
        def history_deals_get(date_from: datetime, date_to: datetime):
            capturado["from"] = date_from
            capturado["to"] = date_to
            return ()

    monkeypatch.setattr(mt5_module, "mt5", _FakeMT5)
    client = MT5Client(tmp_path, history_forward_buffer_hours=24.0)
    client._connected = True

    assert client.history_deals(lookback_days=7) == ()

    ahora = datetime.now(timezone.utc)
    # Un bróker en EET (UTC+3) marca "ahora" hasta 3h por delante de UTC.
    assert capturado["to"] > ahora + timedelta(hours=3)
    assert capturado["from"] < ahora - timedelta(days=7)


def test_buffer_cero_reproduce_el_fallo_original(monkeypatch, tmp_path: Path) -> None:
    capturado: dict[str, datetime] = {}

    class _FakeMT5:
        @staticmethod
        def history_deals_get(date_from: datetime, date_to: datetime):
            capturado["to"] = date_to
            return ()

    monkeypatch.setattr(mt5_module, "mt5", _FakeMT5)
    client = MT5Client(tmp_path, history_forward_buffer_hours=0.0)
    client._connected = True
    client.history_deals(lookback_days=7)

    ahora = datetime.now(timezone.utc)
    assert capturado["to"] <= ahora + timedelta(seconds=1)


def test_error_del_canal_al_core_se_marca_y_se_limpia() -> None:
    """El Manager refleja el fallo de entrega y lo retira al recuperarse."""
    from app.manager.slot_manager import _core_channel_error

    fallando = {
        "last_error": "HTTP 401: unauthorized",
        "last_error_at": "2026-08-23T10:00:05Z",
        "last_ok_at": None,
    }
    marcado = _core_channel_error(fallando, None)
    assert marcado is not None and "401" in marcado

    recuperado = {
        "last_error": "HTTP 401: unauthorized",
        "last_error_at": "2026-08-23T10:00:05Z",
        "last_ok_at": "2026-08-23T10:00:30Z",
    }
    assert _core_channel_error(recuperado, marcado) is None

    # Un error ajeno al canal (p.ej. login MT5) no se pisa.
    assert _core_channel_error(recuperado, "mt5.login falló") == "mt5.login falló"
