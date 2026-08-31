# MT5 Bridge

Reemplazo propio de MetaAPI: un **Bridge** que conecta cuentas MT5 reales
(propias, challenges, fondeadas) y emite eventos normalizados hacia el Core.

El usuario final **no instala EA**. Cada cuenta vive en un terminal MT5
portable (un slot). Preferir password **investor** (solo lectura).

## Arquitectura

```
Core  --HTTP comandos-->  Bridge Manager (FastAPI)
                              |  pool de slots
                              +-- Worker proceso  -->  MT5 portable Slot-01
                              +-- Worker proceso  -->  MT5 portable Slot-02
                              ...
Worker --POST JSON-->  Core /internal/bridge/events
```

Eventos: `connection.status`, `account.snapshot`, `position.opened`,
`position.updated`, `position.closed`, `trade.closed`.

## Requisitos

- Windows (el paquete `MetaTrader5` solo funciona en Windows).
- Python 3.11+.
- Terminales MT5 en modo portable, uno por slot.

## 1. Preparar un terminal MT5 portable

1. Descarga e instala MetaTrader 5 desde tu broker (o el instalador oficial).
2. Copia **toda** la carpeta de instalación a:

   `terminals/Slot-01/`

   Debe existir `terminals/Slot-01/terminal64.exe`.

3. Arranca una vez en portable y cierra:

   ```bat
   terminals\Slot-01\terminal64.exe /portable
   ```

   Eso crea `config/` y `bases/` dentro del slot.

4. **Entra una vez en una cuenta del bróker** (Archivo → Iniciar sesión en
   cuenta de operaciones). Vale una demo. Esto no es para operar: es para que
   el terminal deje de arrancar en el asistente de "abrir cuenta" y guarde la
   lista de servidores del bróker.

   Sin este paso, `mt5.initialize()` falla con `-10005 IPC timeout`: el
   terminal se queda esperando un clic y no atiende a la API.

5. Cierra el terminal. **A partir de aquí no se vuelve a tocar a mano**: el
   Worker hace `initialize` + `login` con las credenciales que manda el Core.

6. Para el resto de slots **no repitas nada de esto**. Deja esa carpeta como
   plantilla y clónala:

   ```bat
   ren terminals\Slot-01 _plantilla
   python -m app.tools.provision_slots --count 15
   ```

   Ver "Escalar el pool" más abajo.

## 2. Configurar variables

```bat
cd C:\Users\Pc\bridge
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Genera la clave Fernet y pégala en `.env`:

```bat
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Ajusta:

- `BRIDGE_API_KEY` — lo que el Core envía al Manager.
- `CORE_EVENTS_URL` — p.ej. `http://localhost:8000/internal/bridge/events`.
- `CORE_API_KEY` — lo que el Bridge envía al Core (`X-API-Key` y `Bearer`).
- `SLOT_COUNT` y `TERMINALS_ROOT`.

## 3. Arrancar el Bridge Manager

Desde la raíz del proyecto (`bridge/`):

```bat
.venv\Scripts\activate
uvicorn app.main:app --host 0.0.0.0 --port 8088
```

Health: `GET http://localhost:8088/api/v1/health`  
Docs: `http://localhost:8088/docs`

## 4. Conectar una cuenta de prueba

El Core (o curl) llama al Manager. Usa la **investor password** si el broker
la ofrece.

```bat
curl -X POST http://localhost:8088/api/v1/accounts/connect ^
  -H "Content-Type: application/json" ^
  -H "X-API-Key: cambia-esta-clave-bridge" ^
  -d "{\"account_id\":\"uuid-interno-del-core\",\"mt5_login\":12345678,\"mt5_password\":\"INVESTOR_PASSWORD\",\"mt5_server\":\"Broker-Server\",\"investor\":true}"
```

Listar slots:

```bat
curl http://localhost:8088/api/v1/slots -H "X-API-Key: cambia-esta-clave-bridge"
```

Desconectar:

