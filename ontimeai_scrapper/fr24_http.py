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
from typing import Any

from FlightRadarAPI.core import Core

from . import config
from .fr24_client import (
    FR24Client,
    FR24EmptyResponseError,
    FR24Error,
    normalize_actual,
    normalize_flight,
)

log = logging.getLogger(__name__)

FR24_HISTORY_URL = f"{Core.api_flightradar_base_url}/flight/list.json"


def fetch_aircraft_history(
    fr24_client: FR24Client,
    registration: str,
    *,
    limit: int = config.FR24_HISTORY_DEFAULT_LIMIT,
    page: int = 1,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Trae el historial reciente de un tail y normaliza.

    Returns:
        (raw_items, flights_rows, actuals_rows) — raw para chain walk en orden,
        flights/actuals listas para UPSERT (skipea vuelos con id==null).

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

    for item in items:
        if not isinstance(item, dict):
            continue
        raw_items.append(item)
        # `flight/list.json` devuelve cada item con las mismas keys que
        # `flight.{...}` del response de airport.json — reusamos los normalizers.
        f = normalize_flight(item, anchor_airport=None, is_arrival_side=None)
        if f and f.get("fa_flight_id"):
            flights_rows.append(f)
        a = normalize_actual(item)
        if a and a.get("fa_flight_id"):
            actuals_rows.append(a)

    log.debug(
        "fetch_aircraft_history(%s): raw=%d kept_flights=%d kept_actuals=%d",
        registration, len(raw_items), len(flights_rows), len(actuals_rows),
    )

    return raw_items, flights_rows, actuals_rows
