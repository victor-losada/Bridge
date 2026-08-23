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
    is_position_fully_closed,
    match_closed_position,
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
