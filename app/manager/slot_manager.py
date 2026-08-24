"""Pool de slots: asignación, lifecycle y reintentos.

Estados:
  free → connecting → connected
                 ↘ error (tras N restarts)
  cualquiera → disconnected → free (al desconectar a petición del Core)

Un slot ocupado no se reasigna. El Manager vigila PIDs y relanza el Worker
si muere inesperadamente mientras la cuenta sigue asignada.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from app.config import Settings, get_settings
from app.manager.process_manager import ProcessManager
from app.manager.state import SlotState, SlotStatus
from app.security.credentials import CredentialCipher

logger = logging.getLogger(__name__)


_CORE_CHANNEL_PREFIX = "el Core no acepta eventos: "


class SlotManagerError(RuntimeError):
    pass


def _core_channel_error(emit: dict, current: str | None) -> str | None:
    """Estado del canal hacia el Core; se limpia solo cuando vuelve a entregar.

    Las marcas de tiempo son ISO-8601 UTC de ancho fijo, así que comparar
    cadenas equivale a comparar instantes.
    """
    error_at = emit.get("last_error_at")
    ok_at = emit.get("last_ok_at")
    if emit.get("last_error") and error_at and (not ok_at or error_at > ok_at):
        return f"{_CORE_CHANNEL_PREFIX}{emit['last_error']}"
    if current and current.startswith(_CORE_CHANNEL_PREFIX):
        return None
    return current


class SlotManager:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.cipher = CredentialCipher(self.settings.fernet_key)
        self.project_root = Path(__file__).resolve().parents[2]
        self.proc = ProcessManager(self.settings, self.project_root)
        self.slots: dict[str, SlotState] = {}
        self._lock = asyncio.Lock()
        self._watch_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.settings.terminals_root.mkdir(parents=True, exist_ok=True)
        for i in range(1, self.settings.slot_count + 1):
            slot_id = f"Slot-{i:02d}"
            path = self.slot_dir(slot_id)
            path.mkdir(parents=True, exist_ok=True)
            self.slots[slot_id] = SlotState(slot_id=slot_id)
        self._watch_task = asyncio.create_task(self._watch_workers(), name="slot-watch")
        logger.info("pool listo: %s slots en %s", len(self.slots), self.settings.terminals_root)

    async def shutdown(self) -> None:
        if self._watch_task:
            self._watch_task.cancel()
        paths = {sid: self.slot_dir(sid) for sid in self.slots}
        self.proc.stop_all(paths)

    def slot_dir(self, slot_id: str) -> Path:
        return self.settings.terminals_root / slot_id

    def terminal_ready(self, slot_id: str) -> bool:
        folder = self.slot_dir(slot_id)
        return (folder / "terminal64.exe").is_file()

    def list_slots(self) -> list[dict]:
        out = []
        for state in self.slots.values():
            item = state.public_dict()
            item["terminal_ready"] = self.terminal_ready(state.slot_id)
            out.append(item)
        return out

    def get_slot(self, slot_id: str) -> SlotState:
        if slot_id not in self.slots:
            raise SlotManagerError(f"slot desconocido: {slot_id}")
        return self.slots[slot_id]

    def find_by_account(self, account_id: str) -> SlotState | None:
        for state in self.slots.values():
            if state.account_id == account_id:
                return state
        return None

    async def connect(
        self,
        *,
        account_id: str,
        mt5_login: int,
        mt5_password: str,
        mt5_server: str,
        investor: bool = True,
        slot_id: str | None = None,
    ) -> SlotState:
        async with self._lock:
            existing = self.find_by_account(account_id)
            if existing and existing.status in {SlotStatus.CONNECTED, SlotStatus.CONNECTING}:
                raise SlotManagerError(
                    f"account_id {account_id} ya está en {existing.slot_id}"
                )

            state = self._pick_slot(slot_id)
            if not self.terminal_ready(state.slot_id):
                raise SlotManagerError(
                    f"{state.slot_id} no tiene terminal64.exe. "
                    "Copia un MT5 portable en esa carpeta."
                )

            state.account_id = account_id
            state.mt5_login = mt5_login
            state.mt5_server = mt5_server
            state.investor = investor
            state.password_encrypted = self.cipher.encrypt(mt5_password)
            state.status = SlotStatus.CONNECTING
            state.last_error = None
            state.restart_count = 0
            state.touch()
            self._spawn(state)
            return state

    async def disconnect(
        self, *, account_id: str | None = None, slot_id: str | None = None
    ) -> SlotState | None:
        """Desconecta y libera el slot.

        Devuelve None si la cuenta no estaba en ningún slot. Desvincular es
        una orden de estado final ("que no esté conectada"), no una operación
        sobre un recurso: si ya se cumple, no es un error. El Manager guarda
        los slots en memoria, así que tras reiniciarlo el Core pide desvincular
        cuentas que aquí ya no existen — y eso no puede dejar al Core sin poder
        desvincular nada.
        """
        async with self._lock:
            state: SlotState | None = None
            if slot_id:
                state = self.get_slot(slot_id)  # slot inexistente sí es error
            elif account_id:
                state = self.find_by_account(account_id)
            if state is None:
                logger.info("desconectar: %s no estaba en ningún slot", account_id)
                return None
            self.proc.stop_slot(state.slot_id, self.slot_dir(state.slot_id))
            state.status = SlotStatus.DISCONNECTED
            state.touch()
            # Liberar el slot para reutilizarlo.
            freed = state.slot_id
            state.reset_assignment()
            logger.info("%s liberado", freed)
            return self.slots[freed]

    async def restart(self, slot_id: str) -> SlotState:
        async with self._lock:
            state = self.get_slot(slot_id)
            if not state.account_id or not state.password_encrypted:
                raise SlotManagerError("el slot no tiene cuenta asignada")
            self._spawn(state)
            return state

    def _pick_slot(self, slot_id: str | None) -> SlotState:
        if slot_id:
            state = self.get_slot(slot_id)
            if state.status not in {SlotStatus.FREE, SlotStatus.DISCONNECTED, SlotStatus.ERROR}:
                raise SlotManagerError(f"{slot_id} no está libre (status={state.status})")
            return state
        for state in self.slots.values():
            if state.status == SlotStatus.FREE and self.terminal_ready(state.slot_id):
                return state
        for state in self.slots.values():
            if state.status == SlotStatus.FREE:
                return state
        raise SlotManagerError("no hay slots libres")

    def _spawn(self, state: SlotState) -> None:
        assert state.account_id and state.mt5_login and state.mt5_server
        assert state.password_encrypted
        password = self.cipher.decrypt(state.password_encrypted)
        pid = self.proc.start_worker(
            slot_id=state.slot_id,
            account_id=state.account_id,
            mt5_login=state.mt5_login,
            mt5_password=password,
            mt5_server=state.mt5_server,
            terminal_path=self.slot_dir(state.slot_id),
        )
        state.worker_pid = pid
        state.status = SlotStatus.CONNECTING
        state.touch()

    def _read_runtime(self, slot_id: str) -> dict:
        path = self.slot_dir(slot_id) / "slot_runtime.json"
        if not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    async def _watch_workers(self) -> None:
        """Si el Worker muere y la cuenta sigue asignada, reintento limitado."""
        while True:
            await asyncio.sleep(3)
            to_restart: list[str] = []
            async with self._lock:
                for state in self.slots.values():
                    if state.status == SlotStatus.FREE or not state.account_id:
                        continue
                    running = self.proc.is_running(state.slot_id)
                    if running:
                        payload = self._read_runtime(state.slot_id)
                        runtime = str(payload.get("status") or "") or None
                        emit = payload.get("emit")
                        if isinstance(emit, dict):
                            state.emit = emit
                            # Un fallo de entrega al Core no mata el worker,
                            # pero sí explica "conecta y no cargan datos".
                            state.last_error = _core_channel_error(
                                emit, state.last_error
                            )
                        if runtime == "connected":
                            state.status = SlotStatus.CONNECTED
                        elif runtime == "error":
                            state.status = SlotStatus.ERROR
                        elif runtime in {"connecting", "reconnecting"}:
                            state.status = (
                                SlotStatus.CONNECTING
                                if runtime == "connecting"
                                else SlotStatus.CONNECTING
                            )
                        state.touch()
                        continue
                    if state.status in {SlotStatus.DISCONNECTED, SlotStatus.ERROR}:
                        continue
                    state.restart_count += 1
                    if state.restart_count > self.settings.worker_restart_max:
                        state.status = SlotStatus.ERROR
                        state.last_error = "worker muerto: se agotaron los reintentos"
                        state.touch()
                        logger.error("%s %s", state.slot_id, state.last_error)
                        continue
                    logger.warning(
                        "%s worker caído, restart %s/%s",
                        state.slot_id,
                        state.restart_count,
                        self.settings.worker_restart_max,
                    )
                    to_restart.append(state.slot_id)
            if to_restart:
                await asyncio.sleep(self.settings.worker_restart_backoff_sec)
                async with self._lock:
                    for slot_id in to_restart:
                        state = self.slots[slot_id]
                        if not state.account_id:
                            continue
                        try:
                            self._spawn(state)
                        except Exception as exc:
                            state.status = SlotStatus.ERROR
                            state.last_error = str(exc)
                            state.touch()
