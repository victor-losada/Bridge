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

_EXE = Path("terminal64.exe")

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

    def shutdown(self):
        pass

    def initialize(self, path=None, timeout=None, portable=None):
        return True

    def account_info(self):
        return None  # nadie dentro: obliga a reintentar el login


@pytest.fixture(autouse=True)
def _sin_esperas(monkeypatch):
    monkeypatch.setattr(mt5_module.time, "sleep", lambda _s: None)


def _cliente(tmp_path: Path, **kwargs) -> MT5Client:
    kwargs.setdefault("reattach_wait_sec", 0)
    return MT5Client(tmp_path, **kwargs)


def test_reintenta_mientras_el_terminal_cambia_de_broker(monkeypatch, tmp_path) -> None:
    fake = _FakeMT5(exitos_tras=3)
    monkeypatch.setattr(mt5_module, "mt5", fake)
    client = _cliente(tmp_path, login_attempts=3)

    assert client._login_with_patience(_EXE, 203395, "x", "NYSMarketsLtd-trade") is True
    assert len(fake.intentos) == 3


def test_el_timeout_de_login_llega_al_terminal(monkeypatch, tmp_path) -> None:
    """Sin timeout explícito, login() se rinde a los 60 s por defecto."""
    fake = _FakeMT5(exitos_tras=1)
    monkeypatch.setattr(mt5_module, "mt5", fake)
    client = _cliente(tmp_path, login_timeout_ms=180_000)

    client._login_with_patience(_EXE, 203395, "x", "NYSMarketsLtd-trade")

    assert fake.intentos[0]["timeout"] == 180_000


def test_credenciales_malas_no_se_reintentan(monkeypatch, tmp_path) -> None:
    """Repetir un login rechazado solo arriesga bloquear la cuenta."""
    fake = _FakeMT5(exitos_tras=99, error=AUTORIZACION)
    monkeypatch.setattr(mt5_module, "mt5", fake)
    client = _cliente(tmp_path, login_attempts=3)

    assert client._login_with_patience(_EXE, 203395, "mala", "NYSMarketsLtd-trade") is False
    assert len(fake.intentos) == 1


def test_se_rinde_tras_agotar_los_intentos(monkeypatch, tmp_path) -> None:
    fake = _FakeMT5(exitos_tras=99)
    monkeypatch.setattr(mt5_module, "mt5", fake)
    client = _cliente(tmp_path, login_attempts=3)

    assert client._login_with_patience(_EXE, 203395, "x", "NYSMarketsLtd-trade") is False
    assert len(fake.intentos) == 3


# --- El IPC se cae al cambiar de bróker ------------------------------------
# login() devuelve -10005 AL INSTANTE (no agota el timeout) porque MT5 corta
# el pipe al reconectar contra el servidor nuevo. El terminal sí acaba
# entrando, así que hay que reenganchar y preguntar quién está dentro.


class _CuentaMT5:
    def __init__(self, login: int) -> None:
        self.login = login


class _FakeMT5PipeCaido:
    """El login prospera dentro del terminal, pero la llamada devuelve -10005."""

    def __init__(self, login_real: int, entra_tras: int = 1) -> None:
        self.login_real = login_real
        self.entra_tras = entra_tras
        self.logins = 0
        self.reenganches = 0

    def login(self, login, password, server, timeout=60_000):
        self.logins += 1
        return False  # siempre falla de cara a la API

    def last_error(self):
        return IPC_TIMEOUT

    def shutdown(self):
        pass

    def initialize(self, path=None, timeout=None, portable=None):
        self.reenganches += 1
        return True

    def account_info(self):
        if self.reenganches >= self.entra_tras:
            return _CuentaMT5(self.login_real)
        return None


def test_reengancha_y_detecta_que_el_terminal_ya_entro(monkeypatch, tmp_path) -> None:
    fake = _FakeMT5PipeCaido(login_real=203395)
    monkeypatch.setattr(mt5_module, "mt5", fake)
    client = MT5Client(tmp_path, login_attempts=3, reattach_wait_sec=0)

    ok = client._login_with_patience(
        tmp_path / "terminal64.exe", 203395, "x", "NYSMarketsLtd-trade"
    )

    assert ok is True
    assert fake.reenganches == 1  # bastó un reenganche


def test_si_entro_otra_cuenta_no_lo_da_por_bueno(monkeypatch, tmp_path) -> None:
    """Reenganchar y ver una cuenta distinta no es un login correcto."""
    fake = _FakeMT5PipeCaido(login_real=999999)
    monkeypatch.setattr(mt5_module, "mt5", fake)
    client = MT5Client(tmp_path, login_attempts=2, reattach_wait_sec=0)

    ok = client._login_with_patience(
        tmp_path / "terminal64.exe", 203395, "x", "NYSMarketsLtd-trade"
    )

    assert ok is False
