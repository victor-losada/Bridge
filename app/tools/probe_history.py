"""Qué trae MT5 realmente en deals y órdenes, para un slot y una cuenta.

Nace de un caso concreto: los trades llegaban al Core con stopLoss en null y
había tres explicaciones posibles —la ventana de historial, el campo `sl` del
deal, o el emparejamiento deal→orden—. Adivinar entre ellas sale caro; esto
las separa.

Imprime, para las últimas posiciones cerradas: el ticket de la orden que las
abrió, si esa orden aparece en el historial, y qué SL/TP trae cada fuente.

Ejecutar con el Worker de ese slot PARADO (desconecta la cuenta primero), o
los dos procesos se pelean por el mismo terminal.

    python -m app.tools.probe_history --slot Slot-01 --login 203395 ^
        --password "..." --server "NYSMarketsLtd-trade" --days 30
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import MetaTrader5 as mt5  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - solo Windows
    mt5 = None  # type: ignore[assignment]

DEAL_ENTRY_IN = 0
_TRADE_TYPES = {0, 1}


def _line(titulo: str) -> None:
    print(f"\n--- {titulo} " + "-" * max(0, 62 - len(titulo)))


def _p(valor: object) -> str:
    """Un 0 de MT5 significa 'sin definir'; que se vea distinto de un precio."""
    try:
        f = float(valor)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "?"
    return "(sin definir)" if abs(f) < 1e-9 else f"{f:.5f}"


def sondear(
    exe: Path, *, login: int, password: str, server: str, days: int, limite: int
) -> int:
    if mt5 is None:
        print("El paquete MetaTrader5 no está instalado (solo Windows).")
        return 2

    print(f"Terminal : {exe}")
    print(f"Cuenta   : {login} en {server}")
    print(f"Ventana  : {days} días")

    if not mt5.initialize(
        path=str(exe),
        login=int(login),
        password=password,
        server=server,
        timeout=180_000,
        portable=True,
    ):
        print(f"\ninitialize falló: {mt5.last_error()}")
        return 1

    try:
        # Misma holgura que usa el Worker: MT5 filtra por hora del servidor
        # del bróker, no por UTC.
        ahora = datetime.now(timezone.utc)
        desde = ahora - timedelta(days=days) - timedelta(hours=24)
        hasta = ahora + timedelta(hours=24)

        _line("1. Qué devuelve MT5")
        deals = mt5.history_deals_get(desde, hasta)
        orders = mt5.history_orders_get(desde, hasta)
        print(f"history_deals_get  -> {len(deals or [])} deals   {mt5.last_error()}")
        print(f"history_orders_get -> {len(orders or [])} órdenes {mt5.last_error()}")

        if orders is None or len(orders) == 0:
            print(
                "\nSin órdenes no hay stop que recuperar. Si hay deals pero no\n"
                "órdenes, el problema es de la llamada o del bróker, no del\n"
                "emparejamiento."
            )
            return 0

        _line("2. Campos de la primera orden")
        primera = orders[0]
        for campo in ("ticket", "position_id", "sl", "tp", "state", "reason", "symbol"):
            print(f"  {campo:12} = {getattr(primera, campo, '(no existe)')}")

        por_ticket = {int(o.ticket): o for o in orders}
        por_posicion: dict[int, object] = {}
        for o in orders:
            pid = int(getattr(o, "position_id", 0) or 0)
            if pid and pid not in por_posicion:
                por_posicion[pid] = o

        entradas: dict[int, object] = {}
        for d in deals or []:
            if int(getattr(d, "entry", -1)) != DEAL_ENTRY_IN:
                continue
            if int(getattr(d, "type", -1)) not in _TRADE_TYPES:
                continue
            pid = int(getattr(d, "position_id", 0) or 0)
            if pid and pid not in entradas:
                entradas[pid] = d

        _line(f"3. Últimas {limite} posiciones: de dónde saldría el stop")
        print(
            f"{'posición':>12} {'orden':>10} {'¿está?':>7} "
            f"{'sl(deal)':>14} {'sl(orden)':>14} {'tp(orden)':>14}"
        )
        con_stop = 0
        for pid in list(entradas)[-limite:]:
            d = entradas[pid]
            ticket = int(getattr(d, "order", 0) or 0)
            o = por_ticket.get(ticket) or por_posicion.get(pid)
            marca = "sí" if o is not None else "NO"
            sl_o = _p(getattr(o, "sl", 0)) if o is not None else "-"
            tp_o = _p(getattr(o, "tp", 0)) if o is not None else "-"
            if o is not None and abs(float(getattr(o, "sl", 0) or 0)) > 1e-9:
                con_stop += 1
            print(
                f"{pid:>12} {ticket:>10} {marca:>7} "
                f"{_p(getattr(d, 'sl', 0)):>14} {sl_o:>14} {tp_o:>14}"
            )

        _line("Lectura")
        total = len(list(entradas)[-limite:])
        print(f"Posiciones miradas          : {total}")
        print(f"Con stop en la orden        : {con_stop}")
        if total and con_stop == 0:
            print(
                "\nNinguna orden trae stop. O el trader lo ponía DESPUÉS de\n"
                "abrir (y entonces no hay nada que recuperar), o este bróker\n"
                "no lo guarda en la orden."
            )
        return 0
    finally:
        mt5.shutdown()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Qué trae MT5 en deals y órdenes.")
    parser.add_argument("--slot", default="Slot-01")
    parser.add_argument("--terminals-root", type=Path, default=Path("terminals"))
    parser.add_argument("--login", type=int, required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--server", required=True)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--limite", type=int, default=15)
    args = parser.parse_args(argv)

    exe = args.terminals_root / args.slot / "terminal64.exe"
    if not exe.is_file():
        parser.error(f"no existe {exe}")
    return sondear(
        exe,
        login=args.login,
        password=args.password,
        server=args.server,
        days=args.days,
        limite=args.limite,
    )


if __name__ == "__main__":
    sys.exit(main())