```bat
curl -X POST http://localhost:8088/api/v1/accounts/disconnect ^
  -H "Content-Type: application/json" ^
  -H "X-API-Key: cambia-esta-clave-bridge" ^
  -d "{\"account_id\":\"uuid-interno-del-core\"}"
```

Logs por slot: `data/logs/Slot-01.log`.

## 5. Contrato de `trade.closed`

**Los cierres parciales esperan al cierre total.** El Worker agrupa los deals
por posición y solo emite cuando el volumen de salida cubre el de entrada. Si
sacas la mitad y dejas correr el resto, no sale nada todavía; al cerrar el
resto sale **un único** `trade.closed` con la operación completa: volumen total
de entrada, precio de apertura y de cierre promediados (el parcial incluido) y
el P&L sumado de todos los deals.

Es lo que evita que una misma operación aparezca troceada, pero tiene una
consecuencia que conviene conocer: quien va sacando parciales no ve nada en el
Core hasta cerrar el último trozo.

Si el Core solo registra operaciones cerradas y no quiere el libro de
abiertas, `EMIT_POSITION_EVENTS=false`. El seguimiento interno de posiciones
se mantiene igual —hace falta para emparejar los cierres—, simplemente no se
emiten `position.opened/updated/closed`.


El Worker agrupa deals por `position_id`. Cuando el volumen de salida cubre
el de entrada, emite un único `trade.closed` (cierres parciales esperan al
cierre total). `dealId` es el ticket del último deal OUT.

Por defecto **no** se reenvía el historial al conectar
(`REPLAY_HISTORY_ON_CONNECT=false`): se marca como visto y solo salen
cierres nuevos.

Al reconectar un Worker se vuelven a emitir `position.opened` de las
posiciones aún abiertas (el Core debe hacer upsert por `positionId`).

## 6. Probar el matcher sin MT5

```bat
pip install pytest
pytest tests/test_deal_matcher.py -q
```

## 7. Escalar el pool

Copiar un MT5 por slot a mano no escala: con 100 cuentas serían 100 copias y
100 logins manuales. Se configura **una** vez una plantilla y se clona.

```bat
python -m app.tools.provision_slots --count 20
python -m app.tools.provision_slots --count 80 --first 21   # ampliar después
python -m app.tools.provision_slots --count 100 --dry-run   # ver sin copiar
```

La plantilla es `terminals/_plantilla` (cámbiala con `--template`): una
instalación de MT5 arrancada una vez en portable y con una sesión iniciada,
tal como en el paso 1.

Del clon se excluye lo que cada terminal regenera solo (`bases`, `logs`,
`MQL5/Logs`) y el estado de un slot anterior (`worker_state.json`,
`slot_runtime.json`). Sin eso, cada copia arrastraría cientos de megas de
historial descargado y, peor, el estado de otro slot.

Es idempotente: un slot que ya tiene `terminal64.exe` se omite, así que se
puede relanzar para ampliar el pool sin tocar los que están conectados.
`--force` lo rehace y **borra su contenido**.

**Un terminal recién clonado sirve para cualquier bróker.** El Worker arranca
el terminal pasándole las credenciales al propio `initialize()`, así que nace
sabiendo a qué cuenta conectarse y no enseña el asistente de cuenta ni el
diálogo de contraseña. Un terminal parado en un diálogo no atiende al canal
IPC: eso es lo que producía los `-10005 IPC timeout` que parecían de red o de
credenciales.

Medido en un slot virgen: entra en **4,6 segundos**, contra los minutos que
tardaba en fallar el arranque en dos pasos. No hace falta preparar nada por
bróker ni entrar a mano en ningún slot, lo cual importa porque los brókers de
los clientes no se conocen de antemano.

Si por lo que sea ese arranque falla, el Worker cae al camino antiguo: lanzar
el terminal, engancharse y hacer el login aparte, con reintentos que toleran
que MT5 tire el canal al cambiar de bróker.

