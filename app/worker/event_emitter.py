"""POST de eventos JSON al Core."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.models.events import BridgeEvent

logger = logging.getLogger(__name__)


class EventEmitter:
    def __init__(
        self,
        url: str,
        api_key: str,
        timeout_sec: float = 15.0,
    ) -> None:
        self._url = url
        self._api_key = api_key
        # trust_env=False: no usar HTTP_PROXY de Windows para localhost.
        self._client = httpx.Client(timeout=timeout_sec, trust_env=False)

    def emit(self, event: BridgeEvent) -> None:
        payload = event.model_dump()
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
            if response.status_code >= 400:
                logger.error(
                    "Core rechazó %s status=%s body=%s",
                    event.event,
                    response.status_code,
                    response.text[:500],
                )
            else:
                logger.info("emitido %s", event.event)
        except httpx.HTTPError as exc:
            # El Worker sigue vivo; el Core puede no estar levantado aún.
            logger.warning(
                "Core no alcanzó (%s) evento=%s url=%s: %s",
                type(exc).__name__,
                event.event,
                self._url,
                exc,
            )

    def emit_raw(self, payload: dict[str, Any]) -> None:
        self.emit(BridgeEvent.model_validate(payload))

    def close(self) -> None:
        self._client.close()
