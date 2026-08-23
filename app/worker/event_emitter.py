"""POST de eventos JSON al Core, con reintentos y estadísticas.

El Core es el único consumidor. Si un evento no llega, la cuenta aparece
"conectada" en el Core pero sin datos, así que aquí se registra SIEMPRE el
resultado del POST (status y cuerpo) y se expone vía `stats()` para que el
Manager lo publique en /slots.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

from app.models.events import BridgeEvent
from app.utils import to_iso_z

logger = logging.getLogger(__name__)

# 4xx que sí merecen reintento; el resto son errores de contrato/credencial.
_RETRYABLE_CLIENT_CODES = {408, 425, 429}


def core_rejection(body: str) -> str | None:
    """Motivo por el que el Core NO procesó un evento que aceptó con 2xx.

    El Core responde 200 aunque descarte el evento (p.ej. `account_id` que no
    reconoce). Sin mirar el cuerpo, un canal roto parece sano:

        {"ok":true,"recibidos":1,"procesados":0,
         "detalle":[{"i":0,"type":"...","ok":false,"error":"cuenta desconocida"}]}

    Devuelve None si el Core sí lo procesó (o si la respuesta no es JSON
    reconocible: en ese caso no inventamos un fallo).
    """
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None

    problems: list[str] = []
    detalle = payload.get("detalle")
    if isinstance(detalle, list):
        for item in detalle:
            if isinstance(item, dict) and item.get("ok") is False:
                problems.append(str(item.get("error") or "sin detalle"))

    if not problems and payload.get("ok") is False:
        problems.append(
            str(payload.get("error") or payload.get("message") or "el Core marcó ok=false")
        )

    if not problems:
        recibidos = payload.get("recibidos")
        procesados = payload.get("procesados")
        if isinstance(recibidos, int) and isinstance(procesados, int):
            if recibidos > 0 and procesados == 0:
                problems.append(f"recibidos={recibidos} procesados=0")

    return "; ".join(problems) if problems else None


@dataclass
class EmitStats:
    """Foto del canal Worker → Core; viaja a slot_runtime.json."""

    sent: int = 0
    failed: int = 0
    # Entregados con 2xx pero descartados por el Core.
    rejected: int = 0
    last_event: str | None = None
    last_ok_at: str | None = None
    last_status: int | None = None
    last_error: str | None = None
    last_error_at: str | None = None
    last_snapshot_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class EventEmitter:
    def __init__(
        self,
        url: str,
        api_key: str,
        timeout_sec: float = 15.0,
        *,
        max_attempts: int = 3,
        backoff_sec: float = 2.0,
        trust_env: bool = False,
    ) -> None:
        self._url = url
        self._api_key = api_key
        self._max_attempts = max(1, max_attempts)
        self._backoff_sec = backoff_sec
        # trust_env=False: ignora HTTP_PROXY del sistema (localhost directo).
        # Ponlo a True si el Core solo es alcanzable a través del proxy.
        self._client = httpx.Client(timeout=timeout_sec, trust_env=trust_env)
        self._stats = EmitStats()

    @property
    def url(self) -> str:
        return self._url

    def stats(self) -> dict[str, Any]:
        return self._stats.as_dict()

    def emit(self, event: BridgeEvent) -> bool:
        """True solo si el Core PROCESÓ el evento.

        Un 2xx no basta: el Core puede aceptar el POST y descartar el evento
        (ver `core_rejection`). Esos descartes ya quedan contabilizados en
        `_post_once`, así que aquí solo se registra lo que ni siquiera llegó.
        """
        payload = event.to_payload()
        last_problem = "sin intentos"
        for attempt in range(1, self._max_attempts + 1):
            outcome, last_problem, delivered = self._post_once(event.event, payload)
            if outcome is True:
                return True
            if delivered:  # llegó pero el Core lo rechazó: ya está registrado
                return False
            if outcome is False:  # permanente: no reintentar
                break
            if attempt < self._max_attempts:
                time.sleep(self._backoff_sec * attempt)

        self._stats.failed += 1
        self._stats.last_error = last_problem[:300]
        self._stats.last_error_at = to_iso_z()
        return False

    def _post_once(
        self, event_name: str, payload: dict
    ) -> tuple[bool | None, str, bool]:
        """(True ok | False permanente | None reintentable, descripción, entregado).

        `entregado` distingue "el Core no lo recibió" de "lo recibió y lo tiró".
        """
        try:
            response = self._client.post(
                self._url,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-API-Key": self._api_key,
                    "Authorization": f"Bearer {self._api_key}",
                },
            )
        except httpx.HTTPError as exc:
            problem = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "Core inalcanzable evento=%s url=%s: %s", event_name, self._url, problem
            )
            return None, problem, False

        self._stats.last_status = response.status_code
        if response.status_code < 400:
            rejection = core_rejection(response.text)
            if rejection is not None:
                # Entregado pero descartado: reintentar no arregla nada.
                self._stats.rejected += 1
                self._stats.last_error = (
                    f"HTTP {response.status_code} pero el Core no lo procesó: {rejection}"
                )[:300]
                self._stats.last_error_at = to_iso_z()
                logger.error(
                    "Core descartó %s (status=%s): %s",
                    event_name,
                    response.status_code,
                    rejection,
                )
                return False, self._stats.last_error, True
            self._stats.sent += 1
            self._stats.last_event = event_name
            self._stats.last_ok_at = to_iso_z()
            if event_name == "account.snapshot":
                self._stats.last_snapshot_at = self._stats.last_ok_at
            logger.info("emitido %s status=%s", event_name, response.status_code)
            return True, "", True

        body = response.text[:500]
        problem = f"HTTP {response.status_code}: {body}"
        permanent = 400 <= response.status_code < 500 and (
            response.status_code not in _RETRYABLE_CLIENT_CODES
        )
        logger.error(
            "Core rechazó %s status=%s url=%s body=%s%s",
            event_name,
            response.status_code,
            self._url,
            body,
            "" if permanent else " (se reintenta)",
        )
        return (False if permanent else None), problem, False

    def emit_raw(self, payload: dict[str, Any]) -> bool:
        return self.emit(BridgeEvent.model_validate(payload))

    def close(self) -> None:
        self._client.close()