**Afinidad de bróker (opcional).** `provision_slots --server` deja un
`slot_broker.txt` en cada slot y el Manager prefiere asignar cada cuenta a un
slot de su bróker; el bróker se ve en `GET /slots`. Con el arranque de arriba
ya no es necesario — un slot sin marca vale para cualquier bróker — pero sigue
disponible por si algún día conviene dedicar slots.

## 8. Reinicios del Manager

Las cuentas asignadas se guardan en `data/slots.json` y **se recuperan solas
al arrancar**: el Manager relanza el Worker de cada una en su mismo slot.

Sin eso, reiniciar el Manager dejaba al Core creyendo que las cuentas seguían
conectadas mientras aquí no había ninguna: dejaban de llegar datos, el Core no
dejaba reconectar la misma cuenta por considerarla ya conectada, y desvincular
no encontraba nada que desvincular.

El password va cifrado con Fernet, igual que en memoria, así que `slots.json`
no contiene credenciales en claro. Aun así vive bajo `data/`, que está en el
`.gitignore` y no debe salir de la máquina.

Si una cuenta no se puede recuperar (se cambió `FERNET_KEY`, desapareció el
terminal del slot), ese slot queda en `error` con el motivo en `last_error`,
visible en `/slots`. Nunca en silencio.

**Los Workers arrancan de uno en uno**, con `WORKER_SPAWN_STAGGER_SEC` de
margen (25 s por defecto). Varios terminales MT5 levantando a la vez se ahogan
—cada uno sincroniza cientos de símbolos y compite por CPU y red— y el
`initialize` muere con `-10005 IPC timeout`. Con tres cuentas, la primera
entraba y las otras dos no levantaban nunca. Recuperar N cuentas tarda por
tanto unos N × 25 s; el Manager responde con normalidad mientras tanto.

## 9. Copias de seguridad

Del Bridge solo hay tres cosas irremplazables, y ninguna pesa:

| Qué | Por qué |
|-----|---------|
| `.env` | Contiene `FERNET_KEY`. **Sin ella `slots.json` es ilegible** y hay que reconectar cada cuenta pidiendo su contraseña otra vez. |
| `data/slots.json` | Qué cuenta ocupa cada slot. |
| La plantilla de terminal | Las listas de servidores de los brókers, que se añaden a mano una por una. |

Lo demás —`bases`, `logs`, los propios slots— lo regenera MT5 solo.

```bat
python -m app.tools.backup --dest D:\copias
python -m app.tools.backup --dest D:\copias --template C:\MT5-plantilla
python -m app.tools.backup --dest D:\copias --keep 14
```

`--keep N` conserva solo las N copias más recientes y borra el resto. Solo toca
carpetas `bridge-backup-*`, así que `--dest` puede ser una carpeta con más cosas
dentro. Sin `--keep` no se borra nada.

Para dejarlo diario y desatendido:

```powershell
.\scripts\install-backup-task.ps1 -Destino D:\copias-bridge -Hora 03:00 -Conservar 14
```

**La `FERNET_KEY` debe estar además en un gestor de contraseñas**, no solo en
el servidor y su copia: es el único fallo sin vuelta atrás. Y la copia
contiene esa clave en claro, así que guárdala como guardarías una contraseña —
en una carpeta local del servidor, nunca en una unidad de red ni sincronizada.

## 10. Arranque automático en el servidor

En un servidor el Bridge tiene que sobrevivir a un reinicio sin que nadie entre
por RDP. Se registra como **tarea programada al iniciar sesión**:

```powershell
.\scripts\install-bridge-task.ps1
Start-ScheduledTask -TaskName "Bridge Manager"
Get-Content .\logs\bridge-20260831.log -Tail 40 -Wait
```

### Por qué una tarea y no un servicio de Windows

Un servicio corre en la *sesión 0*, que no tiene escritorio. MT5 es una
aplicación gráfica y MetaQuotes no soporta ese modo; como el Bridge lanza un
terminal por cuenta, necesitamos una sesión interactiva de verdad.

