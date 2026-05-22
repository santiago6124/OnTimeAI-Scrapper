"""Fase 0 — Validar empíricamente que las fuentes del harvester devuelven los fields esperados.

Corre 5 checks contra fuentes reales (no mocks) y reporta:
  1. FR24 lib            -> get_airport_details("KATL")
  2. FR24 HTTP directo   -> flight/list.json?query={REG}&fetchBy=reg
  3. OpenSky             -> /flights/aircraft?icao24=...
  4. FAA MASTER          -> ReleasableAircraft.zip download + parse
  5. Mapeo de fields     -> dump del mapping vs live.py:_normalize_flight_row()

Uso:
    python scripts/validate_sources.py --all
    python scripts/validate_sources.py --check fr24-airport
    python scripts/validate_sources.py --check fr24-history --tails N301DQ,N821DN
    python scripts/validate_sources.py --check opensky --icao24 a06b4f
    python scripts/validate_sources.py --check faa-master --download-dir ./tmp

Salida: reporte JSON en stdout. Si --output PATH, guarda copia.
Criterio de exito: todos los checks devuelven success=True con >=80% de fields esperados.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import time
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

try:  # graceful: el check FAA y los otros pueden correr sin la lib si no instalada
    from FlightRadarAPI import FlightRadar24API
except ImportError:  # pragma: no cover
    try:
        from FlightRadar24 import FlightRadar24API  # type: ignore[no-redef]
    except ImportError:
        FlightRadar24API = None  # type: ignore[assignment]

import requests

# Permitir correr el script sin instalar el paquete (PYTHONPATH workaround)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ontimeai_scrapper import config  # noqa: E402

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("validate_sources")


# ---------- Tipos ----------


@dataclass
class CheckReport:
    name: str
    success: bool
    latency_s: float
    status_code: int | None = None
    fields_expected: list[str] = field(default_factory=list)
    fields_found: list[str] = field(default_factory=list)
    fields_missing: list[str] = field(default_factory=list)
    sample_record: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def coverage(self) -> float:
        if not self.fields_expected:
            return 1.0
        return len(self.fields_found) / len(self.fields_expected)


# ---------- Helpers ----------


def _pick_user_agent() -> str:
    return random.choice(config.USER_AGENTS)


def _nested_get(obj: Any, dotted_path: str) -> Any:
    """Lookup recursivo: 'flight.aircraft.registration' -> obj['flight']['aircraft']['registration']."""
    cursor = obj
    for key in dotted_path.split("."):
        if cursor is None:
            return None
        if isinstance(cursor, dict):
            cursor = cursor.get(key)
        else:
            try:
                cursor = getattr(cursor, key)
            except AttributeError:
                return None
    return cursor


def _flatten_keys(obj: Any, prefix: str = "", max_depth: int = 4, depth: int = 0) -> list[str]:
    """Enumera keys con dot-notation hasta max_depth."""
    keys: list[str] = []
    if depth >= max_depth or obj is None:
        return keys
    if isinstance(obj, dict):
        for k, v in obj.items():
            full = f"{prefix}.{k}" if prefix else str(k)
            keys.append(full)
            keys.extend(_flatten_keys(v, full, max_depth, depth + 1))
    elif isinstance(obj, list) and obj:
        keys.extend(_flatten_keys(obj[0], f"{prefix}[0]", max_depth, depth + 1))
    return keys


# ---------- Check 1: FR24 lib get_airport_details ----------


FR24_AIRPORT_EXPECTED_FIELDS = [
    "identification.id",
    "aircraft.registration",
    "airline.code.iata",
    "airport.origin.code.iata",
    "airport.destination.code.iata",
    "time.scheduled.departure",
    "time.scheduled.arrival",
    "time.real.departure",
    "time.real.arrival",
]


def check_fr24_airport(airport: str = "KATL", page: int = 1, flight_limit: int = 100) -> CheckReport:
    name = "fr24_airport_details"
    report = CheckReport(
        name=name,
        success=False,
        latency_s=0.0,
        fields_expected=FR24_AIRPORT_EXPECTED_FIELDS,
    )
    if FlightRadar24API is None:
        report.error = "FlightRadarAPI no instalada (pip install FlightRadarAPI==1.5.1)"
        return report

    api = FlightRadar24API()
    start = time.monotonic()
    try:
        details = api.get_airport_details(code=airport, flight_limit=flight_limit, page=page)
    except Exception as exc:  # pragma: no cover - depende de red/Cloudflare
        report.error = f"{type(exc).__name__}: {exc}"
        report.latency_s = time.monotonic() - start
        return report
    report.latency_s = time.monotonic() - start

    # FR24 lib devuelve dict con 'airport' -> 'pluginData' -> 'schedule' -> {arrivals, departures}
    try:
        sched = details["airport"]["pluginData"]["schedule"]
        arrivals = sched.get("arrivals", {}).get("data", []) or []
        departures = sched.get("departures", {}).get("data", []) or []
    except (KeyError, TypeError) as exc:
        report.error = f"shape inesperado: {exc}"
        report.sample_record = {"top_keys": list(details.keys()) if isinstance(details, dict) else None}
        return report

    flights = arrivals + departures
    report.notes.append(f"arrivals={len(arrivals)} departures={len(departures)}")
    if not flights:
        report.error = "respuesta sin vuelos (¿paginación agotada?)"
        return report

    sample = flights[0].get("flight") if isinstance(flights[0], dict) else None
    if sample is None:
        report.error = "primer elemento sin key 'flight'"
        report.sample_record = flights[0]
        return report

    report.sample_record = sample
    for expected in FR24_AIRPORT_EXPECTED_FIELDS:
        value = _nested_get(sample, expected)
        if value not in (None, "", {}):
            report.fields_found.append(expected)
        else:
            report.fields_missing.append(expected)

    report.success = report.coverage >= 0.8
    return report


# ---------- Check 2: FR24 HTTP directo flight/list.json ----------


FR24_HISTORY_EXPECTED_FIELDS = [
    "identification.id",
    "aircraft.registration",
    "airline.code.iata",
    "airport.origin.code.iata",
    "airport.destination.code.iata",
    "time.scheduled.departure",
    "time.scheduled.arrival",
    "time.real.departure",
    "time.real.arrival",
]


def check_fr24_aircraft_history(
    registration: str,
    limit: int = 25,
    page: int = 1,
) -> CheckReport:
    name = f"fr24_aircraft_history[{registration}]"
    report = CheckReport(
        name=name,
        success=False,
        latency_s=0.0,
        fields_expected=FR24_HISTORY_EXPECTED_FIELDS,
    )

    headers = {
        "User-Agent": _pick_user_agent(),
        "Origin": "https://www.flightradar24.com",
        "Referer": f"https://www.flightradar24.com/data/aircraft/{registration.lower()}",
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
    }
    params = {"query": registration, "fetchBy": "reg", "limit": limit, "page": page}
    url = config.FR24_HISTORY_BASE_URL

    log.info("GET %s?%s", url, urlencode(params))
    start = time.monotonic()
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=config.FR24_TIMEOUT_SECONDS)
        report.status_code = resp.status_code
        if resp.status_code != 200:
            report.error = f"HTTP {resp.status_code}: {resp.text[:300]}"
            report.latency_s = time.monotonic() - start
            return report
        payload = resp.json()
    except Exception as exc:
        report.error = f"{type(exc).__name__}: {exc}"
        report.latency_s = time.monotonic() - start
        return report
    report.latency_s = time.monotonic() - start

    try:
        items = payload["result"]["response"]["data"] or []
    except (KeyError, TypeError) as exc:
        report.error = f"shape inesperado: {exc}"
        report.sample_record = {"top_keys": list(payload.keys()) if isinstance(payload, dict) else None}
        return report

    report.notes.append(f"n_items={len(items)}")
    if not items:
        report.error = "respuesta con 0 vuelos para esa registration"
        return report

    sample = items[0].get("flight") if isinstance(items[0], dict) and "flight" in items[0] else items[0]
    report.sample_record = sample
    for expected in FR24_HISTORY_EXPECTED_FIELDS:
        value = _nested_get(sample, expected)
        if value not in (None, "", {}):
            report.fields_found.append(expected)
        else:
            report.fields_missing.append(expected)

    report.success = report.coverage >= 0.8
    return report


# ---------- Check 3: OpenSky /flights/aircraft ----------


OPENSKY_EXPECTED_FIELDS = [
    "icao24",
    "firstSeen",
    "estDepartureAirport",
    "lastSeen",
    "estArrivalAirport",
    "callsign",
]


def check_opensky_aircraft_history(icao24: str, days_back: int = 2) -> CheckReport:
    name = f"opensky_aircraft_history[{icao24}]"
    report = CheckReport(
        name=name,
        success=False,
        latency_s=0.0,
        fields_expected=OPENSKY_EXPECTED_FIELDS,
    )

    icao24 = icao24.lower().strip()
    end = int(datetime.now(timezone.utc).timestamp())
    begin = end - min(days_back, 2) * 24 * 3600  # max 2 días por call

    url = f"{config.OPENSKY_BASE_URL}/flights/aircraft"
    params = {"icao24": icao24, "begin": begin, "end": end}
    auth = None
    if config.OPENSKY_USERNAME and config.OPENSKY_PASSWORD:
        auth = (config.OPENSKY_USERNAME, config.OPENSKY_PASSWORD)
        report.notes.append("auth=user")
    else:
        report.notes.append("auth=anonymous")

    log.info("GET %s?%s", url, urlencode(params))
    start = time.monotonic()
    try:
        resp = requests.get(url, params=params, auth=auth, timeout=20.0)
        report.status_code = resp.status_code
        if resp.status_code == 404:
            # OpenSky returns 404 cuando no hay vuelos en la ventana — no es bug
            report.error = "404 (no flights in window — endpoint reachable but icao24 sin actividad)"
            report.latency_s = time.monotonic() - start
            report.notes.append("endpoint OK pero icao24 sin tráfico; probar con otro")
            return report
        if resp.status_code != 200:
            report.error = f"HTTP {resp.status_code}: {resp.text[:200]}"
            report.latency_s = time.monotonic() - start
            return report
        items = resp.json()
    except Exception as exc:
        report.error = f"{type(exc).__name__}: {exc}"
        report.latency_s = time.monotonic() - start
        return report
    report.latency_s = time.monotonic() - start

    report.notes.append(f"n_items={len(items)}")
    if not items:
        report.error = "respuesta vacía"
        return report

    sample = items[0]
    report.sample_record = sample
    for expected in OPENSKY_EXPECTED_FIELDS:
        if sample.get(expected) not in (None, ""):
            report.fields_found.append(expected)
        else:
            report.fields_missing.append(expected)

    report.success = report.coverage >= 0.8
    return report


# ---------- Check 4: FAA MASTER.txt ----------


FAA_EXPECTED_COLUMNS = [
    "N-NUMBER",
    "MFR MDL CODE",
    "MODE S CODE HEX",
    "MFR",
]


def check_faa_master(download_dir: Path) -> CheckReport:
    name = "faa_master"
    report = CheckReport(
        name=name,
        success=False,
        latency_s=0.0,
        fields_expected=FAA_EXPECTED_COLUMNS,
    )

    download_dir.mkdir(parents=True, exist_ok=True)
    zip_path = download_dir / "ReleasableAircraft.zip"
    master_path = download_dir / "MASTER.txt"

    start = time.monotonic()
    try:
        if not zip_path.exists():
            log.info("downloading %s -> %s", config.FAA_RELEASABLE_ZIP_URL, zip_path)
            resp = requests.get(
                config.FAA_RELEASABLE_ZIP_URL,
                stream=True,
                timeout=120.0,
                headers={"User-Agent": _pick_user_agent()},
            )
            report.status_code = resp.status_code
            if resp.status_code != 200:
                report.error = f"HTTP {resp.status_code}"
                report.latency_s = time.monotonic() - start
                return report
            with zip_path.open("wb") as fh:
                for chunk in resp.iter_content(chunk_size=1024 * 256):
                    fh.write(chunk)
            report.notes.append(f"downloaded={zip_path.stat().st_size / 1024 / 1024:.1f} MB")
        else:
            report.notes.append(f"reused existing zip={zip_path}")

        if not master_path.exists():
            with zipfile.ZipFile(zip_path) as zf:
                names = zf.namelist()
                master_name = next((n for n in names if n.upper().endswith("MASTER.TXT")), None)
                if master_name is None:
                    report.error = f"MASTER.txt no encontrado en zip. Archivos: {names[:10]}"
                    report.latency_s = time.monotonic() - start
                    return report
                zf.extract(master_name, download_dir)
                if master_name != "MASTER.txt":
                    Path(download_dir / master_name).rename(master_path)
            report.notes.append(f"extracted={master_path}")

        with master_path.open("r", encoding="latin-1", errors="replace") as fh:
            header = fh.readline().strip()
            sample_line = fh.readline().strip()

        cols = [c.strip() for c in header.split(",")]
        report.notes.append(f"n_cols={len(cols)}")
        for expected in FAA_EXPECTED_COLUMNS:
            if any(c.upper() == expected.upper() for c in cols):
                report.fields_found.append(expected)
            else:
                report.fields_missing.append(expected)

        report.sample_record = {
            "header_cols": cols[:15],
            "sample_row_first_5_fields": sample_line.split(",")[:5] if sample_line else None,
        }
        report.success = report.coverage >= 0.75
    except Exception as exc:
        report.error = f"{type(exc).__name__}: {exc}"
    finally:
        report.latency_s = time.monotonic() - start

    return report


# ---------- Check 5: mapping ----------


def emit_field_mapping() -> dict[str, dict[str, str | None]]:
    """Dump del mapping entre fuentes y schema de OnTimeAI-Backend/ontimeai/live.py."""
    return {
        "fa_flight_id": {
            "fr24_airport": "flight.identification.id",
            "fr24_history": "identification.id",
            "opensky": "synthetic: f'{icao24}_{firstSeen}'",
        },
        "tail_num": {
            "fr24_airport": "flight.aircraft.registration",
            "fr24_history": "aircraft.registration",
            "opensky": "requires FAA MASTER lookup (icao24 -> N-NUMBER)",
        },
        "op_carrier": {
            "fr24_airport": "flight.airline.code.iata",
            "fr24_history": "airline.code.iata",
            "opensky": "derive from callsign prefix (DAL -> DL, AAL -> AA, ...)",
        },
        "origin_iata": {
            "fr24_airport": "flight.airport.origin.code.iata",
            "fr24_history": "airport.origin.code.iata",
            "opensky": "estDepartureAirport (ICAO -> need IATA lookup)",
        },
        "dest_iata": {
            "fr24_airport": "flight.airport.destination.code.iata",
            "fr24_history": "airport.destination.code.iata",
            "opensky": "estArrivalAirport (ICAO -> need IATA lookup)",
        },
        "crs_dep_utc": {
            "fr24_airport": "flight.time.scheduled.departure (epoch)",
            "fr24_history": "time.scheduled.departure (epoch)",
            "opensky": None,
        },
        "scheduled_in_utc": {
            "fr24_airport": "flight.time.scheduled.arrival",
            "fr24_history": "time.scheduled.arrival",
            "opensky": None,
        },
        "actual_off_utc": {
            "fr24_airport": "flight.time.real.departure",
            "fr24_history": "time.real.departure",
            "opensky": "firstSeen (epoch)",
        },
        "actual_in_utc": {
            "fr24_airport": "flight.time.real.arrival",
            "fr24_history": "time.real.arrival",
            "opensky": "lastSeen (epoch)",
        },
        "arr_delay_min": {
            "fr24_airport": "compute: actual_in - scheduled_in",
            "fr24_history": "compute: actual_in - scheduled_in",
            "opensky": "compute (scheduled NaN si solo OpenSky)",
        },
    }


# ---------- CLI ----------


CHECKS = {
    "fr24-airport": "check_fr24_airport",
    "fr24-history": "check_fr24_aircraft_history",
    "opensky": "check_opensky_aircraft_history",
    "faa-master": "check_faa_master",
}

DEFAULT_TAILS = ["N301DQ", "N821DN", "N371DA"]  # Delta hub tails comunes en ATL
DEFAULT_ICAO24 = "a06b4f"  # Tail Delta conocido — ajustar tras lookup FAA real


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--check", choices=list(CHECKS.keys()), help="Correr solo este check")
    p.add_argument("--all", action="store_true", help="Correr los 4 checks + dump del mapping")
    p.add_argument("--airport", default=config.AIRPORT_CODE, help="Código ICAO para Capa 1")
    p.add_argument("--tails", default=",".join(DEFAULT_TAILS), help="Registrations CSV para fr24-history")
    p.add_argument("--icao24", default=DEFAULT_ICAO24, help="Hex code para opensky")
    p.add_argument("--download-dir", default="./tmp/faa", help="Carpeta para FAA MASTER")
    p.add_argument("--output", help="Ruta para guardar reporte JSON (opcional)")
    p.add_argument("--throttle", type=float, default=config.FR24_THROTTLE_SECONDS, help="Delay entre calls FR24")
    return p.parse_args()


def _throttle(seconds: float) -> None:
    jitter = random.uniform(0, config.FR24_THROTTLE_JITTER_SECONDS)
    time.sleep(seconds + jitter)


def main() -> int:
    args = parse_args()
    reports: list[CheckReport] = []

    run_check = args.check
    run_all = args.all or run_check is None

    if run_all or run_check == "fr24-airport":
        log.info("=== Check 1: FR24 get_airport_details(%s) ===", args.airport)
        reports.append(check_fr24_airport(airport=args.airport))
        _throttle(args.throttle)

    if run_all or run_check == "fr24-history":
        for tail in [t.strip() for t in args.tails.split(",") if t.strip()]:
            log.info("=== Check 2: FR24 history(%s) ===", tail)
            reports.append(check_fr24_aircraft_history(tail))
            _throttle(args.throttle)

    if run_all or run_check == "opensky":
        log.info("=== Check 3: OpenSky aircraft history(%s) ===", args.icao24)
        reports.append(check_opensky_aircraft_history(args.icao24))

    if run_all or run_check == "faa-master":
        log.info("=== Check 4: FAA MASTER.txt ===")
        reports.append(check_faa_master(Path(args.download_dir)))

    mapping = emit_field_mapping() if run_all else None

    summary = {
        "ran_at_utc": datetime.now(timezone.utc).isoformat(),
        "total_checks": len(reports),
        "passed": sum(1 for r in reports if r.success),
        "failed": sum(1 for r in reports if not r.success),
        "reports": [
            {
                **asdict(r),
                "coverage": round(r.coverage, 3),
            }
            for r in reports
        ],
        "field_mapping": mapping,
    }

    out = json.dumps(summary, indent=2, default=str, ensure_ascii=False)
    print(out)
    if args.output:
        Path(args.output).write_text(out, encoding="utf-8")
        log.info("reporte guardado en %s", args.output)

    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
