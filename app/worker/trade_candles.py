"""Velas alrededor de una operación, para que el Core pueda dibujarla.

El Core guarda trades pero no tiene histórico de precios. Sacarlo de una API
externa no sirve: los precios no serían los del bróker del cliente y los
símbolos ni siquiera se llaman igual (EURUSD vs EURUSD.m). El Worker sí tiene
el terminal delante, así que las manda él.

Van en un evento aparte, `trade.candles`, y solo después de que el Core haya
aceptado el `trade.closed`: si rechaza las velas, la operación ya está a
salvo. Lo accesorio no puede poner en riesgo lo que importa.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.models.events import Candle, TradeCandlesData

#: Timeframe -> duración de su vela. Ordenado de menor a mayor.
DURACION = {
    "M1": timedelta(minutes=1),
    "M5": timedelta(minutes=5),
    "M15": timedelta(minutes=15),
    "M30": timedelta(minutes=30),
    "H1": timedelta(hours=1),
    "H4": timedelta(hours=4),
    "D1": timedelta(days=1),
}

#: Velas que debería ocupar la operación en el gráfico. Por debajo se ve como
#: una sola vela y no se aprecia nada; muy por encima no cabe en pantalla.
VELAS_OBJETIVO = 20


def elegir_timeframe(duracion: timedelta) -> str:
    """El timeframe en el que la operación ocupa ~VELAS_OBJETIVO velas.

    Un scalp de 3 minutos y un swing de tres semanas no se leen en la misma
    escala. Se coge el más fino que no desborde, así que una operación muy
    larga acaba en D1 y una instantánea en M1.
    """
    ideal = max(duracion, timedelta(0)) / VELAS_OBJETIVO
    for nombre, paso in DURACION.items():
        if paso >= ideal:
            return nombre
    return "D1"


def ventana(
    open_time: datetime, close_time: datetime, timeframe: str, count: int
) -> tuple[datetime, datetime]:
    """Rango a pedir a MT5: la operación centrada, con contexto a los lados.

    Sin margen, el trade queda pegado al borde del gráfico y no se ve de dónde
    venía el precio.
    """
    paso = DURACION[timeframe]
    dentro = max(int((close_time - open_time) / paso), 1)
    margen = max((count - dentro) // 2, 1) * paso
    return open_time - margen, close_time + margen


def velas_desde_mt5(rates: object) -> list[Candle]:
    """Convierte los rates de MT5 (numpy structured array) a Candle.

    `time` sale en segundos epoch, que es justo lo que esperan las librerías
    de gráficos; no se toca.
    """
    salida: list[Candle] = []
    for r in rates or []:
        try:
            salida.append(
                Candle(
                    time=int(r["time"]),
                    open=float(r["open"]),
                    high=float(r["high"]),
                    low=float(r["low"]),
                    close=float(r["close"]),
                )
            )
        except (KeyError, IndexError, TypeError, ValueError):
            # Una vela ilegible no puede tumbar el resto del gráfico.
            continue
    return salida


def construir(
    *,
    position_id: int,
    symbol: str,
    timeframe: str,
    rates: object,
) -> TradeCandlesData | None:
    """El payload, o None si no hubo velas que mandar."""
    candles = velas_desde_mt5(rates)
    if not candles:
        return None
    return TradeCandlesData(
        positionId=str(position_id),
        symbol=symbol,
        timeframe=timeframe,  # type: ignore[arg-type]
        candles=candles,
    )