El precio es que hace falta **inicio de sesión automático**, o tras un reinicio
no habrá ninguna sesión y la tarea no disparará. La forma correcta de
configurarlo es [Autologon de Sysinternals](https://learn.microsoft.com/sysinternals/downloads/autologon),
que guarda la contraseña como secreto LSA cifrado en vez de dejarla en texto
plano en el registro, que es lo que hace el método manual:

```powershell
.\Autologon.exe <usuario> <equipo> <contraseña>
```

Esto implica que quien tenga acceso físico o por consola al servidor entra al
escritorio sin credenciales. Es aceptable en un servidor dedicado con el RDP
restringido por firewall; no lo es en una máquina compartida.

### Cerrar RDP sin matar la sesión

Con la sesión abierta, **desconectar** (la X de la ventana de RDP) la deja viva
y el Bridge sigue corriendo. **Cerrar sesión** la destruye y se lleva por
delante el Bridge y todos los terminales. No uses "Cerrar sesión".

## 11. Qué falta antes de exponerlo a internet

| | Por qué |
|---|---|
| **TLS con dominio propio** | `/accounts/connect` lleva la contraseña MT5 en el cuerpo. En HTTP plano viaja legible. Let's Encrypt necesita un nombre de dominio, no vale una IP. |
| **Firewall en el 8088** | El puerto debe aceptar solo la IP del Core. |
| **Contraseñas de solo lectura** | Conectar con `investor: true` siempre que el bróker lo permita. |

## 12. Contrato de identidad de la cuenta

El `account_id` que el Core manda en `/accounts/connect` es **opaco para el
Bridge**: no se interpreta ni se transforma, solo se devuelve tal cual en el
`account_id` (y su alias `accountId`) de cada evento. El Bridge no puede
resolver cuentas por su cuenta, así que ese valor tiene que cumplir tres
cosas, y las tres son responsabilidad del Core:

1. **No nulo y no vacío.** Una cuenta sin identificador no puede recibir datos.
2. **Estable.** El mismo valor durante toda la vida de la cuenta: si cambia
   entre una conexión y la siguiente, el Core pierde el hilo de sus propios
   eventos.
3. **El mismo con el que el Core resuelve la cuenta al recibir el evento.** Si
   el Core envía un identificador y luego busca por otro (o por otra columna),
   los eventos llegan y se descartan.

Cada evento lleva además `mt5_login`, que sirve como **verificación**: si no
coincide con el de la cuenta que el Core resolvió, el evento es sospechoso y
conviene registrarlo en vez de aceptarlo.

Un Core que descarta eventos por no reconocer el `account_id` debe decirlo en
el cuerpo de la respuesta. El Bridge lo detecta aunque el status sea 200 (ver
`core_rejection` en `app/worker/event_emitter.py`), lo cuenta en
`emit.rejected` y lo deja en `last_error` de `/slots`.

## 13. "La cuenta conecta pero no cargan los stats"

`POST /accounts/connect` responde `ok` en cuanto asigna el slot y lanza el
Worker: **no** significa que el Core ya esté recibiendo datos. Los stats los
alimenta el evento `account.snapshot`, así que si no cargan, el corte está en
el camino Worker → Core. Para localizarlo:

**1. ¿Llega el Bridge al Core?**

En PowerShell (`curl` a secas es un alias de `Invoke-WebRequest` y no acepta
`-H` ni `-d`; hay que llamar al binario real con `curl.exe`):

```powershell
curl.exe -X POST http://localhost:8088/api/v1/core-ping `
  -H "X-API-Key: TU_BRIDGE_API_KEY" -d "{}"
```

O con el cmdlet nativo:

```powershell
Invoke-RestMethod -Uri "http://localhost:8088/api/v1/core-ping" `
  -Method POST `
  -Headers @{ "X-API-Key" = "TU_BRIDGE_API_KEY" } `
  -ContentType "application/json" `
  -Body "{}" | ConvertTo-Json
```

Devuelve el status y el cuerpo exactos del Core:

| Resultado | Causa |
|-----------|-------|
| `ConnectError` / `ConnectTimeout` | El Core no es alcanzable: DNS, firewall o TLS. Si la máquina sale por proxy, `CORE_HTTP_TRUST_ENV=true`. |
| `401` / `403` | `CORE_API_KEY` no coincide con la que espera el Core. |
| `404` | `CORE_EVENTS_URL` apunta a una ruta que no existe. |
| `2xx` con `core_rejection` | El Core recibe y **descarta**. Ver abajo. |
| `2xx` y `"ok": true` | El canal entero funciona → sigue en el paso 2. |

**Ojo con el 2xx que no procesa.** El Core responde `200` aunque tire el
evento:

```json
{"ok":true,"recibidos":1,"procesados":0,
 "detalle":[{"i":0,"type":"connection.status","ok":false,"error":"cuenta desconocida"}]}
```

Con el `account_id` de prueba eso es lo normal. Repite el ping con el
**account_id real** que el Core mandó en `/accounts/connect`:

```powershell
Invoke-RestMethod -Uri "http://localhost:8088/api/v1/core-ping" `
  -Method POST `
  -Headers @{ "X-API-Key" = "TU_BRIDGE_API_KEY" } `
  -ContentType "application/json" `
  -Body '{"account_id":"EL-UUID-REAL","mt5_login":12345678}' | ConvertTo-Json -Depth 5
```

Si con el id real sigue diciendo `cuenta desconocida`, el Core no reconoce el
mismo id que él envía al conectar: ahí está la causa de que no carguen los
stats, y se arregla en el Core, no en el Bridge. El Bridge ya no da esos
descartes por buenos — los cuenta en `emit.rejected` y los deja en
`last_error`.

**2. ¿Está emitiendo el Worker?**

```powershell
curl.exe http://localhost:8088/api/v1/slots/Slot-01 -H "X-API-Key: TU_BRIDGE_API_KEY"
```

El bloque `emit` dice lo que pasa con cada POST al Core:

- `sent: 0`, `rejected > 0` y `last_error` con el motivo del Core → llega
  pero se descarta (el caso de `cuenta desconocida`).
- `sent: 0`, `failed > 0` → los eventos no llegan siquiera (red, clave, ruta).
- `last_snapshot_at` reciente y `last_status: 200` → el Bridge sí está
  mandando los stats; entonces el fallo está en **cómo el Core mapea `data`**
  de `account.snapshot`.
- todo a cero y `status: connecting` → el login MT5 aún no terminó; mira
  `data/logs/Slot-01.log`.

**3. Contrato de `account.snapshot`.** El Bridge manda las claves en las dos
grafías, así que el Core puede leer cualquiera de las dos:

```json
{
  "event": "account.snapshot",
  "account_id": "uuid-del-core",
  "mt5_login": 12345678,
  "timestamp": "2026-08-23T21:00:00Z",
  "data": {
    "balance": 10000.0, "equity": 10120.5, "margin": 250.0,
    "free_margin": 9870.5, "freeMargin": 9870.5,
    "margin_level": 4048.2, "marginLevel": 4048.2,
    "profit": 120.5, "currency": "USD", "leverage": 100,
    "name": "Mi Cuenta", "server": "Broker-Server"
  }
}
```

**4. Probar sin tocar el Core.** Apunta el Bridge a su propio sink y mira los
eventos crudos en `data/events.jsonl`:

```
CORE_EVENTS_URL=http://127.0.0.1:8088/api/v1/debug/events
```

## API Manager

| Método | Ruta | Descripción |
|--------|------|-------------|
| GET | `/api/v1/health` | Liveness |
| GET | `/api/v1/slots` | Pool |x|
| GET | `/api/v1/slots/{id}` | Detalle |
| POST | `/api/v1/accounts/connect` | Asigna slot y arranca Worker |
| POST | `/api/v1/accounts/disconnect` | Mata Worker + terminal, libera slot (idempotente) |
| POST | `/api/v1/slots/{id}/restart` | Reinicio limpio |
| POST | `/api/v1/core-ping` | Diagnóstico del canal Bridge → Core |

Todas las rutas salvo `health` requieren `X-API-Key` o `Authorization: Bearer`.
