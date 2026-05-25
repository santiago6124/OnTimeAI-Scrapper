"""SQLite + GCS — schema, UPSERTs y sync con el bucket compartido del backend.

El harvester comparte `live_data.db` con OnTimeAI-Backend. Schema de `flights`,
`actuals`, `weather_obs`, `predictions`, `runs` es propiedad del backend
(ver ontimeai/live.py). Acá solo replicamos esos CREATE TABLE IF NOT EXISTS
(idempotente) y agregamos las tablas nuevas del harvester.

Concurrencia: si el live_pull del backend corre simultáneamente, el último
upload gana. Mitigación: staggear crons (live_pull cada 30min en :00/:30,
harvester cada 15min en :05/:20/:35/:50). Pérdida tolerable: hasta 1 tick.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from . import config

log = logging.getLogger(__name__)


# -- Schema --------------------------------------------------------------------

# Replicado idéntico al backend (live.py:92-168). NO modificar sin actualizar
# el backend en paralelo — el feature builder espera estos nombres exactos.
BACKEND_SCHEMA = """
CREATE TABLE IF NOT EXISTS flights (
    fa_flight_id TEXT PRIMARY KEY,
    stable_id TEXT,
    ident_iata TEXT,
    op_carrier TEXT,
    flight_number TEXT,
    tail_num TEXT,
    origin TEXT,
    dest TEXT,
    inbound_fa_flight_id TEXT,
    fl_date TEXT,
    crs_dep_min INTEGER,
    scheduled_out_utc TEXT,
    scheduled_off_utc TEXT,
    scheduled_on_utc TEXT,
    scheduled_in_utc TEXT,
    crs_elapsed_min REAL,
    distance REAL,
    aircraft_type TEXT,
    cancelled INTEGER,
    diverted INTEGER,
    first_seen_utc TEXT NOT NULL,
    last_updated_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_flights_date_dep ON flights(fl_date, scheduled_off_utc);
CREATE INDEX IF NOT EXISTS idx_flights_tail ON flights(tail_num, scheduled_off_utc);
CREATE INDEX IF NOT EXISTS idx_flights_carrier ON flights(op_carrier, scheduled_off_utc);

CREATE TABLE IF NOT EXISTS actuals (
    fa_flight_id TEXT PRIMARY KEY,
    stable_id TEXT,
    actual_out_utc TEXT,
    actual_off_utc TEXT,
    actual_on_utc TEXT,
    actual_in_utc TEXT,
    arr_delay_min REAL,
    departure_delay_min REAL,
    cancelled INTEGER,
    diverted INTEGER,
    settled_at_utc TEXT NOT NULL
);
"""

HARVESTER_SCHEMA = """
CREATE TABLE IF NOT EXISTS tail_lineage_cache (
    tail TEXT PRIMARY KEY,
    hydrated_until TEXT NOT NULL,
    last_pull_source TEXT NOT NULL,
    last_pull_ok INTEGER NOT NULL,
    consecutive_failures INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tail_lineage_hydrated_until
    ON tail_lineage_cache(hydrated_until);

CREATE TABLE IF NOT EXISTS tail_to_icao24_lookup (
    icao24 TEXT PRIMARY KEY,
    n_number TEXT NOT NULL,
    aircraft_type TEXT,
    aircraft_year INTEGER,
    last_synced TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tail_to_icao24_n_number
    ON tail_to_icao24_lookup(n_number);

CREATE TABLE IF NOT EXISTS harvester_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at_utc TEXT NOT NULL,
    layer TEXT NOT NULL,
    n_calls INTEGER,
    n_flights_upserted INTEGER,
    n_actuals_upserted INTEGER,
    n_tails_hydrated INTEGER,
    duration_seconds REAL,
    status TEXT,
    error TEXT
);

-- Mejora #1: ADS-B real-time positions from OpenSky bbox query.
-- Captured each tick (~15 min cadence) for aircraft in a bbox around KATL.
-- Backend can compute time-to-ATL ETAs by joining via callsign or icao24
-- (callsign maps to op_carrier+flight_number when ICAO format like DAL123).
CREATE TABLE IF NOT EXISTS aircraft_position (
    icao24 TEXT NOT NULL,
    captured_at_utc TEXT NOT NULL,
    callsign TEXT,
    registration TEXT,             -- N-number (tail_num) when source provides it
    aircraft_type TEXT,            -- ICAO typecode when available
    lat REAL,
    lon REAL,
    baro_altitude_m REAL,
    geo_altitude_m REAL,
    velocity_mps REAL,
    true_track_deg REAL,
    vertical_rate_mps REAL,
    on_ground INTEGER,
    origin_country TEXT,
    source TEXT,                   -- 'airplanes.live' | 'opensky'
    PRIMARY KEY (icao24, captured_at_utc)
);
CREATE INDEX IF NOT EXISTS idx_aircraft_position_callsign
    ON aircraft_position(callsign, captured_at_utc);
CREATE INDEX IF NOT EXISTS idx_aircraft_position_captured
    ON aircraft_position(captured_at_utc);
-- idx_aircraft_position_registration created lazily in _migrate_aircraft_position
-- because the column was added after the initial table layout.
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Crea tablas si no existen + migra columnas nuevas. Idempotente."""
    conn.executescript(BACKEND_SCHEMA)
    conn.executescript(HARVESTER_SCHEMA)
    _migrate_aircraft_position(conn)
    conn.commit()


def _migrate_aircraft_position(conn: sqlite3.Connection) -> None:
    """ALTER TABLE para columnas agregadas post-creación inicial.

    CREATE TABLE IF NOT EXISTS no altera tablas existentes. Si el harvester
    corrió antes de Mejora #1 con menos columnas, agregamos las que faltan.
    """
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(aircraft_position)").fetchall()}
    except sqlite3.OperationalError:
        return  # table doesn't exist yet; CREATE will handle it
    if not cols:
        return
    if "registration" not in cols:
        conn.execute("ALTER TABLE aircraft_position ADD COLUMN registration TEXT")
    if "aircraft_type" not in cols:
        conn.execute("ALTER TABLE aircraft_position ADD COLUMN aircraft_type TEXT")
    if "source" not in cols:
        conn.execute("ALTER TABLE aircraft_position ADD COLUMN source TEXT")
    # Index creation is idempotent and safe now that column exists.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_aircraft_position_registration "
                 "ON aircraft_position(registration, captured_at_utc)")


@contextmanager
def open_db(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    """Context manager. Activa WAL + foreign keys, asegura schema, commit al salir."""
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    ensure_schema(conn)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# -- UPSERTs -------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def upsert_flights(conn: sqlite3.Connection, rows: Iterable[dict[str, Any]]) -> int:
    """UPSERT en flights. `rows` deben tener al menos fa_flight_id. Devuelve count escrito."""
    now = _now_iso()
    count = 0
    sql = """
    INSERT INTO flights (
        fa_flight_id, stable_id, ident_iata, op_carrier, flight_number,
        tail_num, origin, dest, inbound_fa_flight_id,
        fl_date, crs_dep_min,
        scheduled_out_utc, scheduled_off_utc, scheduled_on_utc, scheduled_in_utc,
        crs_elapsed_min, distance, aircraft_type, cancelled, diverted,
        first_seen_utc, last_updated_utc
    ) VALUES (
        :fa_flight_id, :stable_id, :ident_iata, :op_carrier, :flight_number,
        :tail_num, :origin, :dest, :inbound_fa_flight_id,
        :fl_date, :crs_dep_min,
        :scheduled_out_utc, :scheduled_off_utc, :scheduled_on_utc, :scheduled_in_utc,
        :crs_elapsed_min, :distance, :aircraft_type, :cancelled, :diverted,
        :first_seen, :last_updated
    )
    ON CONFLICT(fa_flight_id) DO UPDATE SET
        stable_id = COALESCE(excluded.stable_id, flights.stable_id),
        ident_iata = COALESCE(excluded.ident_iata, flights.ident_iata),
        op_carrier = COALESCE(excluded.op_carrier, flights.op_carrier),
        flight_number = COALESCE(excluded.flight_number, flights.flight_number),
        tail_num = COALESCE(excluded.tail_num, flights.tail_num),
        origin = COALESCE(excluded.origin, flights.origin),
        dest = COALESCE(excluded.dest, flights.dest),
        inbound_fa_flight_id = COALESCE(excluded.inbound_fa_flight_id, flights.inbound_fa_flight_id),
        fl_date = COALESCE(excluded.fl_date, flights.fl_date),
        crs_dep_min = COALESCE(excluded.crs_dep_min, flights.crs_dep_min),
        scheduled_out_utc = COALESCE(excluded.scheduled_out_utc, flights.scheduled_out_utc),
        scheduled_off_utc = COALESCE(excluded.scheduled_off_utc, flights.scheduled_off_utc),
        scheduled_on_utc = COALESCE(excluded.scheduled_on_utc, flights.scheduled_on_utc),
        scheduled_in_utc = COALESCE(excluded.scheduled_in_utc, flights.scheduled_in_utc),
        crs_elapsed_min = COALESCE(excluded.crs_elapsed_min, flights.crs_elapsed_min),
        distance = COALESCE(excluded.distance, flights.distance),
        aircraft_type = COALESCE(excluded.aircraft_type, flights.aircraft_type),
        cancelled = COALESCE(excluded.cancelled, flights.cancelled),
        diverted = COALESCE(excluded.diverted, flights.diverted),
        last_updated_utc = excluded.last_updated_utc
    """
    for row in rows:
        if not row.get("fa_flight_id"):
            continue
        params = {
            "fa_flight_id": row["fa_flight_id"],
            "stable_id": row.get("stable_id"),
            "ident_iata": row.get("ident_iata"),
            "op_carrier": row.get("op_carrier"),
            "flight_number": row.get("flight_number"),
            "tail_num": row.get("tail_num"),
            "origin": row.get("origin"),
            "dest": row.get("dest"),
            "inbound_fa_flight_id": row.get("inbound_fa_flight_id"),
            "fl_date": row.get("fl_date"),
            "crs_dep_min": row.get("crs_dep_min"),
            "scheduled_out_utc": row.get("scheduled_out_utc"),
            "scheduled_off_utc": row.get("scheduled_off_utc"),
            "scheduled_on_utc": row.get("scheduled_on_utc"),
            "scheduled_in_utc": row.get("scheduled_in_utc"),
            "crs_elapsed_min": row.get("crs_elapsed_min"),
            "distance": row.get("distance"),
            "aircraft_type": row.get("aircraft_type"),
            "cancelled": row.get("cancelled"),
            "diverted": row.get("diverted"),
            "first_seen": now,
            "last_updated": now,
        }
        conn.execute(sql, params)
        count += 1
    return count


def upsert_actuals(conn: sqlite3.Connection, rows: Iterable[dict[str, Any]]) -> int:
    """UPSERT en actuals. Solo guarda filas con al menos un campo real (out/off/on/in)."""
    now = _now_iso()
    count = 0
    sql = """
    INSERT INTO actuals (
        fa_flight_id, stable_id,
        actual_out_utc, actual_off_utc, actual_on_utc, actual_in_utc,
        arr_delay_min, departure_delay_min,
        cancelled, diverted, settled_at_utc
    ) VALUES (
        :fa_flight_id, :stable_id,
        :actual_out_utc, :actual_off_utc, :actual_on_utc, :actual_in_utc,
        :arr_delay_min, :departure_delay_min,
        :cancelled, :diverted, :settled_at_utc
    )
    ON CONFLICT(fa_flight_id) DO UPDATE SET
        stable_id = COALESCE(excluded.stable_id, actuals.stable_id),
        actual_out_utc = COALESCE(excluded.actual_out_utc, actuals.actual_out_utc),
        actual_off_utc = COALESCE(excluded.actual_off_utc, actuals.actual_off_utc),
        actual_on_utc = COALESCE(excluded.actual_on_utc, actuals.actual_on_utc),
        actual_in_utc = COALESCE(excluded.actual_in_utc, actuals.actual_in_utc),
        arr_delay_min = COALESCE(excluded.arr_delay_min, actuals.arr_delay_min),
        departure_delay_min = COALESCE(excluded.departure_delay_min, actuals.departure_delay_min),
        cancelled = COALESCE(excluded.cancelled, actuals.cancelled),
        diverted = COALESCE(excluded.diverted, actuals.diverted),
        settled_at_utc = excluded.settled_at_utc
    """
    for row in rows:
        if not row.get("fa_flight_id"):
            continue
        has_any_actual = any(
            row.get(k) is not None for k in ("actual_out_utc", "actual_off_utc", "actual_on_utc", "actual_in_utc")
        )
        if not has_any_actual:
            continue
        params = {
            "fa_flight_id": row["fa_flight_id"],
            "stable_id": row.get("stable_id"),
            "actual_out_utc": row.get("actual_out_utc"),
            "actual_off_utc": row.get("actual_off_utc"),
            "actual_on_utc": row.get("actual_on_utc"),
            "actual_in_utc": row.get("actual_in_utc"),
            "arr_delay_min": row.get("arr_delay_min"),
            "departure_delay_min": row.get("departure_delay_min"),
            "cancelled": row.get("cancelled"),
            "diverted": row.get("diverted"),
            "settled_at_utc": now,
        }
        conn.execute(sql, params)
        count += 1
    return count


def upsert_aircraft_positions(
    conn: sqlite3.Connection, positions: Iterable[dict[str, Any]]
) -> int:
    """UPSERT en aircraft_position. Cada `position` viene normalizada de
    airplanes.live o OpenSky (Mejora #1). Returns count written."""
    sql = """
    INSERT OR REPLACE INTO aircraft_position (
        icao24, captured_at_utc, callsign, registration, aircraft_type,
        lat, lon, baro_altitude_m, geo_altitude_m, velocity_mps,
        true_track_deg, vertical_rate_mps, on_ground, origin_country, source
    ) VALUES (
        :icao24, :captured_at_utc, :callsign, :registration, :aircraft_type,
        :lat, :lon, :baro_altitude_m, :geo_altitude_m, :velocity_mps,
        :true_track_deg, :vertical_rate_mps, :on_ground, :origin_country, :source
    )
    """
    count = 0
    for p in positions:
        if not p.get("icao24") or not p.get("captured_at_utc"):
            continue
        # Ensure all expected keys exist (fill missing with None)
        for k in ("registration", "aircraft_type", "origin_country", "source"):
            p.setdefault(k, None)
        conn.execute(sql, p)
        count += 1
    return count


def record_harvester_run(
    conn: sqlite3.Connection,
    *,
    layer: str,
    n_calls: int,
    n_flights_upserted: int,
    n_actuals_upserted: int,
    n_tails_hydrated: int,
    duration_seconds: float,
    status: str,
    error: str | None = None,
) -> int:
    """Append-only log de cada ejecución del harvester."""
    cur = conn.execute(
        """
        INSERT INTO harvester_runs (
            run_at_utc, layer, n_calls, n_flights_upserted, n_actuals_upserted,
            n_tails_hydrated, duration_seconds, status, error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _now_iso(),
            layer,
            n_calls,
            n_flights_upserted,
            n_actuals_upserted,
            n_tails_hydrated,
            duration_seconds,
            status,
            error,
        ),
    )
    return int(cur.lastrowid or 0)


# -- GCS sync ------------------------------------------------------------------


def _gcs_client():
    from google.cloud import storage  # import lazy

    return storage.Client(project=config.GCP_PROJECT) if config.GCP_PROJECT else storage.Client()


def download_db_from_gcs(local_path: str | Path = config.LOCAL_DB_PATH) -> Path:
    """Descarga gs://{GCS_BUCKET}/{GCS_DB_BLOB} a local_path. Si no existe en GCS, crea archivo vacío."""
    local = Path(local_path)
    local.parent.mkdir(parents=True, exist_ok=True)
    client = _gcs_client()
    bucket = client.bucket(config.GCS_BUCKET)
    blob = bucket.blob(config.GCS_DB_BLOB)
    if not blob.exists():
        log.warning("DB no existe en gs://%s/%s — creando archivo vacío", config.GCS_BUCKET, config.GCS_DB_BLOB)
        local.write_bytes(b"")
        with open_db(local):  # crea schema en archivo vacío
            pass
        return local
    log.info("downloading gs://%s/%s -> %s", config.GCS_BUCKET, config.GCS_DB_BLOB, local)
    blob.download_to_filename(str(local))
    log.info("downloaded %d bytes", local.stat().st_size)
    return local


def upload_db_to_gcs(local_path: str | Path = config.LOCAL_DB_PATH) -> None:
    """Sube local_path a gs://{GCS_BUCKET}/{GCS_DB_BLOB}. Pisa la versión anterior."""
    local = Path(local_path)
    if not local.exists():
        raise FileNotFoundError(f"local DB no existe: {local}")
    client = _gcs_client()
    bucket = client.bucket(config.GCS_BUCKET)
    blob = bucket.blob(config.GCS_DB_BLOB)
    log.info("uploading %s -> gs://%s/%s", local, config.GCS_BUCKET, config.GCS_DB_BLOB)
    blob.upload_from_filename(str(local))
    log.info("upload OK (%d bytes)", local.stat().st_size)
