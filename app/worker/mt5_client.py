"""Wrapper del paquete oficial MetaTrader5.

Un Worker = un proceso = un terminal portable = un login.
Nunca se llama a initialize() con dos paths distintos en el mismo proceso.
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

import psutil

from app.utils import mt5_time_to_utc, to_iso_z, utc_now

logger = logging.getLogger(__name__)

try:
    import MetaTrader5 as mt5  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - solo se instala en Windows con MT5
    mt5 = None  # type: ignore[assignment]


class MT5NotInstalledError(RuntimeError):
    pass


class MT5ConnectionError(RuntimeError):
    pass


@dataclass(slots=True)
class PositionSnapshot:
    ticket: int
    symbol: str
    type: str  # buy | sell
    volume: float
    open_price: float
    current_price: float
    sl: float
    tp: float
    profit: float
    swap: float
    commission: float
    open_time: datetime
    comment: str
    magic: int

    def fingerprint(self) -> tuple:
        """Campos que disparan position.updated (no cada tick de profit)."""
        return (
            round(self.volume, 8),
            round(self.open_price, 8),
            round(self.sl, 8),
            round(self.tp, 8),
            self.comment,
        )


@dataclass(slots=True)
class AccountSnapshot:
    login: int
    balance: float
    equity: float
    margin: float
    free_margin: float
    margin_level: float | None
    profit: float
    currency: str
    leverage: int
    name: str
    server: str


class MT5Client:
    def __init__(
        self,
        terminal_path: Path,
        timeout_ms: int = 60_000,
        history_forward_buffer_hours: float = 24.0,
        login_timeout_ms: int = 180_000,
        login_attempts: int = 3,
        reattach_wait_sec: float = 20.0,
    ) -> None:
        self.terminal_path = Path(terminal_path)
        self.timeout_ms = timeout_ms
        self.history_forward_buffer_hours = history_forward_buffer_hours
        self.login_timeout_ms = login_timeout_ms
        self.login_attempts = max(1, login_attempts)
        # Margen para que el terminal termine de cambiar de bróker antes de
        # preguntarle quién está dentro.
        self.reattach_wait_sec = reattach_wait_sec
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    def initialize_and_login(
        self,
        login: int,
        password: str,
        server: str,
    ) -> None:
        if mt5 is None:
            raise MT5NotInstalledError(
                "El paquete MetaTrader5 no está instalado. "
                "pip install MetaTrader5 (solo Windows)."
            )

        exe = self._resolve_terminal_exe()
        logger.info("initialize portable path=%s login=%s server=%s", exe, login, server)

        # initialize(login=...) en un solo paso suele dar -10005 IPC timeout:
        # el terminal aún no tiene el pipe listo. Arrancamos /portable, luego
        # attach, luego login.
        try:
            mt5.shutdown()
        except Exception:
            pass
        time.sleep(0.5)
        self._restart_portable_terminal(exe)

        last_err: Any = None
        attached = False
        for portable in (True, False):
            ok = mt5.initialize(
                path=str(exe),
                timeout=self.timeout_ms,
                portable=portable,
            )
            if ok:
                logger.info("mt5.initialize ok portable=%s", portable)
                attached = True
                break
            last_err = mt5.last_error()
            logger.warning("mt5.initialize portable=%s falló: %s", portable, last_err)
            try:
                mt5.shutdown()
            except Exception:
                pass
            time.sleep(1.0)

        if not attached:
            raise MT5ConnectionError(f"mt5.initialize falló: {last_err}")

        if not self._wait_terminal_ipc(max(40.0, self.timeout_ms / 1000.0)):
            err = mt5.last_error()
            mt5.shutdown()
            raise MT5ConnectionError(f"IPC no listo tras initialize: {err}")

        if not self._login_with_patience(exe, login, password, server):
            err = mt5.last_error()
            mt5.shutdown()
            raise MT5ConnectionError(f"mt5.login falló: {err}")

        info = mt5.account_info()
        if info is None:
            err = mt5.last_error()
            mt5.shutdown()
            raise MT5ConnectionError(f"account_info() vacío tras login: {err}")

        if int(info.login) != int(login):
            mt5.shutdown()
            raise MT5ConnectionError(
                f"Login inesperado: terminal={info.login} esperado={login}"
            )

        self._connected = True
        logger.info(
            "conectado trade_mode=%s name=%s server=%s",
            getattr(info, "trade_mode", None),
            info.name,
            info.server,
        )

    def _login_with_patience(
        self, exe: Path, login: int, password: str, server: str
    ) -> bool:
        """Login tolerante al cambio de bróker.

        Si el terminal viene de una plantilla con otro bróker, `login()` lo
        obliga a desconectarse y reconectarse contra el servidor nuevo. En ese
        corte MT5 **tira el canal IPC**, así que la llamada devuelve -10005 de
        inmediato aunque el terminal acabe entrando en la cuenta (se ve en el
        título de su ventana). Y reintentar el login sobre un pipe muerto
        devuelve -10005 otra vez, para siempre.

        Por eso, ante -10005, no se reintenta a ciegas: se vuelve a enganchar
        el IPC y se pregunta al terminal quién está dentro. Si ya es la cuenta
        pedida, el login había funcionado. Un fallo de credenciales no se
        reintenta — repetirlo solo arriesga bloquear la cuenta.
        """
        for attempt in range(1, self.login_attempts + 1):
            if mt5.login(
                login=int(login),
                password=password,
                server=server,
                timeout=self.login_timeout_ms,
            ):
                return True

            err = mt5.last_error()
            code = err[0] if isinstance(err, (tuple, list)) and err else None
            logger.warning(
                "mt5.login intento %s/%s: %s", attempt, self.login_attempts, err
            )
            if code != -10005:
                return False  # credenciales o servidor: reintentar no ayuda
            if attempt == self.login_attempts:
                break

            logger.info(
                "IPC caído por el cambio de bróker; reenganchando para ver "
                "si el terminal ya entró"
            )
            time.sleep(self.reattach_wait_sec)
            if not self._reattach(exe):
                continue
            if self._logged_as(login):
                logger.info("el terminal ya está dentro de la cuenta %s", login)
                return True
        return False

    def _reattach(self, exe: Path) -> bool:
        """Rehace el canal IPC con el terminal, que sigue vivo."""
        try:
            mt5.shutdown()
        except Exception:
            pass
        time.sleep(1.0)
        for portable in (True, False):
            if mt5.initialize(path=str(exe), timeout=self.timeout_ms, portable=portable):
                return True
        logger.warning("no se pudo reenganchar el IPC: %s", mt5.last_error())
        return False

    def _logged_as(self, login: int) -> bool:
        info = mt5.account_info()
        return info is not None and int(info.login) == int(login)

    def shutdown(self) -> None:
        self._connected = False
        if mt5 is not None:
            try:
                mt5.shutdown()
            except Exception:
                logger.exception("mt5.shutdown")

    def last_error(self) -> Any:
        if mt5 is None:
            return None
        return mt5.last_error()

    def terminal_info(self) -> Any:
        self._ensure()
        return mt5.terminal_info()

    def account_info(self) -> AccountSnapshot:
        self._ensure()
        info = mt5.account_info()
        if info is None:
            raise MT5ConnectionError(f"account_info None: {mt5.last_error()}")
        level = float(info.margin_level) if getattr(info, "margin_level", 0) else None
        return AccountSnapshot(
            login=int(info.login),
            balance=float(info.balance),
            equity=float(info.equity),
            margin=float(info.margin),
            free_margin=float(info.margin_free),
            margin_level=level,
            profit=float(info.profit),
            currency=str(info.currency),
            leverage=int(info.leverage),
            name=str(info.name),
            server=str(info.server),
        )

    def positions(self) -> list[PositionSnapshot]:
        self._ensure()
        rows = mt5.positions_get()
        if rows is None:
            # None = error; tupla vacía = sin posiciones
            err = mt5.last_error()
            if err and err[0] != 1:  # 1 = RES_S_OK
                raise MT5ConnectionError(f"positions_get None: {err}")
            return []
        out: list[PositionSnapshot] = []
        for p in rows:
            ptype = "buy" if int(p.type) == 0 else "sell"
            time_raw = getattr(p, "time_msc", None) or getattr(p, "time", None)
            out.append(
                PositionSnapshot(
                    ticket=int(p.ticket),
                    symbol=str(p.symbol),
                    type=ptype,
                    volume=float(p.volume),
                    open_price=float(p.price_open),
                    current_price=float(p.price_current),
                    sl=float(p.sl),
                    tp=float(p.tp),
                    profit=float(p.profit),
                    swap=float(p.swap),
                    # positions_get no siempre trae commission; 0.0 si falta
                    commission=float(getattr(p, "commission", 0.0) or 0.0),
                    open_time=mt5_time_to_utc(time_raw),
                    comment=str(getattr(p, "comment", "") or ""),
                    magic=int(getattr(p, "magic", 0) or 0),
                )
            )
        return out

    def history_deals(self, lookback_days: int) -> Sequence[Any]:
        """Deals del lookback.

        El rango se compara contra la hora del SERVIDOR del bróker, no contra
        UTC. Con un bróker en EET (UTC+2/+3) un tope en `utc_now()` deja fuera
        los cierres de las últimas horas y esos trade.closed no llegan nunca al
        Core. Por eso el tope va con holgura hacia delante; los duplicados los
        filtra el DealMatcher por ticket.
        """
        self._ensure()
        now = utc_now()
        buffer = timedelta(hours=self.history_forward_buffer_hours)
        date_from = now - timedelta(days=lookback_days) - buffer
        date_to = now + buffer
        deals = mt5.history_deals_get(date_from, date_to)
        if deals is None:
            err = mt5.last_error()
            if err and err[0] != 1:
                raise MT5ConnectionError(f"history_deals_get None: {err}")
            return []
        return deals

    def _ensure(self) -> None:
        if mt5 is None or not self._connected:
            raise MT5ConnectionError("MT5 no inicializado")

    def _restart_portable_terminal(self, exe: Path) -> None:
        """Mata un terminal64 colgado de este slot y lanza uno nuevo con /portable.

        Reusar un proceso 'ya en ejecución' tras un IPC timeout deja el pipe
        muerto y el siguiente initialize vuelve a fallar -10005.
        """
        root = str(exe.parent.resolve()).lower()
        _kill_slot_terminals(root)
        time.sleep(1.5)
        logger.info("lanzando %s /portable", exe)
        subprocess.Popen(
            [str(exe), "/portable"],
            cwd=str(exe.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 45
        while time.time() < deadline:
            if _slot_terminal_pid(root) is not None:
                time.sleep(8.0)
                return
            time.sleep(0.4)
        logger.warning("terminal64.exe no apareció en 45s; initialize igual lo intentará")

    def _wait_terminal_ipc(self, timeout_sec: float) -> bool:
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            info = mt5.terminal_info()
            if info is not None:
                logger.info("IPC listo community=%s", getattr(info, "community_account", None))
                return True
            time.sleep(0.5)
        return False

    def _resolve_terminal_exe(self) -> Path:
        path = self.terminal_path
        if path.is_file() and path.suffix.lower() == ".exe":
            return path
        candidate = path / "terminal64.exe"
        if candidate.is_file():
            return candidate
        raise FileNotFoundError(
            f"No se encontró terminal64.exe en {path}. "
            "Copia un terminal MT5 portable en esa carpeta."
        )


def _kill_slot_terminals(root_lower: str) -> None:
    for proc in list(psutil.process_iter(["pid", "name", "exe"])):
        try:
            name = (proc.info.get("name") or "").lower()
            exe = proc.info.get("exe") or ""
            if "terminal64.exe" not in name and "terminal.exe" not in name:
                continue
            if not exe or not str(Path(exe).resolve()).lower().startswith(root_lower):
                continue
            logger.info("kill terminal colgado pid=%s", proc.pid)
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except psutil.TimeoutExpired:
                proc.kill()
        except (psutil.Error, OSError):
            continue


def _slot_terminal_pid(root_lower: str) -> int | None:
    for proc in psutil.process_iter(["pid", "name", "exe"]):
        try:
            name = (proc.info.get("name") or "").lower()
            exe = proc.info.get("exe") or ""
            if "terminal64.exe" not in name and "terminal.exe" not in name:
                continue
            if exe and str(Path(exe).resolve()).lower().startswith(root_lower):
                return int(proc.info["pid"])
        except (psutil.Error, OSError):
            continue
    return None


def position_to_event_dict(pos: PositionSnapshot) -> dict[str, Any]:
    return {
        "positionId": str(pos.ticket),
        "symbol": pos.symbol,
        "type": pos.type,
        "volume": pos.volume,
        "openPrice": pos.open_price,
        "currentPrice": pos.current_price,
        "profit": round(pos.profit, 2),
        "swap": round(pos.swap, 2),
        "commission": round(pos.commission, 2),
        "stopLoss": pos.sl or None,
        "takeProfit": pos.tp or None,
        "openTime": to_iso_z(pos.open_time),
        "comment": pos.comment or None,
    }
