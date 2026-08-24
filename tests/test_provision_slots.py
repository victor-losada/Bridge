"""Clonado de slots desde una plantilla configurada."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.tools.provision_slots import provision

pytest_plugins = ("pytest_asyncio",)


def _plantilla(base: Path) -> Path:
    """Imita un MT5 portable ya arrancado una vez."""
    root = base / "_plantilla"
    (root / "config").mkdir(parents=True)
    (root / "bases" / "Exness").mkdir(parents=True)
    (root / "logs").mkdir()
    (root / "MQL5" / "Logs").mkdir(parents=True)
    (root / "MQL5" / "Experts").mkdir()
    (root / "terminal64.exe").write_text("exe")
    (root / "config" / "servers.dat").write_text("lista de servidores")
    (root / "config" / "accounts.dat").write_text("cuenta guardada")
    (root / "bases" / "Exness" / "hist.hcc").write_text("x" * 5000)
    (root / "logs" / "20260823.log").write_text("log")
    (root / "MQL5" / "Logs" / "20260823.log").write_text("log")
    (root / "MQL5" / "Experts" / "ea.ex5").write_text("ea")
    (root / "worker_state.json").write_text('{"emitted":[1]}')
    (root / "slot_runtime.json").write_text('{"status":"connected"}')
    return root


def test_clona_la_configuracion_y_descarta_lo_regenerable(tmp_path: Path) -> None:
    root = tmp_path / "terminals"
    creados, omitidos = provision(_plantilla(tmp_path), root, 3)

    assert creados == ["Slot-01", "Slot-02", "Slot-03"]
    assert omitidos == []

    slot = root / "Slot-02"
    # Lo que evita el asistente de primer arranque y el login manual:
    assert (slot / "terminal64.exe").is_file()
    assert (slot / "config" / "servers.dat").read_text() == "lista de servidores"
    assert (slot / "MQL5" / "Experts" / "ea.ex5").is_file()
    # Lo que MT5 regenera solo, o pertenece a otro slot:
    assert not (slot / "bases").exists()
    assert not (slot / "logs").exists()
    assert not (slot / "MQL5" / "Logs").exists()
    assert not (slot / "worker_state.json").exists()
    assert not (slot / "slot_runtime.json").exists()


def test_no_pisa_un_slot_ya_provisionado(tmp_path: Path) -> None:
    """Relanzarlo para ampliar el pool no puede tocar los slots en marcha."""
    plantilla = _plantilla(tmp_path)
    root = tmp_path / "terminals"
    provision(plantilla, root, 2)
    (root / "Slot-01" / "worker_state.json").write_text('{"emitted":[99]}')

    creados, omitidos = provision(plantilla, root, 5)

    assert creados == ["Slot-03", "Slot-04", "Slot-05"]
    assert omitidos == ["Slot-01", "Slot-02"]
    assert (root / "Slot-01" / "worker_state.json").read_text() == '{"emitted":[99]}'


def test_force_rehace_el_slot(tmp_path: Path) -> None:
    plantilla = _plantilla(tmp_path)
    root = tmp_path / "terminals"
    provision(plantilla, root, 1)
    (root / "Slot-01" / "worker_state.json").write_text("basura")

    creados, _ = provision(plantilla, root, 1, force=True)

    assert creados == ["Slot-01"]
    assert not (root / "Slot-01" / "worker_state.json").exists()


def test_permite_ampliar_desde_un_numero(tmp_path: Path) -> None:
    root = tmp_path / "terminals"
    creados, _ = provision(_plantilla(tmp_path), root, 2, first=21)
    assert creados == ["Slot-21", "Slot-22"]


def test_plantilla_sin_terminal_falla_pronto(tmp_path: Path) -> None:
    vacia = tmp_path / "vacia"
    vacia.mkdir()
    with pytest.raises(SystemExit, match="terminal64.exe"):
        provision(vacia, tmp_path / "terminals", 1)


def test_dry_run_no_escribe(tmp_path: Path) -> None:
    root = tmp_path / "terminals"
    creados, _ = provision(_plantilla(tmp_path), root, 3, dry_run=True)
    assert creados == ["Slot-01", "Slot-02", "Slot-03"]
    assert not (root / "Slot-01").exists()


# --- Desvincular es idempotente --------------------------------------------

@pytest.mark.asyncio
async def test_desconectar_una_cuenta_que_no_esta_no_es_error(tmp_path: Path) -> None:
    """El Manager guarda los slots en memoria: tras reiniciarlo, el Core pide
    desvincular cuentas que aquí ya no existen. Devolver 404 dejaba al Core
    sin poder desvincular nada."""
    from app.config import Settings
    from app.manager.slot_manager import SlotManager

    settings = Settings(
        bridge_api_key="x" * 16,
        core_api_key="y" * 8,
        fernet_key="Ky1DFHTvPX2CjJKcgTLdmB2fF2ZQK5Xz3mFvMHVGLJU=",
        slot_count=2,
        terminals_root=tmp_path / "terminals",
        data_dir=tmp_path / "data",
    )
    manager = SlotManager(settings)
    manager.slots = {}
    await manager.start()
    manager._watch_task.cancel()

    assert await manager.disconnect(account_id="uuid-que-no-esta") is None
