"""Copia de seguridad: lo irremplazable entra, lo regenerable no."""

from __future__ import annotations

from pathlib import Path

from app.tools.backup import backup


def _proyecto(base: Path) -> Path:
    root = base / "Bridge"
    (root / "app").mkdir(parents=True)
    (root / "data").mkdir()
    (root / ".env").write_text("FERNET_KEY=abc\nBRIDGE_API_KEY=xyz\n", encoding="utf-8")
    (root / "data" / "slots.json").write_text('{"slots":[]}', encoding="utf-8")
    (root / "data" / "logs").mkdir()
    (root / "data" / "logs" / "Slot-01.log").write_text("ruido", encoding="utf-8")
    return root


def test_guarda_lo_irremplazable(tmp_path: Path) -> None:
    root = _proyecto(tmp_path)

    destino = backup(root, tmp_path / "copias", marca="prueba")

    assert (destino / ".env").read_text(encoding="utf-8").startswith("FERNET_KEY=")
    assert (destino / "data" / "slots.json").is_file()
    # Los logs se regeneran: no tienen por qué estar.
    assert not (destino / "data" / "logs").exists()


def test_sin_slots_json_no_falla(tmp_path: Path) -> None:
    """No existe hasta la primera conexión; no es motivo para no copiar el .env."""
    root = _proyecto(tmp_path)
    (root / "data" / "slots.json").unlink()

    destino = backup(root, tmp_path / "copias", marca="prueba")

    assert (destino / ".env").is_file()
    assert not (destino / "data" / "slots.json").exists()


def test_la_plantilla_va_sin_lo_que_mt5_regenera(tmp_path: Path) -> None:
    root = _proyecto(tmp_path)
    plantilla = tmp_path / "MT5-plantilla"
    (plantilla / "config").mkdir(parents=True)
    (plantilla / "bases" / "Exness").mkdir(parents=True)
    (plantilla / "logs").mkdir()
    (plantilla / "terminal64.exe").write_text("exe", encoding="utf-8")
    (plantilla / "config" / "servers.dat").write_text("brokers", encoding="utf-8")
    (plantilla / "bases" / "Exness" / "h.hcc").write_text("x" * 1000, encoding="utf-8")

    destino = backup(root, tmp_path / "copias", template=plantilla, marca="prueba")

    assert (destino / "plantilla" / "config" / "servers.dat").is_file()
    assert not (destino / "plantilla" / "bases").exists()
    assert not (destino / "plantilla" / "logs").exists()


def test_cada_copia_va_a_su_carpeta(tmp_path: Path) -> None:
    root = _proyecto(tmp_path)

    una = backup(root, tmp_path / "copias", marca="20260829-2300")
    otra = backup(root, tmp_path / "copias", marca="20260830-2300")

    assert una != otra
    assert una.is_dir() and otra.is_dir()
