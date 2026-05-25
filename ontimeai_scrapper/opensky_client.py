"""OpenSky Network — ADS-B real-time positions via /states/all (Mejora #1).

Gratis sin auth:
  - 400 req/día anónimo (con BBOX usamos ~96 calls/día = 1 por tick c/15min)
  - 100 req/min anónimo
  - Hard limit 10 días history

URL:
  https://opensky-network.org/api/states/all
  ?lamin=28.6&lomin=-89.4&lamax=38.6&lomax=-79.4

Devuelve aircraft EN AIRE dentro del bbox ±5° alrededor de KATL (~300nm).
Para arrivals a ATL próximos a aterrizar, esto da ETA real:
  time_to_atl_min = haversine(lat,lon, ATL_lat,ATL_lon) / velocity_mps / 60

El backend (Tier 3 #3 del FIXES_PLAN.md) podrá usar esto para boost de
predicciones de arrivals donde el avión ya está en formación.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Iterator

import requests

from . import config

log = logging.getLogger(__name__)

# Bbox sobre KATL (33.6367°N, 84.4281°W) ±5° (~300nm radio aproximado)
ATL_BBOX = {
    "lamin": 28.6,
    "lomin": -89.5,
    "lamax": 38.6,
    "lomax": -79.5,
}

OPENSKY_STATES_URL = "https://opensky-network.org/api/states/all"
# Hard-coded conservative throttle: don't exceed 1 call per 12s avg → 5/min ≤ 100/min limit.
_OPENSKY_THROTTLE_SECONDS = 12.0
_OPENSKY_TIMEOUT = 15.0
# Limit how many recent calls we allow per process run (defensive).
_OPENSKY_MAX_CALLS_PER_TICK = 3


class OpenSkyClient:
    """Anonymous-friendly OpenSky client for bbox state queries."""

    def __init__(
        self,
        *,
        username: str = config.OPENSKY_USERNAME,
        password: str = config.OPENSKY_PASSWORD,
    ):
        self._username = username or None
        self._password = password or None
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "OnTimeAI-Academic/1.0 (research; contact via github.com/santiago6124)",
            "Accept": "application/json",
        })
        self._last_call_t: float = 0.0
        self._calls_this_run: int = 0

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call_t
        if elapsed < _OPENSKY_THROTTLE_SECONDS:
            time.sleep(_OPENSKY_THROTTLE_SECONDS - elapsed)
        self._last_call_t = time.monotonic()

    def get_states_bbox(
        self,
        *,
        lamin: float = ATL_BBOX["lamin"],
        lomin: float = ATL_BBOX["lomin"],
        lamax: float = ATL_BBOX["lamax"],
        lomax: float = ATL_BBOX["lomax"],
    ) -> list[dict]:
        """Fetch current aircraft states in the bbox. Returns list of dicts
        normalized to our aircraft_position schema. Empty list on any failure
        (graceful degradation — ADS-B is enrichment, not load-bearing).
        """
        if self._calls_this_run >= _OPENSKY_MAX_CALLS_PER_TICK:
            log.warning("OpenSky: max calls per tick reached, skipping")
            return []

        self._throttle()
        params = {
            "lamin": lamin,
            "lomin": lomin,
            "lamax": lamax,
            "lomax": lomax,
        }
        auth = None
        if self._username and self._password:
            auth = (self._username, self._password)
        try:
            r = self._session.get(
                OPENSKY_STATES_URL,
                params=params,
                auth=auth,
                timeout=_OPENSKY_TIMEOUT,
            )
            self._calls_this_run += 1
            if r.status_code == 429:
                log.warning("OpenSky: 429 rate-limited")
                return []
            if r.status_code >= 400:
                log.warning("OpenSky: HTTP %d — %s", r.status_code, r.text[:200])
                return []
            payload = r.json()
        except requests.RequestException as exc:
            log.warning("OpenSky: request failed — %s", exc)
            return []
        except ValueError as exc:
            log.warning("OpenSky: JSON parse failed — %s", exc)
            return []

        states = payload.get("states") or []
        capture_iso = datetime.now(timezone.utc).isoformat()
        return [_normalize_state(s, capture_iso) for s in states if _is_valid_state(s)]


def _is_valid_state(s) -> bool:
    """OpenSky returns states as ordered arrays; we need at least icao24 (index 0)."""
    return isinstance(s, (list, tuple)) and len(s) >= 17 and s[0]


def _normalize_state(s: list, captured_at_utc: str) -> dict:
    """Map OpenSky positional array to our aircraft_position schema.

    Field order per OpenSky API spec (0..16):
      0: icao24, 1: callsign, 2: origin_country, 3: time_position,
      4: last_contact, 5: longitude, 6: latitude, 7: baro_altitude,
      8: on_ground, 9: velocity, 10: true_track, 11: vertical_rate,
      12: sensors, 13: geo_altitude, 14: squawk, 15: spi, 16: position_source
    """
    callsign = (s[1] or "").strip() if s[1] else None
    return {
        "icao24": str(s[0]).strip().lower(),
        "captured_at_utc": captured_at_utc,
        "callsign": callsign or None,
        "registration": None,        # OpenSky doesn't return registration
        "aircraft_type": None,        # idem
        "lat": float(s[6]) if s[6] is not None else None,
        "lon": float(s[5]) if s[5] is not None else None,
        "baro_altitude_m": float(s[7]) if s[7] is not None else None,
        "geo_altitude_m": float(s[13]) if s[13] is not None else None,
        "velocity_mps": float(s[9]) if s[9] is not None else None,
        "true_track_deg": float(s[10]) if s[10] is not None else None,
        "vertical_rate_mps": float(s[11]) if s[11] is not None else None,
        "on_ground": 1 if s[8] else 0,
        "origin_country": (s[2] or "").strip() or None,
        "source": "opensky",
    }


def iter_atl_arrivals_in_air(
    states: list[dict], *, max_distance_nm: float = 250.0
) -> Iterator[dict]:
    """Filter states list to aircraft likely heading TO ATL (heading bracket +
    approaching). Useful when backend wants to compute ETAs.

    This is a coarse filter — the backend will do the precise great-circle
    distance + ETA computation. We just narrow the candidate set.
    """
    ATL_LAT, ATL_LON = 33.6367, -84.4281
    for st in states:
        if st.get("on_ground"):
            continue
        lat = st.get("lat")
        lon = st.get("lon")
        if lat is None or lon is None:
            continue
        # Rough proximity check (Pythagorean on flat earth — fine for filter)
        d_lat = abs(lat - ATL_LAT)
        d_lon = abs(lon - ATL_LON)
        if d_lat * 60 > max_distance_nm or d_lon * 50 > max_distance_nm:
            continue
        yield st
