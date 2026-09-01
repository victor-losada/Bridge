"""Velas de una operación: escala, encuadre y tolerancia a datos malos."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.worker.trade_candles import (
    DURACION,
    construir,
    elegir_timeframe,
    velas_desde_mt5,
    ventana,
)


def _t(minutes: float) -> datetime:
    return datetime(2026, 8, 21, 14, 0, tzinfo=timezone.utc) + timedelta(minutes=minutes)


def test_la_escala_se_adapta_a_lo_que_duro_la_operacion():
    """Un scalp y un swing no se leen en el mismo timeframe."""
    assert elegir_timeframe(timedelta(minutes=4)) == "M1"
    assert elegir_timeframe(timedelta(hours=2)) == "M15"
    assert elegir_timeframe(timedelta(days=3)) == "H4"
    assert elegir_timeframe(timedelta(days=90)) == "D1"


def test_una_operacion_larguisima_no_se_sale_de_la_tabla():
    assert elegir_timeframe(timedelta(days=3650)) == "D1"


def test_una_operacion_instantanea_no_rompe():
    """Cerrar en el mismo segundo que se abre es posible (scalp, o un stop)."""
    assert elegir_timeframe(timedelta(0)) == "M1"


def test_la_ventana_deja_contexto_a_los_lados():
    """Pegado al borde no se ve de dónde venía el precio."""
    desde, hasta = ventana(_t(0), _t(30), "M1", count=150)

    assert desde < _t(0)
    assert hasta > _t(30)
    # El trade queda aproximadamente centrado.
    antes = _t(0) - desde
    despues = hasta - _t(30)
    assert abs(antes - despues) < DURACION["M1"] * 2


def test_la_ventana_da_margen_aunque_el_trade_llene_el_grafico():
    """Si la operación ya ocupa más velas que las pedidas, margen mínimo."""
    desde, hasta = ventana(_t(0), _t(600), "M1", count=10)

    assert desde < _t(0)
    assert hasta > _t(600)


def test_una_vela_ilegible_no_tumba_el_resto():
    rates = [
        {"time": 1000, "open": 1.1, "high": 1.2, "low": 1.0, "close": 1.15},
        {"time": 1060, "open": "no es un numero"},
        {"time": 1120, "open": 1.15, "high": 1.25, "low": 1.1, "close": 1.2},
    ]

    velas = velas_desde_mt5(rates)

    assert [v.time for v in velas] == [1000, 1120]


def test_sin_velas_no_se_manda_nada():
    """Un símbolo retirado del bróker no devuelve histórico: no hay evento."""
    assert construir(position_id=1, symbol="EURUSD", timeframe="M5", rates=[]) is None
    assert construir(position_id=1, symbol="EURUSD", timeframe="M5", rates=None) is None


def test_el_payload_lleva_lo_que_espera_un_grafico():
    rates = [{"time": 1000, "open": 1.1, "high": 1.2, "low": 1.0, "close": 1.15}]

    data = construir(position_id=42, symbol="EURUSD", timeframe="M5", rates=rates)

    assert data is not None
    assert data.positionId == "42"
    assert data.timeframe == "M5"
    # Segundos epoch, sin convertir: es lo que esperan las librerías.
    assert data.candles[0].time == 1000
    assert data.candles[0].high == 1.2


class _ArrayComoNumpy(list):
    """Se comporta como un array de numpy en lo que importa aquí.

    numpy prohíbe evaluar la verdad de un array de varios elementos, así que
    `rates or []` lanza ValueError en producción. Una lista normal no lo
    reproduce: por eso las primeras pruebas pasaban con el fallo dentro.
    """

    def __bool__(self) -> bool:
        raise ValueError(
            "The truth value of an array with more than one element is ambiguous"
        )


def test_un_array_de_numpy_no_se_evalua_como_booleano():
    rates = _ArrayComoNumpy(
        [{"time": 1000, "open": 1.1, "high": 1.2, "low": 1.0, "close": 1.15}]
    )

    velas = velas_desde_mt5(rates)

    assert [v.time for v in velas] == [1000]


def test_construir_con_un_array_de_numpy():
    rates = _ArrayComoNumpy(
        [{"time": 1000, "open": 1.1, "high": 1.2, "low": 1.0, "close": 1.15}]
    )

    data = construir(position_id=7, symbol="EURUSD", timeframe="M5", rates=rates)

    assert data is not None
    assert len(data.candles) == 1


def test_un_array_vacio_de_numpy_no_manda_nada():
    assert construir(
        position_id=7, symbol="EURUSD", timeframe="M5", rates=_ArrayComoNumpy()
    ) is None
