# Terminales MT5 portables

Cada carpeta `Slot-XX` debe contener una copia independiente del terminal
MetaTrader 5 en modo portable (`/portable`).

Mínimo requerido:
  Slot-01/terminal64.exe

Pasos rápidos:
  1. Instala MT5 en una carpeta temporal o usa una instalación existente.
  2. Copia TODO el directorio del terminal a `_plantilla/`.
  3. Arranca una vez: `terminal64.exe /portable`, cierra el asistente, entra
     en una cuenta del bróker (vale una demo) y cierra el terminal.
     Sin ese login la API falla con -10005 IPC timeout.
  4. Clona los slots: `python -m app.tools.provision_slots --count 15`
     (cada slot = copia completa, no un acceso directo).

No compartas `terminal64.exe` entre slots: el paquete MetaTrader5
ata un proceso Python a un path de terminal.
