"""Las cuentas asignadas sobreviven a un reinicio del Manager.

Sin esto, reiniciar uvicorn dejaba al Core creyendo que las cuentas seguían
conectadas mientras el Bridge no tenía ninguna: dejaban de llegar datos, el
Core no permitía reconectar la misma cuenta y el desvincular no encontraba
nada que desvincular.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.config import Settings
from app.manager.slot_manager import SlotManager
from app.manager.state import SlotStatus

FERNET = "Ky1DFHTvPX2CjJKcgTLdmB2fF2ZQK5Xz3mFvMHVGLJU="


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        bridge_api_key="x" * 16,
        core_api_key="y" * 8,
        fernet_key=FERNET,
        slot_count=3,
        terminals_root=tmp_path / "terminals",
        data_dir=tmp_path / "data",
    )


async def _manager(tmp_path: Path, spawned: list, settings=None) -> SlotManager:
    settings = settings or _settings(tmp_path)
    settings.worker_spawn_stagger_sec = 0  # sin esperas en los tests
    manager = SlotManager(settings)
    manager.proc.start_worker = lambda **kw: (  # type: ignore[method-assign]
        spawned.append(kw) or 4242
    )
    manager.terminal_ready = lambda slot_id: True  # type: ignore[method-assign]
    await manager.start()
    if manager._restore_task:
        await manager._restore_task  # la recuperación va en segundo plano
    if manager._watch_task:
        manager._watch_task.cancel()
    return manager


async def test_la_cuenta_vuelve_sola_tras_reiniciar(tmp_path: Path) -> None:
    spawned: list = []
    manager = await _manager(tmp_path, spawned)
    await manager.connect(
        account_id="uuid-nys",
        mt5_login=203395,
        mt5_password="secreta",
        mt5_server="NYSMarketsLtd-trade",
        investor=False,
    )
    assert len(spawned) == 1

    # Reinicio del Manager: proceso nuevo, memoria en blanco.
    spawned.clear()
    reiniciado = await _manager(tmp_path, spawned)

    assert len(spawned) == 1
    assert spawned[0]["mt5_login"] == 203395
    assert spawned[0]["mt5_password"] == "secreta"  # descifrado correctamente
    estado = reiniciado.find_by_account("uuid-nys")
    assert estado is not None and estado.slot_id == "Slot-01"
    assert estado.mt5_server == "NYSMarketsLtd-trade"
    assert estado.investor is False


async def test_desvincular_no_deja_rastro(tmp_path: Path) -> None:
    spawned: list = []
    manager = await _manager(tmp_path, spawned)
    await manager.connect(
        account_id="uuid-nys",
        mt5_login=203395,
        mt5_password="secreta",
        mt5_server="S",
    )
    await manager.disconnect(account_id="uuid-nys")

    spawned.clear()
    reiniciado = await _manager(tmp_path, spawned)

    assert spawned == []
    assert reiniciado.find_by_account("uuid-nys") is None


async def test_dos_cuentas_vuelven_a_sus_slots(tmp_path: Path) -> None:
    spawned: list = []
    manager = await _manager(tmp_path, spawned)
    await manager.connect(
        account_id="uuid-nys", mt5_login=203395, mt5_password="a", mt5_server="NYS"
    )
    await manager.connect(
        account_id="uuid-exness", mt5_login=198812927, mt5_password="b", mt5_server="EX"
    )

    spawned.clear()
    reiniciado = await _manager(tmp_path, spawned)

    assert {s["mt5_login"] for s in spawned} == {203395, 198812927}
    assert reiniciado.find_by_account("uuid-nys").slot_id == "Slot-01"
    assert reiniciado.find_by_account("uuid-exness").slot_id == "Slot-02"


async def test_fernet_cambiada_deja_el_slot_en_error_visible(tmp_path: Path) -> None:
    """Un fallo al recuperar no puede pasar en silencio."""
    spawned: list = []
    manager = await _manager(tmp_path, spawned)
    await manager.connect(
        account_id="uuid-nys", mt5_login=203395, mt5_password="a", mt5_server="NYS"
    )

    otra_clave = _settings(tmp_path)
    otra_clave.fernet_key = "3sHkPTsw1DBKLbEnYbNjPmZXNXR0hFzHFyZlNCUjuQE="
    spawned.clear()
    reiniciado = await _manager(tmp_path, spawned, settings=otra_clave)

    assert spawned == []
    estado = reiniciado.slots["Slot-01"]
    assert estado.status == SlotStatus.ERROR
    assert "no se pudo recuperar" in (estado.last_error or "")


async def test_un_archivo_corrupto_no_impide_arrancar(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "slots.json").write_text("{ esto no es json", encoding="utf-8")

    spawned: list = []
    manager = await _manager(tmp_path, spawned, settings=settings)

    assert spawned == []
    assert len(manager.slots) == 3


async def test_el_password_no_se_guarda_en_claro(tmp_path: Path) -> None:
    spawned: list = []
    manager = await _manager(tmp_path, spawned)
    await manager.connect(
        account_id="uuid-nys",
        mt5_login=203395,
        mt5_password="password-en-claro",
        mt5_server="NYS",
    )

    guardado = (manager.settings.data_dir / "slots.json").read_text(encoding="utf-8")
    assert "password-en-claro" not in guardado
    assert json.loads(guardado)["slots"][0]["mt5_login"] == 203395


async def test_los_workers_no_arrancan_todos_a_la_vez(tmp_path: Path) -> None:
    """Tres terminales MT5 levantando a la vez se ahogan entre ellos.

    Visto en producción: al recuperar tres cuentas de golpe, la primera
    entraba y las otras dos morían con -10005 IPC timeout en el initialize.
    """
    import asyncio

    spawned: list = []
    manager = await _manager(tmp_path, spawned)
    for i, (uuid, login) in enumerate(
        [("a", 203395), ("b", 198812585), ("c", 198812927)]
    ):
        await manager.connect(
            account_id=uuid, mt5_login=login, mt5_password="x", mt5_server="S"
        )
    assert len(spawned) == 3

    # Reinicio con margen entre arranques.
    settings = _settings(tmp_path)
    settings.worker_spawn_stagger_sec = 0.05
    spawned.clear()
    reiniciado = SlotManager(settings)
    reiniciado.proc.start_worker = lambda **kw: (  # type: ignore[method-assign]
        spawned.append(kw) or 4242
    )
    reiniciado.terminal_ready = lambda slot_id: True  # type: ignore[method-assign]
    await reiniciado.start()

    # El primero sale enseguida; los demás esperan su turno.
    await asyncio.sleep(0.01)
    assert len(spawned) == 1

    await reiniciado._restore_task
    assert len(spawned) == 3
    if reiniciado._watch_task:
        reiniciado._watch_task.cancel()


async def test_el_watchdog_no_relanza_lo_que_espera_turno(tmp_path: Path) -> None:  # noqa: E501
    """Un slot en cola de recuperación no está caído: está esperando.

    Sin esto, el watchdog lo veía sin proceso, lo daba por muerto y lo
    arrancaba en paralelo con la recuperación: dos terminales del mismo slot
    lanzándose y matándose entre ellos.
    """
    spawned: list = []
    manager = await _manager(tmp_path, spawned)
    for uuid, login in [("a", 203395), ("b", 198812585), ("c", 198812927)]:
        await manager.connect(
            account_id=uuid, mt5_login=login, mt5_password="x", mt5_server="S"
        )

    settings = _settings(tmp_path)
    settings.worker_spawn_stagger_sec = 5  # los últimos siguen en cola
    reiniciado = SlotManager(settings)
    reiniciado.proc.start_worker = lambda **kw: 4242  # type: ignore[method-assign]
    reiniciado.proc.is_running = lambda slot_id: False  # type: ignore[method-assign]
    reiniciado.terminal_ready = lambda slot_id: True  # type: ignore[method-assign]
    await reiniciado.start()
    await asyncio.sleep(0.05)  # deja arrancar al primero

    assert reiniciado._pending_restore == {"Slot-02", "Slot-03"}
    # El watchdog no debe tocarlos aunque no tengan proceso vivo.
    for slot_id in ("Slot-02", "Slot-03"):
        assert reiniciado.slots[slot_id].restart_count == 0

    for task in (reiniciado._restore_task, reiniciado._watch_task):
        if task:
            task.cancel()


async def test_un_slots_json_con_bom_se_lee_igual(tmp_path: Path) -> None:
    """Windows mete un BOM con facilidad y el fichero se edita a mano.

    Visto en producción: tras editar slots.json con PowerShell
    (Set-Content -Encoding UTF8), el Manager arrancaba sin recuperar ninguna
    cuenta por un 'Unexpected UTF-8 BOM'.
    """
    spawned: list = []
    manager = await _manager(tmp_path, spawned)
    await manager.connect(
        account_id="uuid-nys", mt5_login=203395, mt5_password="x", mt5_server="S"
    )

    ruta = manager.settings.data_dir / "slots.json"
    ruta.write_text("﻿" + ruta.read_text(encoding="utf-8"), encoding="utf-8")

    spawned.clear()
    reiniciado = await _manager(tmp_path, spawned)

    assert len(spawned) == 1
    assert reiniciado.find_by_account("uuid-nys") is not None
