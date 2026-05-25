"""airplanes.live — community ADS-B feed, gratis sin auth (Mejora #1, primary).

Mejor que OpenSky para nuestro caso porque:
  - Devuelve `r` (registration N-number) → match directo con flights.tail_num
  - Devuelve `t` (aircraft typecode ICAO) → enriches AIRCRAFT_FAMILY feature
  - Designed for community/research use, friendly a data center IPs (Cloud Run)
  - Sin rate limit declarado (usamos 1 call/tick)

Endpoint:
  https://api.airplanes.live/v2/point/{lat}/{lon}/{radius_nm}

ATL: lat=33.6367, lon=-84.4281, radius=250nm covers most pre-arrival traffic.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

ATL_LAT = 33.6367
ATL_LON = -84.4281
ATL_RADIUS_NM = 250

AIRPLANES_LIVE_URL = "https://api.airplanes.live/v2/point/{lat}/{lon}/{radius}"
_REQUEST_TIMEOUT = 15.0
_MIN_INTERVAL_SECONDS = 5.0  # be a good citizen


class AirplanesLiveClient:
    """Public read-only client for airplanes.live aggregated ADS-B."""

    def __init__(self):
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "OnTimeAI-Academic/1.0 (research; github.com/santiago6124)",
            "Accept": "application/json",
        })
        self._last_call_t: float = 0.0

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call_t
        if elapsed < _MIN_INTERVAL_SECONDS:
            time.sleep(_MIN_INTERVAL_SECONDS - elapsed)
        self._last_call_t = time.monotonic()

    def get_aircraft_point(
        self,
        lat: float = ATL_LAT,
        lon: float = ATL_LON,
        radius_nm: int = ATL_RADIUS_NM,
    ) -> list[dict]:
        """Fetch aircraft within `radius_nm` of (lat,lon). Normalized to our
        aircraft_position schema. Empty list on any error (graceful degrade).
        """
        self._throttle()
        url = AIRPLANES_LIVE_URL.format(lat=lat, lon=lon, radius=radius_nm)
        try:
            r = self._session.get(url, timeout=_REQUEST_TIMEOUT)
            if r.status_code >= 400:
                log.warning("airplanes.live: HTTP %d — %s", r.status_code, r.text[:200])
                return []
            payload = r.json()
        except requests.RequestException as exc:
            log.warning("airplanes.live: request failed — %s", exc)
            return []
        except ValueError as exc:
            log.warning("airplanes.live: JSON parse failed — %s", exc)
            return []

        aircraft = payload.get("ac") or []
        capture_iso = datetime.now(timezone.utc).isoformat()
        normalized: list[dict] = []
        for ac in aircraft:
            try:
                row = _normalize(ac, capture_iso)
            except Exception as exc:  # noqa: BLE001
                log.debug("airplanes.live: skip aircraft due to %s", exc)
                continue
            if row:
                normalized.append(row)
        return normalized


def _normalize(ac: dict, captured_at_utc: str) -> dict | None:
    """Map an airplanes.live aircraft dict to our aircraft_position schema."""
    hex_id = (ac.get("hex") or "").strip().lower()
    if not hex_id:
        return None

    # Altitude — alt_baro in feet (string "ground" or number); alt_geom in feet.
    def _ft_to_m(v):
        if v is None or v == "ground":
            return None
        try:
            return float(v) * 0.3048
        except (TypeError, ValueError):
            return None

    # Groundspeed — knots → m/s
    gs_kt = ac.get("gs")
    velocity_mps = float(gs_kt) * 0.5144 if gs_kt is not None else None

    # Baro rate (ft/min) → m/s
    baro_rate = ac.get("baro_rate")
    vertical_rate_mps = float(baro_rate) * 0.00508 if baro_rate is not None else None

    on_ground = 1 if ac.get("alt_baro") == "ground" else 0

    return {
        "icao24": hex_id,
        "captured_at_utc": captured_at_utc,
        "callsign": (ac.get("flight") or "").strip() or None,
        "registration": (ac.get("r") or "").strip().upper() or None,
        "aircraft_type": (ac.get("t") or "").strip().upper() or None,
        "lat": ac.get("lat"),
        "lon": ac.get("lon"),
        "baro_altitude_m": _ft_to_m(ac.get("alt_baro")),
        "geo_altitude_m": _ft_to_m(ac.get("alt_geom")),
        "velocity_mps": velocity_mps,
        "true_track_deg": ac.get("track"),
        "vertical_rate_mps": vertical_rate_mps,
        "on_ground": on_ground,
        "origin_country": None,
        "source": "airplanes.live",
    }
