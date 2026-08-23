"""El canal Worker → Core: reintentos, permanencia de errores y stats."""

from __future__ import annotations

import httpx
import pytest

from app.models.events import AccountSnapshotData, BridgeEvent, ConnectionStatusData
from app.worker.event_emitter import EventEmitter


def _event() -> BridgeEvent:
    return BridgeEvent.make(
        event="account.snapshot",
        account_id="acc-1",
        mt5_login=123,
        timestamp="2026-08-23T00:00:00Z",
        data={"balance": 10.0},
    )


def _emitter(handler, **kwargs) -> tuple[EventEmitter, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    emitter = EventEmitter("http://core.test/events", "k" * 16, **kwargs)
    emitter._client = httpx.Client(transport=httpx.MockTransport(_capture))
    return emitter, seen


def test_evento_aceptado_actualiza_stats() -> None:
    emitter, seen = _emitter(lambda r: httpx.Response(200, json={"ok": True}))
    assert emitter.emit(_event()) is True
    stats = emitter.stats()
    assert (stats["sent"], stats["failed"]) == (1, 0)
    assert stats["last_status"] == 200
    assert stats["last_snapshot_at"] is not None
    assert len(seen) == 1
    assert seen[0].headers["X-API-Key"] == "k" * 16


def test_401_no_se_reintenta_y_queda_registrado() -> None:
    """Clave mala = fallo permanente: reintentar solo retrasa el diagnóstico."""
    emitter, seen = _emitter(
        lambda r: httpx.Response(401, text="unauthorized"), max_attempts=3
    )
    assert emitter.emit(_event()) is False
    assert len(seen) == 1
    stats = emitter.stats()
    assert stats["failed"] == 1
    assert "401" in stats["last_error"]


def test_500_se_reintenta_hasta_agotar() -> None:
    emitter, seen = _emitter(
        lambda r: httpx.Response(500, text="boom"), max_attempts=3, backoff_sec=0
    )
    assert emitter.emit(_event()) is False
    assert len(seen) == 3


def test_error_de_red_se_reintenta_y_luego_recupera() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("sin ruta al Core", request=request)
        return httpx.Response(200, json={"ok": True})

    emitter, seen = _emitter(handler, max_attempts=3, backoff_sec=0)
    assert emitter.emit(_event()) is True
    assert len(seen) == 2
    assert emitter.stats()["sent"] == 1


def test_snapshot_viaja_en_las_dos_grafias() -> None:
    """El Core mapea los stats lea snake_case o camelCase."""
    payload = AccountSnapshotData(
        balance=100.0,
        equity=110.0,
        margin=5.0,
        free_margin=105.0,
        margin_level=2200.0,
        profit=10.0,
        currency="USD",
        leverage=100,
    ).to_payload()
    assert payload["free_margin"] == payload["freeMargin"] == 105.0
    assert payload["margin_level"] == payload["marginLevel"] == 2200.0
    assert payload["balance"] == 100.0


def test_connection_status_lleva_slot_en_las_dos_grafias() -> None:
    payload = ConnectionStatusData(status="connected", slot_id="Slot-03").to_payload()
    assert payload["slot_id"] == payload["slotId"] == "Slot-03"


@pytest.mark.parametrize("code", [408, 429])
def test_codigos_transitorios_se_reintentan(code: int) -> None:
    emitter, seen = _emitter(
        lambda r: httpx.Response(code, text="slow down"), max_attempts=2, backoff_sec=0
    )
    assert emitter.emit(_event()) is False
    assert len(seen) == 2


# --- 2xx que en realidad es un descarte -------------------------------------
# El Core responde 200 aunque tire el evento. Sin mirar el cuerpo, un canal
# roto parece sano: es exactamente el caso "conecta y no cargan los stats".

def test_200_con_cuenta_desconocida_no_cuenta_como_entregado() -> None:
    """Respuesta real del Core de producción ante un account_id que no conoce."""
    cuerpo = {
        "ok": True,
        "recibidos": 1,
        "procesados": 0,
        "detalle": [
            {"i": 0, "type": "connection.status", "ok": False, "error": "cuenta desconocida"}
        ],
    }
    emitter, seen = _emitter(lambda r: httpx.Response(200, json=cuerpo), max_attempts=3)

    assert emitter.emit(_event()) is False
    # Reintentar no arregla un account_id desconocido.
    assert len(seen) == 1

    stats = emitter.stats()
    assert stats["rejected"] == 1
    assert stats["sent"] == 0
    assert stats["failed"] == 0
    assert stats["last_ok_at"] is None
    assert "cuenta desconocida" in stats["last_error"]


def test_200_procesado_si_cuenta_como_entregado() -> None:
    cuerpo = {
        "ok": True,
        "recibidos": 1,
        "procesados": 1,
        "detalle": [{"i": 0, "type": "account.snapshot", "ok": True}],
    }
    emitter, _ = _emitter(lambda r: httpx.Response(200, json=cuerpo))

    assert emitter.emit(_event()) is True
    stats = emitter.stats()
    assert (stats["sent"], stats["rejected"]) == (1, 0)
    assert stats["last_snapshot_at"] is not None


def test_respuesta_sin_contrato_conocido_se_da_por_buena() -> None:
    """Un Core que solo responde 'OK' no debe marcarse como fallo inventado."""
    from app.worker.event_emitter import core_rejection

    assert core_rejection("OK") is None
    assert core_rejection('{"ok":true}') is None
    assert core_rejection("") is None
    assert core_rejection('{"ok":false,"error":"firma inválida"}') == "firma inválida"
    assert core_rejection('{"recibidos":3,"procesados":0}') == "recibidos=3 procesados=0"
    assert core_rejection('{"recibidos":3,"procesados":3}') is None
