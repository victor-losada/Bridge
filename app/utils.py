"""Utilidades compartidas (tiempo UTC, volumen, logs)."""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from typing import Any


VOLUME_EPS = 1e-6


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_iso_z(dt: datetime | None = None) -> str:
    """ISO-8601 en UTC con sufijo Z, sin microsegundos."""
    if dt is None:
        dt = utc_now()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def mt5_time_to_utc(value: Any) -> datetime:
    """Convierte time/time_msc de MT5 a datetime UTC.

    MetaTrader5 suele devolver epoch en segundos (int) o datetime naive
    interpretado como reloj del terminal (típicamente UTC en brokers serios).
    """
    if value is None:
        return utc_now()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        # time_msc viene en milisegundos
        ts = float(value)
        if ts > 10_000_000_000:
            ts = ts / 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    return utc_now()


def volumes_equal(a: float, b: float) -> bool:
    return abs(a - b) < VOLUME_EPS


def setup_logging(slot_id: str | None = None) -> None:
    fmt = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
    if slot_id:
        fmt = f"%(asctime)s | %(levelname)-7s | {slot_id} | %(name)s | %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        datefmt="%Y-%m-%dT%H:%M:%SZ",
        stream=sys.stdout,
        force=True,
    )
    logging.Formatter.converter = lambda *args: datetime.now(timezone.utc).timetuple()
