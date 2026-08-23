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

## API Manager

| Método | Ruta | Descripción |
|--------|------|-------------|
| GET | `/api/v1/health` | Liveness |
| GET | `/api/v1/slots` | Pool |x|
| GET | `/api/v1/slots/{id}` | Detalle |
| POST | `/api/v1/accounts/connect` | Asigna slot y arranca Worker |
| POST | `/api/v1/accounts/disconnect` | Mata Worker + terminal, libera slot |
| POST | `/api/v1/slots/{id}/restart` | Reinicio limpio |

Todas las rutas salvo `health` requieren `X-API-Key` o `Authorization: Bearer`.
