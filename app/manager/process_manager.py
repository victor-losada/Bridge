"""Arranque y parada de procesos Worker + terminal64.exe por slot.

El password se inyecta SOLO en el entorno del hijo (no va en argv).
Al stop: SIGTERM/terminate al Python worker y luego se mata el terminal
cuyo exe vive dentro de la carpeta del slot (portable).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil

from app.config import Settings

logger = logging.getLogger(__name__)


class ProcessManager:
    def __init__(self, settings: Settings, project_root: Path) -> None:
        self.settings = settings
        self.project_root = project_root
        self._procs: dict[str, subprocess.Popen[bytes]] = {}

    def start_worker(
        self,
        *,
        slot_id: str,
        account_id: str,
        mt5_login: int,
        mt5_password: str,
        mt5_server: str,
        terminal_path: Path,
    ) -> int:
        self.stop_slot(slot_id, terminal_path)

        env = os.environ.copy()
        env.update(
            {
                "SLOT_ID": slot_id,
                "ACCOUNT_ID": account_id,
                "MT5_LOGIN": str(mt5_login),
                "MT5_PASSWORD": mt5_password,
                "MT5_SERVER": mt5_server,
                "MT5_TERMINAL_PATH": str(terminal_path),
                "CORE_EVENTS_URL": self.settings.core_events_url,
                "CORE_API_KEY": self.settings.core_api_key,
                "POLL_ACCOUNT_SEC": str(self.settings.poll_account_sec),
                "POLL_POSITIONS_SEC": str(self.settings.poll_positions_sec),
                "POLL_HISTORY_SEC": str(self.settings.poll_history_sec),
                "HISTORY_LOOKBACK_DAYS": str(self.settings.history_lookback_days),
                "HISTORY_FORWARD_BUFFER_HOURS": str(
                    self.settings.history_forward_buffer_hours
                ),
                "CORE_RETRY_MAX": str(self.settings.core_retry_max),
                "CORE_HTTP_TRUST_ENV": str(self.settings.core_http_trust_env).lower(),
                "REPLAY_HISTORY_ON_CONNECT": str(
                    self.settings.replay_history_on_connect
                ).lower(),
                "MT5_INIT_TIMEOUT_MS": str(self.settings.mt5_init_timeout_ms),
                "MT5_LOGIN_TIMEOUT_MS": str(self.settings.mt5_login_timeout_ms),
                "MT5_LOGIN_ATTEMPTS": str(self.settings.mt5_login_attempts),
                "MT5_REATTACH_WAIT_SEC": str(self.settings.mt5_reattach_wait_sec),
                "LOGIN_RETRY_MAX": str(self.settings.login_retry_max),
                "LOGIN_RETRY_BACKOFF_SEC": str(self.settings.login_retry_backoff_sec),
                "PYTHONUNBUFFERED": "1",
            }
        )

        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]

        log_dir = self.settings.data_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"{slot_id}.log"
        handle = open(log_file, "ab", buffering=0)  # noqa: SIM115 — lo cierra el GC al terminar

        proc = subprocess.Popen(
            [sys.executable, "-m", "app.worker", "--slot-id", slot_id],
            cwd=str(self.project_root),
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
        self._procs[slot_id] = proc
        logger.info("%s worker pid=%s log=%s", slot_id, proc.pid, log_file)
        return proc.pid

    def stop_slot(self, slot_id: str, terminal_path: Path) -> None:
        proc = self._procs.pop(slot_id, None)
        if proc is not None and proc.poll() is None:
            logger.info("%s terminate worker pid=%s", slot_id, proc.pid)
            _terminate_process_tree(proc.pid, self.settings.shutdown_timeout_sec)
        # El terminal portable a veces queda huérfano tras matar al worker.
        kill_portable_terminal(terminal_path, self.settings.shutdown_timeout_sec)

    def is_running(self, slot_id: str) -> bool:
        proc = self._procs.get(slot_id)
        return proc is not None and proc.poll() is None

    def worker_pid(self, slot_id: str) -> int | None:
        proc = self._procs.get(slot_id)
        if proc is None or proc.poll() is not None:
            return None
        return proc.pid

    def stop_all(self, slots: dict[str, Path]) -> None:
        for slot_id, path in slots.items():
            try:
                self.stop_slot(slot_id, path)
            except Exception:
                logger.exception("stop_all %s", slot_id)


def kill_portable_terminal(terminal_path: Path, timeout_sec: float) -> None:
    """Mata terminal64.exe cuyo ejecutable está bajo la carpeta del slot."""
    root = terminal_path.resolve()
    if root.is_file():
        root = root.parent
    root_s = str(root).lower()
    victims: list[psutil.Process] = []
    for proc in psutil.process_iter(["pid", "name", "exe"]):
        try:
            name = (proc.info.get("name") or "").lower()
            exe = (proc.info.get("exe") or "")
            if "terminal64.exe" not in name and "terminal.exe" not in name:
                continue
            if exe and str(Path(exe).resolve()).lower().startswith(root_s):
                victims.append(proc)
        except (psutil.Error, OSError):
            continue
    for proc in victims:
        try:
            logger.info("terminate terminal pid=%s exe=%s", proc.pid, proc.exe())
            proc.terminate()
        except psutil.Error:
            continue
    deadline = time.time() + timeout_sec
    for proc in victims:
        try:
            proc.wait(timeout=max(0.1, deadline - time.time()))
        except (psutil.TimeoutExpired, psutil.Error):
            try:
                proc.kill()
            except psutil.Error:
                pass


def _terminate_process_tree(pid: int, timeout_sec: float) -> None:
    try:
        parent = psutil.Process(pid)
    except psutil.Error:
        return
    children = parent.children(recursive=True)
    for child in children:
        try:
            child.terminate()
        except psutil.Error:
            pass
    try:
        parent.terminate()
    except psutil.Error:
        return
    _, alive = psutil.wait_procs([parent, *children], timeout=timeout_sec)
    for proc in alive:
        try:
            proc.kill()
        except psutil.Error:
            pass
