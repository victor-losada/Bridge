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
from app.worker.deal_matcher import DealMatcher, deal_from_mt5
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
        self.login_retry_max = login_retry_max
        self.login_retry_backoff_sec = login_retry_backoff_sec

        self.client = MT5Client(
            self.terminal_path,
            timeout_ms=mt5_init_timeout_ms,
            history_forward_buffer_hours=history_forward_buffer_hours,
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
            self._positions[pos.ticket] = pos
            self._remember_sl_tp(pos)
            self._emit("position.opened", position_to_event_dict(pos))
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
        current_list = self.client.positions()
        current = {p.ticket: p for p in current_list}

        for ticket, pos in current.items():
            self._remember_sl_tp(pos)
            prev = self._positions.get(ticket)
            if prev is None:
                self._emit("position.opened", position_to_event_dict(pos))
            elif prev.fingerprint() != pos.fingerprint():
                self._emit("position.updated", position_to_event_dict(pos))

        for ticket, prev in list(self._positions.items()):
            if ticket not in current:
                self._emit("position.closed", position_to_event_dict(prev))
                self._pending_closed.add(ticket)

        self._positions = current

    def _poll_history(self) -> None:
        deals = self._pull_deals()
        fresh = self.matcher.ingest(deals)
        if fresh:
            logger.info("deals nuevos: %s", len(fresh))

        # Primero intentar cerrar las posiciones que desaparecieron del libro.
        for pid in list(self._pending_closed):
            trades = self.matcher.pop_ready_trades(
                sl_tp_by_position=self._sl_tp,
                only_position_id=pid,
            )
            if trades:
                for trade in trades:
                    self._emit_trade(trade)
                self._pending_closed.discard(pid)

        # Cierres que ocurrieron entre polls (o cuentas netting rápidas).
        for trade in self.matcher.pop_ready_trades(sl_tp_by_position=self._sl_tp):
            self._pending_closed.discard(trade.position_id)
            self._emit_trade(trade)

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

    def _remember_sl_tp(self, pos: PositionSnapshot) -> None:
        sl = pos.sl if pos.sl else None
        tp = pos.tp if pos.tp else None
        self._sl_tp[pos.ticket] = (sl, tp)

    def _emit_trade(self, trade) -> None:  # noqa: ANN001
        self._emit("trade.closed", trade.to_event_data().model_dump())
        logger.info(
            "trade.closed position=%s %s %s vol=%s pnl=%s",
            trade.position_id,
            trade.symbol,
            trade.direction,
            trade.volume,
            trade.profit,
        )

    def _status(self, status: str, message: str | None = None) -> None:
        data = ConnectionStatusData(
            status=status,  # type: ignore[arg-type]
            slot_id=self.slot_id,
            message=message,
        )
        self._emit("connection.status", data.to_payload())

    def _emit(self, event: str, data: dict) -> None:
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
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
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
    )
    raise SystemExit(worker.run())
