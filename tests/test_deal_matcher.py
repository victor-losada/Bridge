"""Pruebas unitarias del emparejamiento de deals (sin MT5)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.worker.deal_matcher import (
    DEAL_ENTRY_IN,
    DEAL_ENTRY_OUT,
    DEAL_TYPE_BUY,
    DEAL_TYPE_SELL,
    DealMatcher,
    RawDeal,
    RawOrder,
    is_position_fully_closed,
    match_closed_position,
    order_from_mt5,
)


def _t(minutes: int) -> datetime:
    return datetime(2026, 8, 21, 14, 0, tzinfo=timezone.utc) + timedelta(minutes=minutes)


def _deal(**kwargs) -> RawDeal:
    base = dict(
        ticket=1,
        position_id=987654321,
        symbol="EURUSD",
        deal_type=DEAL_TYPE_BUY,
        entry=DEAL_ENTRY_IN,
        volume=0.10,
        price=1.08523,
        profit=0.0,
        swap=0.0,
        commission=-0.35,
        time=_t(0),
    )
    base.update(kwargs)
    return RawDeal(**base)  # type: ignore[arg-type]


def test_round_trip_buy():
    deals = [
        _deal(ticket=11, entry=DEAL_ENTRY_IN, deal_type=DEAL_TYPE_BUY, price=1.08523, commission=-0.35),
        _deal(
            ticket=22,
            entry=DEAL_ENTRY_OUT,
            deal_type=DEAL_TYPE_SELL,
            price=1.08710,
            profit=18.70,
            commission=-0.35,
            time=_t(60),
        ),
    ]
    assert is_position_fully_closed(deals)
    trade = match_closed_position(deals, stop_loss=1.08, take_profit=1.092)
    assert trade is not None
    data = trade.to_event_data()
    assert data.positionId == "987654321"
    assert data.dealId == "22"
    assert data.type == "buy"
    assert data.volume == 0.10
    assert data.openPrice == 1.08523
    assert data.closePrice == 1.08710
    assert data.profit == 18.7
    assert data.commission == -0.70
    assert data.stopLoss == 1.08


def test_partial_close_is_not_trade_closed():
    deals = [
        _deal(ticket=1, volume=0.20, entry=DEAL_ENTRY_IN),
        _deal(
            ticket=2,
            volume=0.10,
            entry=DEAL_ENTRY_OUT,
            deal_type=DEAL_TYPE_SELL,
            profit=5.0,
            time=_t(10),
        ),
    ]
    assert not is_position_fully_closed(deals)
    assert match_closed_position(deals) is None


def test_matcher_emits_once():
    matcher = DealMatcher()
    in_deal = _deal(ticket=1)
    out_deal = _deal(
        ticket=2,
        entry=DEAL_ENTRY_OUT,
        deal_type=DEAL_TYPE_SELL,
        price=1.08710,
        profit=10.0,
        time=_t(5),
    )
    matcher.ingest([in_deal, out_deal])
    first = matcher.pop_ready_trades()
    second = matcher.pop_ready_trades()
    assert len(first) == 1
    assert second == []


def test_seed_prevents_replay():
    matcher = DealMatcher()
    matcher.ingest(
        [
            _deal(ticket=1),
            _deal(
                ticket=2,
                entry=DEAL_ENTRY_OUT,
                deal_type=DEAL_TYPE_SELL,
                profit=1.0,
                time=_t(3),
            ),
        ]
    )
    matcher.mark_emitted(matcher.snapshot_closed_ids())
    assert matcher.pop_ready_trades() == []


# --- SL/TP de apertura ------------------------------------------------------
#
# El caso real que motiva esto: un trade cerrado hace semanas se reconstruye
# desde el historial. Nadie sondeó su posición mientras vivía, y el bróker
# dejó el `sl` del deal en 0. Sin mirar la orden, ese trade llega al Core sin
# stop y no se puede calcular su R.


def _orden(ticket: int, *, sl: float = 0.0, tp: float = 0.0) -> RawOrder:
    return RawOrder(ticket=ticket, position_id=987654321, sl=sl, tp=tp)


def _cerrado(**kwargs):
    """Un round trip mínimo: entra y sale."""
    deals = [
        _deal(ticket=11, entry=DEAL_ENTRY_IN, price=1.08000, order=500, **kwargs),
        _deal(
            ticket=22,
            entry=DEAL_ENTRY_OUT,
            deal_type=DEAL_TYPE_SELL,
            price=1.08400,
            time=_t(30),
            order=501,
        ),
    ]
    return deals


def test_el_stop_sale_de_la_orden_cuando_el_deal_no_lo_trae():
    deals = _cerrado()  # deal con sl=0.0, como lo deja la mayoría de brókers

    trade = match_closed_position(deals, orders={500: _orden(500, sl=1.07800, tp=1.08600)})

    assert trade is not None
    assert trade.initial_stop_loss == 1.07800
    assert trade.initial_take_profit == 1.08600
    # Y a falta de algo más reciente, también sirve como último conocido.
    assert trade.stop_loss == 1.07800


def test_el_stop_movido_no_pisa_al_inicial():
    """Mover el stop a break-even no puede alterar el riesgo asumido al entrar.

    Si lo pisara, la R saldría infinita: riesgo cero.
    """
    deals = _cerrado()

    trade = match_closed_position(
        deals,
        stop_loss=1.08000,  # movido a la entrada
        orders={500: _orden(500, sl=1.07800)},
    )

    assert trade is not None
    assert trade.stop_loss == 1.08000
    assert trade.initial_stop_loss == 1.07800


def test_sin_ordenes_el_trade_sale_igual():
    """Las órdenes son una mejora, no un requisito: nunca deben bloquear."""
    deals = _cerrado()

    trade = match_closed_position(deals)

    assert trade is not None
    assert trade.initial_stop_loss is None
    assert trade.to_event_data().initialStopLoss is None


def test_un_cero_de_mt5_no_es_un_stop():
    """MT5 usa 0.0 para 'sin definir'. Un 0.0 en el payload sería un precio."""
    deals = _cerrado()

    trade = match_closed_position(deals, orders={500: _orden(500, sl=0.0, tp=0.0)})

    assert trade is not None
    assert trade.initial_stop_loss is None
    assert trade.stop_loss is None


def test_el_payload_lleva_los_dos_stops():
    deals = _cerrado()

    trade = match_closed_position(
        deals, stop_loss=1.08000, orders={500: _orden(500, sl=1.07800, tp=1.08600)}
    )

    assert trade is not None
    data = trade.to_event_data()
    assert data.stopLoss == 1.08000
    assert data.initialStopLoss == 1.07800
    assert data.initialTakeProfit == 1.08600


def test_order_from_mt5_ignora_una_orden_sin_ticket():
    class _Falsa:
        ticket = 0
        position_id = 1
        sl = 1.0
        tp = 2.0

    assert order_from_mt5(_Falsa()) is None
