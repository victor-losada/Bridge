"""Worker: un proceso, una cuenta, un terminal portable.

Ciclo:
  1. Login MT5 (investor preferido).
  2. Cargar historial de deals (lookback) y sembrar el DealMatcher.
     Por defecto NO se reenvían trade.closed antiguos al Core.
  3. Loop:
       - positions_get  → opened / updated / closed
       - history_deals  → emparejar IN+OUT → trade.closed
       - account_info   → account.snapshot
  4. Si una posición desaparece de positions_get pero los deals OUT aún
     no están en historial (retraso del servidor), queda en pending_closed
     y se reintenta en el siguiente poll.

Persistencia local (terminals/Slot-XX/worker_state.json):
  position_ids ya emitidos como trade.closed, para sobrevivir un restart.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import time
from pathlib import Path

from app.models.events import (
    AccountSnapshotData,
    BridgeEvent,
    ConnectionStatusData,
)
from app.utils import setup_logging, to_iso_z, utc_now
from app.worker.deal_matcher import (
    DealMatcher,
    deal_from_mt5,
    order_from_mt5,
    orders_by_ticket,
)
from app.worker import trade_candles
from app.worker.event_emitter import EventEmitter
from app.worker.mt5_client import (
    MT5Client,
    MT5ConnectionError,
    PositionSnapshot,
    position_to_event_dict,
)

logger = logging.getLogger("worker")


class Worker:
    def __init__(
        self,
        *,
        slot_id: str,
        account_id: str,
        mt5_login: int,
        mt5_password: str,
        mt5_server: str,
        terminal_path: Path,
        core_events_url: str,
        core_api_key: str,
        poll_account_sec: float = 9.0,
        poll_positions_sec: float = 6.0,
        poll_history_sec: float = 7.0,
        history_lookback_days: int = 7,
        replay_history_on_connect: bool = False,
        mt5_init_timeout_ms: int = 60_000,
        login_retry_max: int = 4,
        login_retry_backoff_sec: float = 8.0,
        core_retry_max: int = 3,
        core_trust_env: bool = False,
        history_forward_buffer_hours: float = 24.0,
        mt5_login_timeout_ms: int = 180_000,
        mt5_login_attempts: int = 3,
        mt5_reattach_wait_sec: float = 20.0,
        emit_position_events: bool = True,
        emit_trade_candles: bool = False,
        trade_candles_count: int = 150,
    ) -> None:
        self.slot_id = slot_id
        self.account_id = account_id
        self.mt5_login = int(mt5_login)
        self._password = mt5_password
        self.mt5_server = mt5_server
        self.terminal_path = Path(terminal_path)
        self.poll_account_sec = poll_account_sec
        self.poll_positions_sec = poll_positions_sec
        self.poll_history_sec = poll_history_sec
        self.history_lookback_days = history_lookback_days
        self.replay_history_on_connect = replay_history_on_connect
        # El libro de posiciones abiertas es opcional: hay Cores que solo
        # quieren operaciones cerradas. El seguimiento interno se mantiene
        # igual (hace falta para emparejar los cierres), solo no se emite.
        self.emit_position_events = emit_position_events
        # Las velas de cada operación son un extra para el gráfico del Core:
        # apagadas por defecto porque un Core que no las espere las rechaza.
        self.emit_trade_candles = emit_trade_candles
        self.trade_candles_count = trade_candles_count
        self.login_retry_max = login_retry_max
        self.login_retry_backoff_sec = login_retry_backoff_sec

        self.client = MT5Client(
            self.terminal_path,
            timeout_ms=mt5_init_timeout_ms,
            history_forward_buffer_hours=history_forward_buffer_hours,
            login_timeout_ms=mt5_login_timeout_ms,
            login_attempts=mt5_login_attempts,
            reattach_wait_sec=mt5_reattach_wait_sec,
        )
        self.emitter = EventEmitter(
            core_events_url,
            core_api_key,
            max_attempts=core_retry_max,
            trust_env=core_trust_env,
        )
        self.matcher = DealMatcher()

        self._running = True
        self._positions: dict[int, PositionSnapshot] = {}
        self._sl_tp: dict[int, tuple[float | None, float | None]] = {}
        self._pending_closed: set[int] = set()
        self._state_path = self._resolve_state_path()

        self._last_runtime_status = "connecting"
        self._next_account = 0.0
        self._next_positions = 0.0
        self._next_history = 0.0

    def run(self) -> int:
        signal.signal(signal.SIGTERM, self._handle_stop)
        signal.signal(signal.SIGINT, self._handle_stop)
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, self._handle_stop)  # type: ignore[attr-defined]

        self._load_persist()
        self._write_runtime("connecting")
        if not self._login_with_retries():
            return 2

        try:
            self._bootstrap_history()
            self._loop()
            return 0
        except Exception:
            logger.exception("worker crash")
            self._status("error", "excepción no controlada en el worker")
            return 1
        finally:
            self._write_runtime("disconnected")
            self._status("disconnected", "worker detenido")
            self.client.shutdown()
            self.emitter.close()

    def _handle_stop(self, *_args: object) -> None:
        logger.info("señal de parada")
        self._running = False

    def _login_with_retries(self) -> bool:
        self._status("connecting", "iniciando terminal portable y login")
        last_err = "login fallido"
        for attempt in range(1, self.login_retry_max + 1):
            try:
                self.client.initialize_and_login(
                    self.mt5_login, self._password, self.mt5_server
                )
                # OJO: el password NO se borra. Este mismo método se reutiliza
                # para reconectar tras una caída de MT5; si se vaciaba, todos
                # los re-logins fallaban y el slot acababa en error.
                # (El plaintext ya vive en MT5_PASSWORD del entorno del hijo.)
                self._write_runtime("connected")
                self._status("connected", "login ok")
                return True
            except Exception as exc:
                last_err = str(exc)
                logger.warning("login intento %s/%s: %s", attempt, self.login_retry_max, exc)
                self.client.shutdown()
                if attempt < self.login_retry_max and self._running:
                    self._write_runtime("reconnecting")
                    self._status("reconnecting", last_err)
                    time.sleep(self.login_retry_backoff_sec * attempt)
        self._write_runtime("error")
        self._status("error", last_err)
        return False

    def _bootstrap_history(self) -> None:
        deals = self._pull_deals()
        self.matcher.ingest(deals)
        already_closed = self.matcher.snapshot_closed_ids()
        if self.replay_history_on_connect:
            trades = self.matcher.pop_ready_trades(sl_tp_by_position=self._sl_tp)
            for trade in trades:
                self._emit_trade(trade)
            logger.info("replay de %s trades cerrados del lookback", len(trades))
        else:
            # Sembrar para no spamear al Core con historial viejo.
            self.matcher.mark_emitted(already_closed)
            logger.info(
                "historial sembrado: %s deals, %s posiciones cerradas marcadas (sin emitir)",
                len(deals),
                len(already_closed),
            )
        self._save_persist()

        # Snapshot inicial de posiciones abiertas (sin position.opened de más:
        # sí emitimos opened para que el Core tenga el libro actual).
        for pos in self.client.positions():
            self._remember_sl_tp(pos)
            if self._emit_position("position.opened", pos):
                self._positions[pos.ticket] = pos
            else:
                # Sin registrarla, el primer poll la reemite.
                logger.warning(
                    "position.opened %s no aceptada al arrancar; se reintentará",
                    pos.ticket,
                )
        self._poll_account(force=True)

    def _loop(self) -> None:
        logger.info("loop de polling arrancado")
        while self._running:
            now = time.monotonic()
            try:
                if now >= self._next_positions:
                    self._poll_positions()
                    self._next_positions = now + self.poll_positions_sec
                if now >= self._next_history:
                    self._poll_history()
                    self._next_history = now + self.poll_history_sec
                if now >= self._next_account:
                    self._poll_account()
                    self._next_account = now + self.poll_account_sec
            except MT5ConnectionError as exc:
                logger.warning("desconexión MT5: %s", exc)
                self._status("reconnecting", str(exc))
                self.client.shutdown()
                if not self._login_with_retries():
                    break
            except Exception:
                logger.exception("error en poll")
            time.sleep(0.25)

    def _poll_positions(self) -> None:
        """Libro de posiciones, con reintento de lo que el Core no acepte.

        Una posición solo se da por conocida si su evento entró. Si el Core lo
        rechaza, se conserva el estado ANTERIOR: el siguiente poll vuelve a
        ver la diferencia y lo reemite. Sin esto, un rechazo hacía desaparecer
        la posición del libro del Core hasta el siguiente arranque del Worker.
        """
        current = {p.ticket: p for p in self.client.positions()}
        previous = self._positions
        known: dict[int, PositionSnapshot] = {}

        for ticket, pos in current.items():
            self._remember_sl_tp(pos)
            prev = previous.get(ticket)
            if prev is None:
                if self._emit_position("position.opened", pos):
                    known[ticket] = pos
                else:
                    logger.warning(
                        "position.opened %s no aceptada; se reintentará", ticket
                    )
            elif prev.fingerprint() != pos.fingerprint():
                if self._emit_position("position.updated", pos):
                    known[ticket] = pos
                else:
                    known[ticket] = prev  # el cambio sigue pendiente
                    logger.warning(
                        "position.updated %s no aceptada; se reintentará", ticket
                    )
            else:
                known[ticket] = pos

        for ticket, prev in previous.items():
            if ticket in current:
                continue
            if not self._emit_position("position.closed", prev):
                # Sigue abierta para nosotros: el próximo poll la ve cerrarse
                # otra vez y reemite.
                known[ticket] = prev
                logger.warning(
                    "position.closed %s no aceptada; se reintentará", ticket
                )
            self._pending_closed.add(ticket)

        self._positions = known

    def _poll_history(self) -> None:
        deals = self._pull_deals()
        fresh = self.matcher.ingest(deals)
        if fresh:
            logger.info("deals nuevos: %s", len(fresh))
        orders = self._pull_orders()

        # Primero intentar cerrar las posiciones que desaparecieron del libro.
        for pid in list(self._pending_closed):
            trades = self.matcher.pop_ready_trades(
                sl_tp_by_position=self._sl_tp,
                orders=orders,
                only_position_id=pid,
            )
            if trades:
                for trade in trades:
                    self._emit_trade(trade)
                self._pending_closed.discard(pid)

        # Cierres que ocurrieron entre polls (o cuentas netting rápidas).
        for trade in self.matcher.pop_ready_trades(
            sl_tp_by_position=self._sl_tp, orders=orders
        ):
            self._pending_closed.discard(trade.position_id)
            self._emit_trade(trade)  # si el Core lo rechaza, vuelve a pending

        self._save_persist()

    def _poll_account(self, force: bool = False) -> None:
        snap = self.client.account_info()
        data = AccountSnapshotData(
            balance=snap.balance,
            equity=snap.equity,
            margin=snap.margin,
            free_margin=snap.free_margin,
            margin_level=snap.margin_level,
            profit=round(snap.profit, 2),
            currency=snap.currency,
            leverage=snap.leverage,
            name=snap.name,
            server=snap.server,
        )
        self._emit("account.snapshot", data.to_payload())
        _ = force

    def _pull_deals(self) -> list:
        raw = self.client.history_deals(self.history_lookback_days)
        parsed = []
        for item in raw:
            deal = deal_from_mt5(item)
            if deal is not None:
                parsed.append(deal)
        return parsed

    def _pull_orders(self) -> dict:
        """SL/TP de apertura por ticket de orden.

        Es lo unico que recupera el stop de un trade historico: cuando el
        Worker no estaba vivo mientras la posicion existia, nadie sondeo su
        SL y el deal casi siempre lo trae en 0.

        Un fallo aqui no puede tumbar la emision de trade.closed: sin ordenes
        el trade sale igual, solo que sin stop.
        """
        try:
            raw = self.client.history_orders(self.history_lookback_days)
        except Exception:  # noqa: BLE001 - el trade importa mas que su stop
            logger.warning("no se pudo leer el historial de ordenes", exc_info=True)
            return {}
        parsed = []
        for item in raw:
            order = order_from_mt5(item)
            if order is not None:
                parsed.append(order)
        return orders_by_ticket(parsed)

    def _remember_sl_tp(self, pos: PositionSnapshot) -> None:
        sl = pos.sl if pos.sl else None
        tp = pos.tp if pos.tp else None
        self._sl_tp[pos.ticket] = (sl, tp)

    def _emit_trade_candles(self, trade) -> None:  # noqa: ANN001
        """Velas de la operación, para que el Core pueda dibujarla.

        Se llama solo con el trade ya aceptado. Cualquier fallo aquí se traga:
        quedarse sin gráfico es un incordio, perder la operación no.
        """
        if not self.emit_trade_candles:
            return
        try:
            tf = trade_candles.elegir_timeframe(trade.close_time - trade.open_time)
            desde, hasta = trade_candles.ventana(
                trade.open_time, trade.close_time, tf, self.trade_candles_count
            )
            rates = self.client.copy_rates(trade.symbol, tf, desde, hasta)
            data = trade_candles.construir(
                position_id=trade.position_id,
                symbol=trade.symbol,
                timeframe=tf,
                rates=rates,
            )
        except Exception:  # noqa: BLE001 - el trade ya está guardado
            logger.warning(
                "no se pudieron leer las velas de %s", trade.position_id, exc_info=True
            )
            return
        if data is None:
            logger.info("sin velas para %s (%s)", trade.symbol, trade.position_id)
            return
        self._emit("trade.candles", data.model_dump())

    def _emit_trade(self, trade) -> None:  # noqa: ANN001
        ok = self._emit("trade.closed", trade.to_event_data().model_dump())
        if not ok:
            # Un trade cerrado no se puede perder: si el Core no lo aceptó,
            # se desmarca y vuelve a salir en el siguiente poll de historial.
            self.matcher.unmark_emitted([trade.position_id])
            self._pending_closed.add(trade.position_id)
            logger.warning(
                "trade.closed position=%s no aceptado por el Core; se reintentará",
                trade.position_id,
            )
            return
        logger.info(
            "trade.closed position=%s %s %s vol=%s pnl=%s",
            trade.position_id,
            trade.symbol,
            trade.direction,
            trade.volume,
            trade.profit,
        )
        # Solo con el trade ya a salvo en el Core.
        self._emit_trade_candles(trade)

    def _status(self, status: str, message: str | None = None) -> None:
        data = ConnectionStatusData(
            status=status,  # type: ignore[arg-type]
            slot_id=self.slot_id,
            message=message,
        )
        self._emit("connection.status", data.to_payload())

    def _emit_position(self, event: str, pos: PositionSnapshot) -> bool:
        """Emite un evento de posición, si el Core los quiere.

        Con emit_position_events=False devuelve True sin mandar nada: el
        seguimiento interno sigue igual y los trade.closed no se ven afectados.
        """
        if not self.emit_position_events:
            return True
        return self._emit(event, position_to_event_dict(pos))

    def _emit(self, event: str, data: dict) -> bool:
        payload = BridgeEvent.make(
            event=event,
            account_id=self.account_id,
            mt5_login=self.mt5_login,
            timestamp=to_iso_z(utc_now()),
            data=data,
        )
        ok = self.emitter.emit(payload)
        # Publicar el resultado para que /slots muestre si el Core recibe o no.
        if not ok or event in {"connection.status", "account.snapshot"}:
            self._write_runtime(self._last_runtime_status)
        return ok

    def _write_runtime(self, status: str) -> None:
        """Archivo que lee el Manager para el estado del slot (sin IPC extra).

        Incluye el estado del canal hacia el Core: sin esto, un Core que
        rechaza o no recibe los eventos es invisible desde /slots.
        """
        self._last_runtime_status = status
        folder = self.terminal_path if self.terminal_path.is_dir() else self.terminal_path.parent
        path = folder / "slot_runtime.json"
        try:
            path.write_text(
                json.dumps(
                    {
                        "status": status,
                        "account_id": self.account_id,
                        "mt5_login": self.mt5_login,
                        "updated_at": to_iso_z(),
                        "core_events_url": self.emitter.url,
                        "emit": self.emitter.stats(),
                    }
                ),
                encoding="utf-8",
            )
        except Exception:
            logger.exception("no se pudo escribir %s", path)

    def _resolve_state_path(self) -> Path:
        folder = self.terminal_path if self.terminal_path.is_dir() else self.terminal_path.parent
        return folder / "worker_state.json"

    def _load_persist(self) -> None:
        if not self._state_path.is_file():
            return
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8-sig"))
            self.matcher.load_persist(payload)
            logger.info("estado local cargado: %s", self._state_path)
        except Exception:
            logger.exception("no se pudo leer %s", self._state_path)

    def _save_persist(self) -> None:
        try:
            self._state_path.write_text(
                json.dumps(self.matcher.dump_persist()),
                encoding="utf-8",
            )
        except Exception:
            logger.exception("no se pudo escribir %s", self._state_path)


def run_worker_from_env() -> None:
    parser = argparse.ArgumentParser(description="MT5 Bridge Worker")
    parser.add_argument("--slot-id", default=os.environ.get("SLOT_ID", "Slot-01"))
    args, _unknown = parser.parse_known_args()
    slot_id = args.slot_id
    setup_logging(slot_id)

    required = [
        "ACCOUNT_ID",
        "MT5_LOGIN",
        "MT5_PASSWORD",
        "MT5_SERVER",
        "MT5_TERMINAL_PATH",
        "CORE_EVENTS_URL",
        "CORE_API_KEY",
    ]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"Faltan variables de entorno: {', '.join(missing)}")

    worker = Worker(
        slot_id=slot_id,
        account_id=os.environ["ACCOUNT_ID"],
        mt5_login=int(os.environ["MT5_LOGIN"]),
        mt5_password=os.environ["MT5_PASSWORD"],
        mt5_server=os.environ["MT5_SERVER"],
        terminal_path=Path(os.environ["MT5_TERMINAL_PATH"]),
        core_events_url=os.environ["CORE_EVENTS_URL"],
        core_api_key=os.environ["CORE_API_KEY"],
        poll_account_sec=float(os.environ.get("POLL_ACCOUNT_SEC", "9")),
        poll_positions_sec=float(os.environ.get("POLL_POSITIONS_SEC", "6")),
        poll_history_sec=float(os.environ.get("POLL_HISTORY_SEC", "7")),
        history_lookback_days=int(os.environ.get("HISTORY_LOOKBACK_DAYS", "7")),
        replay_history_on_connect=os.environ.get("REPLAY_HISTORY_ON_CONNECT", "false").lower()
        in {"1", "true", "yes"},
        mt5_init_timeout_ms=int(os.environ.get("MT5_INIT_TIMEOUT_MS", "60000")),
        login_retry_max=int(os.environ.get("LOGIN_RETRY_MAX", "4")),
        login_retry_backoff_sec=float(os.environ.get("LOGIN_RETRY_BACKOFF_SEC", "8")),
        core_retry_max=int(os.environ.get("CORE_RETRY_MAX", "3")),
        core_trust_env=os.environ.get("CORE_HTTP_TRUST_ENV", "false").lower()
        in {"1", "true", "yes"},
        history_forward_buffer_hours=float(
            os.environ.get("HISTORY_FORWARD_BUFFER_HOURS", "24")
        ),
        mt5_login_timeout_ms=int(os.environ.get("MT5_LOGIN_TIMEOUT_MS", "180000")),
        mt5_login_attempts=int(os.environ.get("MT5_LOGIN_ATTEMPTS", "3")),
        mt5_reattach_wait_sec=float(os.environ.get("MT5_REATTACH_WAIT_SEC", "20")),
        emit_position_events=os.environ.get("EMIT_POSITION_EVENTS", "true").lower()
        not in {"0", "false", "no"},
        emit_trade_candles=os.environ.get("EMIT_TRADE_CANDLES", "false").lower()
        in {"1", "true", "yes"},
        trade_candles_count=int(os.environ.get("TRADE_CANDLES_COUNT", "150")),
    )
    raise SystemExit(worker.run())
