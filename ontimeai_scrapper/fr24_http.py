"""Capa 2A — Chain walk via HTTP directo a flight/list.json.

Aprovecha el APIClient interno de la librería FR24 (curl_cffi + chrome136)
para pasar Cloudflare desde IP residencial Y desde Cloud Run.

Endpoint:
    https://api.flightradar24.com/common/v1/flight/list.json
    ?query={REG}&fetchBy=reg&limit={N}&page=1

Devuelve el itinerario completo del tail (futuro + pasado en orden descendente
por scheduled_departure). Skipeamos vuelos futuros con id==null porque Capa 1
los capturará cuando se acerquen al anchor.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from FlightRadarAPI.core import Core

from . import config
from .fr24_client import (
    SYNTHETIC_ID_PREFIX,
    FR24Client,
    FR24EmptyResponseError,
    FR24Error,
    normalize_actual,
    normalize_flight,
)

log = logging.getLogger(__name__)

FR24_HISTORY_URL = f"{Core.api_flightradar_base_url}/flight/list.json"


def _is_future_atl_leg(f: dict[str, Any] | None, *, horizon_hours: int, now: datetime) -> bool:
    """True if a normalized leg is an ATL-relevant flight still ahead of `now`.

    Keeps only the useful future legs from a tail's itinerary: anchored at ATL (either
    direction), not cancelled, scheduled departure in (now, now+horizon].
    """
    if not f or f.get("cancelled"):
        return False
    if f.get("origin") != "ATL" and f.get("dest") != "ATL":
        return False
    sout = f.get("scheduled_out_utc")
    if not sout:
        return False
    try:
        dep = datetime.fromisoformat(sout)
    except ValueError:
        return False
    if dep.tzinfo is None:
        dep = dep.replace(tzinfo=timezone.utc)
    return now < dep <= now + timedelta(hours=horizon_hours)


def fetch_aircraft_history(
    fr24_client: FR24Client,
    registration: str,
    *,
    limit: int = config.FR24_HISTORY_DEFAULT_LIMIT,
    page: int = 1,
    capture_future_legs: bool = False,
    future_horizon_hours: int = config.FUTURE_LEG_HORIZON_HOURS,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Trae el historial reciente de un tail y normaliza.

    Args:
        capture_future_legs: si True, además del historial con id real, retiene los
            vuelos FUTUROS con id==null que son ATL-relevantes (origin o dest = ATL) y
            caen dentro de `future_horizon_hours`, asignándoles un id sintético
            determinístico. Esto adelanta el descubrimiento de salidas de ATL de ~1h a
            varias horas vía rotación del avión. Default False.

    Returns:
        (raw_items, flights_rows, actuals_rows) — raw para chain walk en orden,
        flights/actuals listas para UPSERT. Con capture_future_legs, flights_rows
        incluye placeholders SYN-… para los vuelos futuros sin id.

    Raises:
        FR24Error / FR24RateLimitedError si Cloudflare/HTTP/parse fallan.
    """
    api_client = fr24_client.api_client
    if api_client is None:
        raise FR24Error("APIClient interno no disponible (¿lib FR24 instalada?)")

    params = {
        "query": registration.upper(),
        "fetchBy": "reg",
        "limit": int(limit),
        "page": int(page),
    }

    def _do_request():
        resp = api_client.request(
            FR24_HISTORY_URL,
            params=params,
            headers=Core.json_headers,
            timeout=int(config.FR24_TIMEOUT_SECONDS),
        )
        status = resp.get_status_code()
        if status >= 400:
            raise FR24Error(f"flight/list.json HTTP {status} for {registration}")
        return resp.get_json_content()

    payload = fr24_client.throttled_call(
        f"flight_list({registration})", _do_request
    )

    try:
        items = (payload.get("result") or {}).get("response", {}).get("data") or []
    except AttributeError as exc:
        raise FR24Error(f"shape inesperado en flight/list.json: {exc}") from exc

    if not items:
        raise FR24EmptyResponseError(f"flight/list.json sin items para {registration}")

    flights_rows: list[dict[str, Any]] = []
    actuals_rows: list[dict[str, Any]] = []
    raw_items: list[dict[str, Any]] = []
    n_future = 0
    now = datetime.now(timezone.utc)

    for item in items:
        if not isinstance(item, dict):
            continue
        raw_items.append(item)
        # `flight/list.json` devuelve cada item con las mismas keys que
        # `flight.{...}` del response de airport.json — reusamos los normalizers.
        f = normalize_flight(item, anchor_airport=None, is_arrival_side=None)
        if f and f.get("fa_flight_id"):
            flights_rows.append(f)
        elif capture_future_legs:
            # id==null → vuelo futuro aún no activado. Retenerlo como placeholder
            # sintético sólo si es una pierna ATL futura dentro del horizonte.
            fs = normalize_flight(
                item, anchor_airport=None, is_arrival_side=None, allow_synthetic_id=True
            )
            if (
                fs
                and str(fs.get("fa_flight_id", "")).startswith(SYNTHETIC_ID_PREFIX)
                and _is_future_atl_leg(fs, horizon_hours=future_horizon_hours, now=now)
            ):
                flights_rows.append(fs)
                n_future += 1
        a = normalize_actual(item)
        if a and a.get("fa_flight_id"):
            actuals_rows.append(a)

    log.debug(
        "fetch_aircraft_history(%s): raw=%d kept_flights=%d kept_actuals=%d future_legs=%d",
        registration, len(raw_items), len(flights_rows), len(actuals_rows), n_future,
    )

    return raw_items, flights_rows, actuals_rows
