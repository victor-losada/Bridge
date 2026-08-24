"""Login MT5 cuando el slot cambia de bróker.

Los slots se clonan de una plantilla que trae dentro la sesión de OTRO
bróker. Al conectar una cuenta de un bróker distinto, el terminal tiene que
desconectarse, localizar el servidor nuevo y sincronizar sus símbolos: pasa
del minuto y el timeout por defecto de login() lo corta a medias.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.worker import mt5_client as mt5_module
from app.worker.mt5_client import MT5Client

IPC_TIMEOUT = (-10005, "IPC timeout")
AUTORIZACION = (-6, "Terminal: Authorization failed")


class _FakeMT5:
    """Terminal simulado: tarda N intentos en completar el cambio de bróker."""

    def __init__(self, exitos_tras: int, error=IPC_TIMEOUT) -> None:
        self.exitos_tras = exitos_tras
        self.error = error
        self.intentos: list[dict] = []

    def login(self, login: int, password: str, server: str, timeout: int = 60_000):
        self.intentos.append({"login": login, "server": server, "timeout": timeout})
        return len(self.intentos) >= self.exitos_tras

    def last_error(self):
        return self.error


@pytest.fixture(autouse=True)
def _sin_esperas(monkeypatch):
    monkeypatch.setattr(mt5_module.time, "sleep", lambda _s: None)


def _cliente(tmp_path: Path, **kwargs) -> MT5Client:
    return MT5Client(tmp_path, **kwargs)


def test_reintenta_mientras_el_terminal_cambia_de_broker(monkeypatch, tmp_path) -> None:
    fake = _FakeMT5(exitos_tras=3)
    monkeypatch.setattr(mt5_module, "mt5", fake)
    client = _cliente(tmp_path, login_attempts=3)

    assert client._login_with_patience(203395, "x", "NYSMarketsLtd-trade") is True
    assert len(fake.intentos) == 3


def test_el_timeout_de_login_llega_al_terminal(monkeypatch, tmp_path) -> None:
    """Sin timeout explícito, login() se rinde a los 60 s por defecto."""
    fake = _FakeMT5(exitos_tras=1)
    monkeypatch.setattr(mt5_module, "mt5", fake)
    client = _cliente(tmp_path, login_timeout_ms=180_000)

    client._login_with_patience(203395, "x", "NYSMarketsLtd-trade")

    assert fake.intentos[0]["timeout"] == 180_000


def test_credenciales_malas_no_se_reintentan(monkeypatch, tmp_path) -> None:
    """Repetir un login rechazado solo arriesga bloquear la cuenta."""
    fake = _FakeMT5(exitos_tras=99, error=AUTORIZACION)
    monkeypatch.setattr(mt5_module, "mt5", fake)
    client = _cliente(tmp_path, login_attempts=3)

    assert client._login_with_patience(203395, "mala", "NYSMarketsLtd-trade") is False
    assert len(fake.intentos) == 1


def test_se_rinde_tras_agotar_los_intentos(monkeypatch, tmp_path) -> None:
    fake = _FakeMT5(exitos_tras=99)
    monkeypatch.setattr(mt5_module, "mt5", fake)
    client = _cliente(tmp_path, login_attempts=3)

    assert client._login_with_patience(203395, "x", "NYSMarketsLtd-trade") is False
    assert len(fake.intentos) == 3
