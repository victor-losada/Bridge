"""Emparejamiento de deals MT5 → trade.closed.

MT5 no guarda "trades" como el Core. Guarda:

- **Position**: exposición neta abierta (ticket = position_id).
- **Deal**: cada ejecución en el servidor (entrada, salida, parcial, reverse).

Regla de oro: todos los deals de un mismo `position_id` forman el ciclo de vida
de esa posición. Cuando el volumen de entrada se ha compensado con el de
salida, la posición está cerrada y podemos emitir `trade.closed`.

Constantes oficiales (MetaTrader5):
    DEAL_ENTRY_IN      = 0  apertura
    DEAL_ENTRY_OUT     = 1  cierre (total o parcial)
    DEAL_ENTRY_INOUT   = 2  reverse (cierra la posición y abre la contraria)
    DEAL_ENTRY_OUT_BY  = 3  cierre por posición opuesta (Close By)

    DEAL_TYPE_BUY      = 0
    DEAL_TYPE_SELL     = 1
    (el resto: balance, credit, charge... se ignoran)

Precios:
    openPrice  = VWAP de deals de entrada
    closePrice = VWAP de deals de salida
    profit/swap/commission = suma de TODOS los deals de la posición
      (MT5 suele poner el PnL realizado en los deals OUT)

SL/TP no viajan de forma fiable en el deal; el Worker los inyecta desde
el último snapshot de la posición abierta.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Literal, Sequence

from app.models.events import ClosedTradeData
from app.utils import VOLUME_EPS, mt5_time_to_utc, to_iso_z, volumes_equal

DEAL_ENTRY_IN = 0
DEAL_ENTRY_OUT = 1
DEAL_ENTRY_INOUT = 2
DEAL_ENTRY_OUT_BY = 3

DEAL_TYPE_BUY = 0
DEAL_TYPE_SELL = 1

# Entradas que CIERRAN exposición de la posición actual.
_EXIT_ENTRIES = frozenset({DEAL_ENTRY_OUT, DEAL_ENTRY_INOUT, DEAL_ENTRY_OUT_BY})
_TRADE_TYPES = frozenset({DEAL_TYPE_BUY, DEAL_TYPE_SELL})


@dataclass(slots=True)
class RawDeal:
    """Deal ya normalizado; independiente del objeto nativo de MetaTrader5."""

    ticket: int
    position_id: int
    symbol: str
    deal_type: int
    entry: int
    volume: float
    price: float
    profit: float
    swap: float
    commission: float
    time: datetime
    order: int = 0
    sl: float = 0.0
    tp: float = 0.0
    magic: int = 0
    comment: str = ""

    @property
    def is_market_deal(self) -> bool:
        return self.deal_type in _TRADE_TYPES and self.volume > 0

    @property
    def is_entry(self) -> bool:
        return self.entry == DEAL_ENTRY_IN

    @property
    def is_exit(self) -> bool:
        return self.entry in _EXIT_ENTRIES


@dataclass(slots=True)
class MatchedTrade:
    """Resultado interno antes de serializar al evento."""

    position_id: int
    last_out_ticket: int
    symbol: str
    direction: Literal["buy", "sell"]
    volume: float
    open_price: float
    close_price: float
    open_time: datetime
    close_time: datetime
    profit: float
    swap: float
    commission: float
    stop_loss: float | None = None
    take_profit: float | None = None
    initial_stop_loss: float | None = None
    initial_take_profit: float | None = None
    in_deals: list[RawDeal] = field(default_factory=list)
    out_deals: list[RawDeal] = field(default_factory=list)

    def to_event_data(self) -> ClosedTradeData:
        return ClosedTradeData(
            positionId=str(self.position_id),
            dealId=str(self.last_out_ticket),
            symbol=self.symbol,
            type=self.direction,
            volume=round(self.volume, 8),
            openPrice=_r(self.open_price),
            closePrice=_r(self.close_price),
            openTime=to_iso_z(self.open_time),
            closeTime=to_iso_z(self.close_time),
            profit=round(self.profit, 2),
            swap=round(self.swap, 2),
            commission=round(self.commission, 2),
            stopLoss=_opt_price(self.stop_loss),
            takeProfit=_opt_price(self.take_profit),
            initialStopLoss=_opt_price(self.initial_stop_loss),
            initialTakeProfit=_opt_price(self.initial_take_profit),
        )


@dataclass(slots=True)
class RawOrder:
    """La orden que abrió una posición, con el SL/TP que llevaba entonces.

    MT5 guarda el stop de forma fiable en la ORDEN, no en el deal: el campo
    `sl` de un deal viene en 0 con muchos brókers. Y como modificar el stop de
    una posición viva no genera ninguna orden nueva, lo que la orden de
    apertura lleva es el stop INICIAL, que es justamente el que sirve para
    calcular la R de la operación.
    """

    ticket: int
    position_id: int
    sl: float = 0.0
    tp: float = 0.0


def order_from_mt5(order: object) -> RawOrder | None:
    """Adapta un TradeOrder de MetaTrader5 (namedtuple) a RawOrder."""
    ticket = int(getattr(order, "ticket", 0) or 0)
    if ticket <= 0:
        return None
    return RawOrder(
        ticket=ticket,
        position_id=int(getattr(order, "position_id", 0) or 0),
        sl=float(getattr(order, "sl", 0) or 0),
        tp=float(getattr(order, "tp", 0) or 0),
    )


def orders_by_ticket(orders: Iterable[RawOrder]) -> dict[int, RawOrder]:
    return {o.ticket: o for o in orders}


def deal_from_mt5(deal: object) -> RawDeal | None:
    """Adapta un TradeDeal de MetaTrader5 (namedtuple) a RawDeal."""
    ticket = int(getattr(deal, "ticket", 0) or 0)
    position_id = int(getattr(deal, "position_id", 0) or 0)
    deal_type = int(getattr(deal, "type", -1))
    entry = int(getattr(deal, "entry", -1))
    volume = float(getattr(deal, "volume", 0) or 0)
    if ticket <= 0 or position_id <= 0 or deal_type not in _TRADE_TYPES:
        return None
    time_raw = getattr(deal, "time_msc", None) or getattr(deal, "time", None)
    return RawDeal(
        ticket=ticket,
        position_id=position_id,
        symbol=str(getattr(deal, "symbol", "") or ""),
        deal_type=deal_type,
        entry=entry,
        volume=volume,
        price=float(getattr(deal, "price", 0) or 0),
        profit=float(getattr(deal, "profit", 0) or 0),
        swap=float(getattr(deal, "swap", 0) or 0),
        commission=float(getattr(deal, "commission", 0) or 0),
        time=mt5_time_to_utc(time_raw),
        order=int(getattr(deal, "order", 0) or 0),
        sl=float(getattr(deal, "sl", 0) or 0),
        tp=float(getattr(deal, "tp", 0) or 0),
        magic=int(getattr(deal, "magic", 0) or 0),
        comment=str(getattr(deal, "comment", "") or ""),
    )


def group_deals_by_position(deals: Iterable[RawDeal]) -> dict[int, list[RawDeal]]:
    grouped: dict[int, list[RawDeal]] = {}
    for deal in deals:
        if not deal.is_market_deal:
            continue
        grouped.setdefault(deal.position_id, []).append(deal)
    for pid in grouped:
        grouped[pid].sort(key=lambda d: (d.time, d.ticket))
    return grouped


def net_volumes(deals: Sequence[RawDeal]) -> tuple[float, float]:
    """Volumen de entrada vs salida de una posición."""
    in_vol = sum(d.volume for d in deals if d.is_entry)
    out_vol = sum(d.volume for d in deals if d.is_exit)
    return in_vol, out_vol


def is_position_fully_closed(deals: Sequence[RawDeal]) -> bool:
    """True si hay al menos una entrada y el volumen OUT cubre el IN.

    Hedging vs netting: en ambos modos MT5 agrupa por position_id.
    Un cierre parcial deja in_vol > out_vol → aún no emitimos trade.closed.
    """
    in_vol, out_vol = net_volumes(deals)
    if in_vol <= VOLUME_EPS or out_vol <= VOLUME_EPS:
        return False
    return out_vol + VOLUME_EPS >= in_vol


def vwap(deals: Sequence[RawDeal]) -> float:
    total_vol = sum(d.volume for d in deals)
    if total_vol <= VOLUME_EPS:
        return 0.0
    return sum(d.price * d.volume for d in deals) / total_vol


def _primero(*valores: float | None) -> float | None:
    """El primer valor utilizable. Un 0.0 en MT5 significa 'sin definir'."""
    for v in valores:
        if v not in (None, 0.0):
            return v
    return None


def match_closed_position(
    deals: Sequence[RawDeal],
    *,
    stop_loss: float | None = None,
    take_profit: float | None = None,
    orders: dict[int, RawOrder] | None = None,
) -> MatchedTrade | None:
    """Construye un MatchedTrade si los deals cierran por completo la posición.

    Dirección: tipo del primer deal de ENTRADA (buy=largo, sell=corto).
    Los deals OUT tienen el tipo contrario; no se usa para `type`.
    """
    market = [d for d in deals if d.is_market_deal]
    if not market or not is_position_fully_closed(market):
        return None

    entries = [d for d in market if d.is_entry]
    exits = [d for d in market if d.is_exit]
    if not entries or not exits:
        return None

    first_in = entries[0]
    last_out = max(exits, key=lambda d: (d.time, d.ticket))
    direction: Literal["buy", "sell"] = "buy" if first_in.deal_type == DEAL_TYPE_BUY else "sell"
    in_vol, _out_vol = net_volumes(market)

    # El stop tal como estaba al ABRIR: la orden de entrada lo conserva aunque
    # el trade sea historico y nadie sondeara la posicion mientras vivia.
    apertura = (orders or {}).get(first_in.order)
    sl_inicial = _primero(apertura.sl if apertura else None)
    tp_inicial = _primero(apertura.tp if apertura else None)

    # El ultimo conocido: lo visto en el libro de posiciones manda, porque
    # recoge las modificaciones posteriores. Si no lo hay, se cae hacia el
    # deal y, por ultimo, hacia la orden.
    sl = _primero(stop_loss, first_in.sl, sl_inicial)
    tp = _primero(take_profit, first_in.tp, tp_inicial)

    return MatchedTrade(
        position_id=first_in.position_id,
        last_out_ticket=last_out.ticket,
        symbol=first_in.symbol or last_out.symbol,
        direction=direction,
        volume=in_vol,
        open_price=vwap(entries),
        close_price=vwap(exits),
        open_time=min(d.time for d in entries),
        close_time=max(d.time for d in exits),
        profit=sum(d.profit for d in market),
        swap=sum(d.swap for d in market),
        commission=sum(d.commission for d in market),
        stop_loss=sl,
        take_profit=tp,
        initial_stop_loss=sl_inicial,
        initial_take_profit=tp_inicial,
        in_deals=entries,
        out_deals=exits,
    )


class DealMatcher:
    """Estado incremental: deals vistos + posiciones ya emitidas como trade.closed.

    El Worker alimenta `ingest()` en cada poll de historial. `pop_ready_trades()`
    devuelve solo ciclos NUEVOS y completos.
    """

    def __init__(self) -> None:
        self._by_position: dict[int, dict[int, RawDeal]] = {}
        self._emitted: set[int] = set()
        self._known_tickets: set[int] = set()

    @property
    def emitted_position_ids(self) -> set[int]:
        return set(self._emitted)

    def mark_emitted(self, position_ids: Iterable[int]) -> None:
        """Marca historial previo como ya procesado (anti-replay al conectar)."""
        for pid in position_ids:
            self._emitted.add(int(pid))

    def unmark_emitted(self, position_ids: Iterable[int]) -> None:
        """Deshace la marca para que el trade se reintente.

        `pop_ready_trades` marca como emitido antes de que el Worker envíe:
        si el Core rechaza el evento, sin esto el trade se perdería para
        siempre porque nunca volvería a salir del matcher.
        """
        for pid in position_ids:
            self._emitted.discard(int(pid))

    def ingest(self, deals: Iterable[RawDeal]) -> list[RawDeal]:
        """Añade deals nuevos. Devuelve los que no se habían visto."""
        fresh: list[RawDeal] = []
        for deal in deals:
            if not deal.is_market_deal:
                continue
            if deal.ticket in self._known_tickets:
                continue
            self._known_tickets.add(deal.ticket)
            bucket = self._by_position.setdefault(deal.position_id, {})
            bucket[deal.ticket] = deal
            fresh.append(deal)
        return fresh

    def pop_ready_trades(
        self,
        *,
        sl_tp_by_position: dict[int, tuple[float | None, float | None]] | None = None,
        orders: dict[int, RawOrder] | None = None,
        only_position_id: int | None = None,
    ) -> list[MatchedTrade]:
        """Devuelve trades cerrados aún no emitidos y los marca como emitidos."""
        sl_tp_by_position = sl_tp_by_position or {}
        ready: list[MatchedTrade] = []
        targets = (
            [only_position_id]
            if only_position_id is not None
            else list(self._by_position.keys())
        )
        for pid in targets:
            if pid is None or pid in self._emitted:
                continue
            deals = list(self._by_position.get(pid, {}).values())
            sl, tp = sl_tp_by_position.get(pid, (None, None))
            matched = match_closed_position(
                deals, stop_loss=sl, take_profit=tp, orders=orders
            )
            if matched is None:
                continue
            self._emitted.add(pid)
            ready.append(matched)
        ready.sort(key=lambda t: t.close_time)
        return ready

    def snapshot_closed_ids(self) -> list[int]:
        """position_id que YA están cerrados en el buffer (para seed inicial)."""
        closed: list[int] = []
        for pid, bucket in self._by_position.items():
            if is_position_fully_closed(list(bucket.values())):
                closed.append(pid)
        return closed

    def dump_persist(self) -> dict:
        return {
            "emitted": sorted(self._emitted),
            "tickets": sorted(self._known_tickets),
        }

    def load_persist(self, payload: dict) -> None:
        self._emitted.update(int(x) for x in payload.get("emitted", []))
        self._known_tickets.update(int(x) for x in payload.get("tickets", []))


def _r(value: float) -> float:
    return round(float(value), 8)


def _opt_price(value: float | None) -> float | None:
    if value is None:
        return None
    if abs(float(value)) < VOLUME_EPS:
        return None
    return round(float(value), 8)
