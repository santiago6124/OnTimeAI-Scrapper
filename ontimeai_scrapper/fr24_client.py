"""Wrapper sobre la librería FlightRadarAPI (Capa 1 del harvester).

Encapsula throttle, backoff, jitter, paginación y normalización al schema
de OnTimeAI-Backend (flights/actuals).

Pin de versión: requirements.txt fija FlightRadarAPI==1.5.1. NO auto-upgrade.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

try:
    from FlightRadarAPI import FlightRadar24API
    from FlightRadarAPI.request import APIClient
except ImportError:  # fallback al import legacy
    from FlightRadar24 import FlightRadar24API  # type: ignore[no-redef]
    APIClient = None  # type: ignore[assignment]

from . import config

log = logging.getLogger(__name__)


# -- Excepciones ---------------------------------------------------------------


class FR24Error(Exception):
    """Error genérico del cliente FR24."""


class FR24RateLimitedError(FR24Error):
    """403/429 — Cloudflare bot detection o rate-limit del API."""


class FR24EmptyResponseError(FR24Error):
    """Respuesta sin vuelos (puede ser fin de paginación o downtime)."""


# -- Cliente -------------------------------------------------------------------


@dataclass
class FR24Page:
    arrivals: list[dict[str, Any]]
    departures: list[dict[str, Any]]
    page: int
    has_more: bool

    @property
    def total(self) -> int:
        return len(self.arrivals) + len(self.departures)


class FR24Client:
    """Sesión única reutilizable. Aplica throttle global entre calls."""

    def __init__(
        self,
        *,
        throttle_seconds: float = config.FR24_THROTTLE_SECONDS,
        jitter_seconds: float = config.FR24_THROTTLE_JITTER_SECONDS,
        max_retries: int = config.FR24_MAX_RETRIES,
    ) -> None:
        self._api = FlightRadar24API()
        self._throttle = throttle_seconds
        self._jitter = jitter_seconds
        self._max_retries = max_retries
        self._last_call_at: float = 0.0

    @property
    def api_client(self):
        """Acceso al APIClient interno (curl_cffi + chrome136) para reuso de cookies/TLS."""
        # name-mangling: FlightRadar24API guarda el cliente como __client
        return getattr(self._api, "_FlightRadar24API__client", None)

    def throttled_call(self, name: str, fn, *args, **kwargs):
        """Expone el wrapper de throttle+backoff a otros módulos (ej. fr24_http)."""
        return self._call_with_backoff(name, fn, *args, **kwargs)

    # -- internal --

    def _wait_throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call_at
        delay = self._throttle - elapsed
        if delay > 0:
            time.sleep(delay + random.uniform(0, self._jitter))
        self._last_call_at = time.monotonic()

    def _call_with_backoff(self, name: str, fn, *args, **kwargs):
        last_exc: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            self._wait_throttle()
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 — lib levanta builtin Exception
                last_exc = exc
                msg = str(exc).lower()
                if "403" in msg or "429" in msg or "blocked" in msg or "cloudflare" in msg:
                    backoff = (2 ** attempt) + random.uniform(0, 1)
                    log.warning("%s rate-limited (attempt %d/%d) — backoff %.1fs",
                                name, attempt, self._max_retries, backoff)
                    time.sleep(backoff)
                    continue
                raise FR24Error(f"{name} failed: {exc}") from exc
        raise FR24RateLimitedError(f"{name} exhausted retries: {last_exc}")

    # -- API pública --

    def get_airport_page(
        self,
        airport_code: str,
        *,
        page: int = 1,
        flight_limit: int = 100,
    ) -> FR24Page:
        """Una página de arrivals + departures."""
        details = self._call_with_backoff(
            f"get_airport_details({airport_code}, page={page})",
            self._api.get_airport_details,
            code=airport_code,
            flight_limit=flight_limit,
            page=page,
        )
        try:
            sched = details["airport"]["pluginData"]["schedule"]
            arrivals = (sched.get("arrivals") or {}).get("data") or []
            departures = (sched.get("departures") or {}).get("data") or []
            arr_meta = (sched.get("arrivals") or {})
            dep_meta = (sched.get("departures") or {})
        except (KeyError, TypeError) as exc:
            raise FR24Error(f"shape inesperado: {exc}") from exc

        arr_total = (arr_meta.get("item") or {}).get("total") or len(arrivals)
        dep_total = (dep_meta.get("item") or {}).get("total") or len(departures)
        has_more = len(arrivals) + len(departures) < (arr_total + dep_total)

        return FR24Page(
            arrivals=[a.get("flight") or {} for a in arrivals if isinstance(a, dict)],
            departures=[d.get("flight") or {} for d in departures if isinstance(d, dict)],
            page=page,
            has_more=has_more,
        )

    def iter_airport_flights(
        self,
        airport_code: str,
        *,
        max_pages: int = 7,
        flight_limit: int = 100,
    ) -> Iterator[FR24Page]:
        """Itera páginas hasta agotar o tocar max_pages. Each yield es FR24Page."""
        for page in range(1, max_pages + 1):
            try:
                p = self.get_airport_page(airport_code, page=page, flight_limit=flight_limit)
            except FR24EmptyResponseError:
                break
            yield p
            if not p.has_more or p.total == 0:
                break


# -- Normalización a schema flights/actuals ------------------------------------


def _epoch_to_iso(epoch: int | float | None) -> str | None:
    if epoch is None or epoch == 0:
        return None
    try:
        dt = datetime.fromtimestamp(int(epoch), tz=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    except (ValueError, OSError):
        return None


def _local_date_and_min(epoch: int | float | None, tz_offset_seconds: int | None) -> tuple[str | None, int | None]:
    """Devuelve (fl_date_local, crs_dep_min_local). tz_offset en segundos (-14400 = EDT)."""
    if epoch is None or epoch == 0:
        return None, None
    try:
        offset = int(tz_offset_seconds or 0)
        local_dt = datetime.fromtimestamp(int(epoch) + offset, tz=timezone.utc)
        fl_date = local_dt.strftime("%Y-%m-%d")
        crs_dep_min = local_dt.hour * 60 + local_dt.minute
        return fl_date, crs_dep_min
    except (ValueError, OSError):
        return None, None


def _parse_flight_number(ident_iata: str | None) -> str | None:
    if not ident_iata:
        return None
    digits = "".join(ch for ch in ident_iata if ch.isdigit())
    return digits or None


def _status_flags(flight: dict[str, Any]) -> tuple[int | None, int | None]:
    status = (flight.get("status") or {})
    text = str(status.get("text") or "").lower()
    generic_status = ((status.get("generic") or {}).get("status") or {})
    diverted = 1 if ("divert" in text or generic_status.get("diverted")) else 0
    cancelled = 1 if ("cancel" in text or generic_status.get("type") == "cancellation") else 0
    return cancelled, diverted


def normalize_flight(
    flight: dict[str, Any],
    *,
    anchor_airport: str | None = None,
    is_arrival_side: bool | None = None,
) -> dict[str, Any] | None:
    """Convierte el dict anidado de FR24 al schema flat de la tabla flights.

    Args:
        flight: el dict bajo `data[].flight` del response de FR24.
        anchor_airport: código del aeropuerto-anchor (ej. KATL/ATL) cuando viene de Capa 1.
            Se usa para inferir el código que FR24 omite (dest en arrivals, origin en departures).
        is_arrival_side: si está scoped a anchor: True=anchor es destino, False=anchor es origen.

    Devuelve None si el flight no tiene fa_flight_id usable.
    """
    ident = flight.get("identification") or {}
    fa_flight_id = ident.get("id")
    if not fa_flight_id:
        return None

    aircraft = flight.get("aircraft") or {}
    airline = flight.get("airline") or {}
    airport = flight.get("airport") or {}
    origin = airport.get("origin") or {}
    dest = airport.get("destination") or {}
    time_block = flight.get("time") or {}
    sched = time_block.get("scheduled") or {}
    real = time_block.get("real") or {}

    origin_iata = (((origin.get("code") or {}).get("iata")) or "").upper() or None
    dest_iata = (((dest.get("code") or {}).get("iata")) or "").upper() or None

    # Anchor-side inference cuando FR24 omite el lado implícito
    anchor_iata = (anchor_airport or "").upper().lstrip("K") or None
    if anchor_iata:
        if is_arrival_side is True and not dest_iata:
            dest_iata = anchor_iata
        elif is_arrival_side is False and not origin_iata:
            origin_iata = anchor_iata

    sched_out_epoch = sched.get("departure")
    sched_in_epoch = sched.get("arrival")

    origin_tz_offset = ((origin.get("timezone") or {}).get("offset"))
    fl_date, crs_dep_min = _local_date_and_min(sched_out_epoch, origin_tz_offset)

    scheduled_out_utc = _epoch_to_iso(sched_out_epoch)
    scheduled_in_utc = _epoch_to_iso(sched_in_epoch)

    crs_elapsed_min: float | None = None
    if sched_out_epoch and sched_in_epoch:
        try:
            crs_elapsed_min = (int(sched_in_epoch) - int(sched_out_epoch)) / 60.0
        except (ValueError, TypeError):
            crs_elapsed_min = None

    ident_iata = (ident.get("number") or {}).get("default")
    op_carrier = ((airline.get("code") or {}).get("iata"))
    cancelled, diverted = _status_flags(flight)

    return {
        "fa_flight_id": fa_flight_id,
        "stable_id": fa_flight_id,
        "ident_iata": ident_iata,
        "op_carrier": op_carrier,
        "flight_number": _parse_flight_number(ident_iata),
        "tail_num": (aircraft.get("registration") or None),
        "origin": origin_iata,
        "dest": dest_iata,
        "inbound_fa_flight_id": None,  # se hidrata en Capa 2 (chain walk)
        "fl_date": fl_date,
        "crs_dep_min": crs_dep_min,
        "scheduled_out_utc": scheduled_out_utc,
        "scheduled_off_utc": scheduled_out_utc,  # FR24 no diferencia gate/wheels-up programado
        "scheduled_on_utc": scheduled_in_utc,
        "scheduled_in_utc": scheduled_in_utc,
        "crs_elapsed_min": crs_elapsed_min,
        "distance": None,  # FR24 no lo expone consistentemente
        "aircraft_type": ((aircraft.get("model") or {}).get("code")),
        "cancelled": cancelled,
        "diverted": diverted,
    }


def normalize_actual(flight: dict[str, Any]) -> dict[str, Any] | None:
    """Extrae actuals (real.{departure,arrival}) en formato actuals table."""
    ident = flight.get("identification") or {}
    fa_flight_id = ident.get("id")
    if not fa_flight_id:
        return None

    time_block = flight.get("time") or {}
    real = time_block.get("real") or {}
    sched = time_block.get("scheduled") or {}

    actual_off_epoch = real.get("departure")
    actual_in_epoch = real.get("arrival")
    if not actual_off_epoch and not actual_in_epoch:
        return None

    arr_delay_min: float | None = None
    if actual_in_epoch and sched.get("arrival"):
        try:
            arr_delay_min = (int(actual_in_epoch) - int(sched["arrival"])) / 60.0
        except (ValueError, TypeError):
            arr_delay_min = None

    dep_delay_min: float | None = None
    if actual_off_epoch and sched.get("departure"):
        try:
            dep_delay_min = (int(actual_off_epoch) - int(sched["departure"])) / 60.0
        except (ValueError, TypeError):
            dep_delay_min = None

    cancelled, diverted = _status_flags(flight)

    return {
        "fa_flight_id": fa_flight_id,
        "stable_id": fa_flight_id,
        "actual_out_utc": None,  # FR24 no expone gate-out, solo wheels-up
        "actual_off_utc": _epoch_to_iso(actual_off_epoch),
        "actual_on_utc": _epoch_to_iso(actual_in_epoch),  # aprox: touchdown == gate-in en FR24
        "actual_in_utc": _epoch_to_iso(actual_in_epoch),
        "arr_delay_min": arr_delay_min,
        "departure_delay_min": dep_delay_min,
        "cancelled": cancelled,
        "diverted": diverted,
    }
