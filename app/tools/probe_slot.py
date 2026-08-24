"""Sonda el terminal de un slot sin meter al Bridge de por medio.

Aísla si el problema es del terminal/bróker o del Worker. Ejecutar con el
Manager PARADO: dos procesos peleándose por el mismo terminal dan justo el
IPC timeout que se intenta diagnosticar.

    python -m app.tools.probe_slot --slot Slot-02
    python -m app.tools.probe_slot --slot Slot-02 --login 203395 ^
        --password "..." --server "NYSMarketsLtd-trade"

Sin credenciales solo engancha el IPC y dice qué cuenta hay dentro.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

try:
    import MetaTrader5 as mt5  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - solo Windows
    mt5 = None  # type: ignore[assignment]


def _line(titulo: str) -> None:
    print(f"\n--- {titulo} " + "-" * max(0, 60 - len(titulo)))


def probe(
    exe: Path,
    *,
    login: int | None,
    password: str,
    server: str,
    init_timeout_ms: int,
    login_timeout_ms: int,
) -> int:
    if mt5 is None:
        print("El paquete MetaTrader5 no está instalado (solo funciona en Windows).")
        return 2
    if not exe.is_file():
        print(f"No existe {exe}")
        return 2

    print(f"Paquete MetaTrader5 : {mt5.__version__}")
    print(f"Terminal            : {exe}")

    _line("1. initialize (engancha el IPC)")
    ok = mt5.initialize(path=str(exe), timeout=init_timeout_ms, portable=True)
    print(f"initialize portable=True -> {ok}  {mt5.last_error()}")
    if not ok:
        ok = mt5.initialize(path=str(exe), timeout=init_timeout_ms, portable=False)
        print(f"initialize portable=False -> {ok}  {mt5.last_error()}")
    if not ok:
        print(
            "\nEl terminal no atiende. Casi siempre es que está esperando algo en\n"
            "pantalla (asistente de cuenta, aviso de servidor). Ábrelo a mano con\n"
            f'   Start-Process "{exe}" -ArgumentList "/portable"\n'
            "mira qué muestra, entra en la cuenta una vez, ciérralo y repite."
        )
        return 1

    _line("2. terminal_info")
    info = mt5.terminal_info()
    if info is None:
        print(f"terminal_info vacío: {mt5.last_error()}")
        mt5.shutdown()
        return 1
    print(f"build={info.build}  connected={info.connected}  path={info.path}")

    _line("3. cuenta actualmente dentro")
    cuenta = mt5.account_info()
    if cuenta is None:
        print(f"account_info vacío: {mt5.last_error()} (sin sesión iniciada)")
    else:
        print(f"login={cuenta.login}  server={cuenta.server}  balance={cuenta.balance}")

    if login is None:
        mt5.shutdown()
        print("\nSin --login: no se intenta cambiar de cuenta.")
        return 0

    if cuenta is not None and int(cuenta.login) == int(login):
        print(f"\nYa está dentro de {login}: no hace falta login.")
        mt5.shutdown()
        return 0

    _line(f"4. login a {login} en {server}")
    print(f"(timeout {login_timeout_ms} ms = {login_timeout_ms / 1000:.0f} s)")
    inicio = time.time()
    logged = mt5.login(
        login=int(login), password=password, server=server, timeout=login_timeout_ms
    )
    print(f"login -> {logged}  {mt5.last_error()}  ({time.time() - inicio:.1f} s)")

    if not logged:
        _line("5. reenganche (el cambio de bróker tira el IPC)")
        print("esperando 20 s...")
        time.sleep(20)
        mt5.shutdown()
        time.sleep(1)
        re_ok = mt5.initialize(path=str(exe), timeout=init_timeout_ms, portable=True)
        print(f"initialize -> {re_ok}  {mt5.last_error()}")
        cuenta = mt5.account_info() if re_ok else None
        if cuenta is not None:
            print(f"dentro: login={cuenta.login} server={cuenta.server}")
            if int(cuenta.login) == int(login):
                print("\nEl login SÍ funcionó: solo se perdió el canal al hacerlo.")
                mt5.shutdown()
                return 0
        print("\nEl login no llegó a entrar. Revisa credenciales y servidor.")
        mt5.shutdown()
        return 1

    cuenta = mt5.account_info()
    print(f"dentro: login={cuenta.login} server={cuenta.server} balance={cuenta.balance}")
    mt5.shutdown()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sonda el terminal MT5 de un slot.")
    parser.add_argument("--slot", default="Slot-01")
    parser.add_argument("--terminals-root", type=Path, default=Path("terminals"))
    parser.add_argument("--login", type=int, default=None)
    parser.add_argument("--password", default="")
    parser.add_argument("--server", default="")
    parser.add_argument(
        "--init-timeout-ms", type=int, default=60_000, help="En MILIsegundos."
    )
    parser.add_argument(
        "--login-timeout-ms", type=int, default=180_000, help="En MILIsegundos."
    )
    args = parser.parse_args(argv)

    if args.login is not None and not args.server:
        parser.error("--login necesita también --server")

    return probe(
        args.terminals_root / args.slot / "terminal64.exe",
        login=args.login,
        password=args.password,
        server=args.server,
        init_timeout_ms=args.init_timeout_ms,
        login_timeout_ms=args.login_timeout_ms,
    )


if __name__ == "__main__":
    sys.exit(main())
