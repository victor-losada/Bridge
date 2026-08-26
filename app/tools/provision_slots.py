"""Crea carpetas de slot clonando un terminal MT5 ya configurado.

Copiar a mano un MT5 por slot no escala: con 100 cuentas son 100 copias y 100
logins manuales. En su lugar se configura UNA vez una plantilla y se clona.

    # 1. Prepara la plantilla (una sola vez, a mano):
    #    - copia una instalación de MT5 en terminals/_plantilla
    #    - arráncala con /portable, cierra el asistente, entra en una cuenta
    #      demo del bróker (para que quede la lista de servidores) y ciérrala
    # 2. Clona:
    python -m app.tools.provision_slots --count 20

Lo que NO se copia: el historial descargado (`bases`), los logs y el estado
de un slot anterior. Son cientos de megas por copia, MT5 los regenera solo, y
arrastrar el estado de otro slot es justo lo que no se quiere.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

# Directorios que cada terminal regenera por su cuenta. Copiarlos multiplica
# el tamaño del clon sin aportar nada.
SKIP_DIRS = {"bases", "logs", "tester", "history"}

# Ficheros de estado: pertenecen al slot que los escribió, nunca al clon.
SKIP_FILES = {"slot_runtime.json", "worker_state.json"}

SKIP_SUFFIXES = {".log"}


def _ignore(directory: str, names: list[str]) -> set[str]:
    parent = Path(directory).name.lower()
    ignored: set[str] = set()
    for name in names:
        lowered = name.lower()
        full = Path(directory) / name
        if full.is_dir():
            # 'Logs' cuelga tanto de la raíz como de MQL5/
            if lowered in SKIP_DIRS or (parent == "mql5" and lowered == "logs"):
                ignored.add(name)
        elif lowered in SKIP_FILES or Path(lowered).suffix in SKIP_SUFFIXES:
            ignored.add(name)
    return ignored


def _dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


#: Marca qué bróker conoce el terminal de un slot. Un clon solo arranca sin
#: diálogos si recibe una cuenta del mismo bróker que la plantilla.
BROKER_MARKER = "slot_broker.txt"


def provision(
    template: Path,
    terminals_root: Path,
    count: int,
    *,
    first: int = 1,
    force: bool = False,
    dry_run: bool = False,
    server: str = "",
) -> tuple[list[str], list[str]]:
    """Clona la plantilla en Slot-XX. Devuelve (creados, omitidos)."""
    template = template.resolve()
    if not (template / "terminal64.exe").is_file():
        raise SystemExit(
            f"La plantilla {template} no tiene terminal64.exe.\n"
            "Copia ahí una instalación de MT5 y arráncala una vez con /portable."
        )

    terminals_root.mkdir(parents=True, exist_ok=True)
    created: list[str] = []
    skipped: list[str] = []

    for i in range(first, first + count):
        slot_id = f"Slot-{i:02d}"
        destination = terminals_root / slot_id
        if (destination / "terminal64.exe").is_file() and not force:
            skipped.append(slot_id)
            continue
        if dry_run:
            created.append(slot_id)
            continue
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(template, destination, ignore=_ignore)
        if server:
            (destination / BROKER_MARKER).write_text(server, encoding="utf-8")
        created.append(slot_id)
        print(f"  {slot_id} listo{f' [{server}]' if server else ''}")

    return created, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Clona un terminal MT5 configurado en carpetas de slot."
    )
    parser.add_argument(
        "--template",
        type=Path,
        default=Path("terminals/_plantilla"),
        help="Carpeta con el MT5 ya configurado (por defecto terminals/_plantilla).",
    )
    parser.add_argument(
        "--terminals-root",
        type=Path,
        default=Path("terminals"),
        help="Dónde se crean los Slot-XX (por defecto terminals).",
    )
    parser.add_argument("--count", type=int, required=True, help="Cuántos slots crear.")
    parser.add_argument(
        "--first", type=int, default=1, help="Número del primer slot (por defecto 1)."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Rehace los slots que ya tengan terminal64.exe. Borra su contenido.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Enseña qué haría, sin copiar nada."
    )
    parser.add_argument(
        "--server",
        default="",
        help=(
            "Servidor MT5 con el que se configuró la plantilla, p.ej. "
            "Exness-MT5Trial11. Se anota en el slot para que el Manager le "
            "asigne cuentas de ese bróker."
        ),
    )
    args = parser.parse_args(argv)

    if args.count < 1:
        parser.error("--count debe ser 1 o más")

    template_size = _dir_size(args.template) if args.template.is_dir() else 0
    print(f"Plantilla : {args.template}  ({template_size / 1_048_576:.0f} MB a clonar)")
    print(f"Destino   : {args.terminals_root}")
    if args.dry_run:
        print("(dry-run: no se copia nada)")

    created, skipped = provision(
        args.template,
        args.terminals_root,
        args.count,
        first=args.first,
        force=args.force,
        dry_run=args.dry_run,
        server=args.server,
    )

    print()
    print(f"Creados : {len(created)}")
    if skipped:
        print(f"Omitidos: {len(skipped)} (ya tenían terminal64.exe; usa --force)")
    if created and not args.dry_run:
        total = template_size * len(created) / 1_073_741_824
        print(f"Ocupado : ~{total:.1f} GB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
