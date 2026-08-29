"""Copia de seguridad de lo que no se puede regenerar.

Del Bridge solo hay tres cosas irremplazables, y ninguna pesa:

  .env             la FERNET_KEY, sin la cual slots.json es ilegible y hay que
                   reconectar cada cuenta pidiendo su contraseña otra vez
  data/slots.json  qué cuenta ocupa cada slot
  la plantilla     las listas de servidores de los brókers, que se añaden a
                   mano una por una (opcional, con --template)

Lo demás —bases, logs, los propios slots— lo regenera MT5 solo.

    python -m app.tools.backup --dest D:\\copias
    python -m app.tools.backup --dest D:\\copias --template C:\\MT5-plantilla

OJO: la copia contiene la FERNET_KEY en claro. Guárdala donde guardarías una
contraseña, no en una carpeta compartida.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

#: Lo pequeño e irremplazable. Sin esto no se recupera nada.
ESENCIALES = (".env", "data/slots.json")

#: Lo que MT5 regenera solo; copiarlo multiplica el tamaño sin aportar.
PLANTILLA_IGNORA = {"bases", "logs", "tester", "history"}


def _ignora_plantilla(directory: str, names: list[str]) -> set[str]:
    parent = Path(directory).name.lower()
    fuera = set()
    for name in names:
        lowered = name.lower()
        if (Path(directory) / name).is_dir():
            if lowered in PLANTILLA_IGNORA or (parent == "mql5" and lowered == "logs"):
                fuera.add(name)
        elif lowered.endswith(".log"):
            fuera.add(name)
    return fuera


def backup(
    root: Path, dest: Path, *, template: Path | None = None, marca: str | None = None
) -> Path:
    """Copia lo esencial a dest/bridge-backup-<fecha>. Devuelve esa carpeta."""
    marca = marca or datetime.now().strftime("%Y%m%d-%H%M%S")
    destino = dest / f"bridge-backup-{marca}"
    destino.mkdir(parents=True, exist_ok=True)

    copiados: list[str] = []
    faltan: list[str] = []
    for relativo in ESENCIALES:
        origen = root / relativo
        if not origen.is_file():
            faltan.append(relativo)
            continue
        final = destino / relativo
        final.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(origen, final)
        copiados.append(relativo)

    for relativo in copiados:
        print(f"  guardado  {relativo}")
    for relativo in faltan:
        # slots.json no existe hasta la primera conexión: no es un error.
        print(f"  NO EXISTE {relativo}")

    if template is not None:
        if not (template / "terminal64.exe").is_file():
            print(f"  OJO: {template} no parece una plantilla (sin terminal64.exe)")
        else:
            print(f"  copiando la plantilla desde {template} (tarda)...")
            shutil.copytree(
                template, destino / "plantilla", ignore=_ignora_plantilla
            )
            print("  guardado  plantilla")

    return destino


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Copia de seguridad de la configuración del Bridge."
    )
    parser.add_argument(
        "--dest", type=Path, required=True, help="Dónde dejar la copia."
    )
    parser.add_argument(
        "--root", type=Path, default=Path("."), help="Raíz del proyecto (por defecto .)"
    )
    parser.add_argument(
        "--template",
        type=Path,
        default=None,
        help="Incluir también la plantilla de terminal (p.ej. C:\\MT5-plantilla).",
    )
    args = parser.parse_args(argv)

    if not (args.root / "app").is_dir():
        parser.error(f"{args.root.resolve()} no parece la raíz del Bridge")

    destino = backup(args.root, args.dest, template=args.template)

    total = sum(f.stat().st_size for f in destino.rglob("*") if f.is_file())
    print()
    print(f"Copia en: {destino}  ({total / 1_048_576:.1f} MB)")
    print()
    print("Contiene la FERNET_KEY en claro: guárdala como guardarías una")
    print("contraseña. Y ten la clave también en tu gestor de contraseñas, no")
    print("solo aquí: si pierdes la copia Y el servidor, no hay vuelta atrás.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
