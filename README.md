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

4. Repite la copia completa para `Slot-02`, `Slot-03`, … (no uses shortcuts
   ni la misma carpeta para dos slots). Con `SLOT_COUNT=15` prepara 15 copias.

5. No hace falta loguear la cuenta a mano: el Worker hace `initialize` + `login`.

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

## 7. "La cuenta conecta pero no cargan los stats"

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
| POST | `/api/v1/accounts/disconnect` | Mata Worker + terminal, libera slot |
| POST | `/api/v1/slots/{id}/restart` | Reinicio limpio |
| POST | `/api/v1/core-ping` | Diagnóstico del canal Bridge → Core |

Todas las rutas salvo `health` requieren `X-API-Key` o `Authorization: Bearer`.
