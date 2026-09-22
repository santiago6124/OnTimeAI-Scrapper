"""Capa 2 — Cache de hidratación por tail + SQL chain walk para inbound_fa_flight_id.

`maybe_hydrate_tail(conn, fr24_client, tail)`:
  1. Si el tail está en cache fresco (< freshness_hours) → skip (cache hit).
  2. Si tiene N+ failures consecutivos → skip hasta próximo refresh manual.
  3. Sino: fetch_aircraft_history(tail) → UPSERT flights + actuals → populate inbound chain → update cache.

`populate_inbound_chain(conn, tail)`:
  Para cada vuelo del tail, busca el vuelo previo del mismo tail que aterrizó en
  el origen actual antes de la salida programada — y lo setea como inbound_fa_flight_id.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from enum import Enum

from . import config, db
from .fr24_client import FR24Client, FR24EmptyResponseError, FR24Error, FR24RateLimitedError
from .fr24_http import fetch_aircraft_history

log = logging.getLogger(__name__)


class HydrationStatus(str, Enum):
    HIT_CACHE = "hit_cache"          # cache fresco, sin call
    HYDRATED = "hydrated"            # call ok, datos escritos
    EMPTY = "empty"                  # call ok pero sin items
    SKIPPED_FAILURES = "skipped"     # demasiados failures, esperando ventana
    RATE_LIMITED = "rate_limited"    # Cloudflare/429/403
    FAILED = "failed"                # error transitorio


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")


def pending_outcome_tails(
    conn: sqlite3.Connection,
    *,
    grace_minutes: int = config.PENDING_OUTCOME_GRACE_MINUTES,
    lookback_hours: int = config.PENDING_OUTCOME_LOOKBACK_HOURS,
) -> set[str]:
    """Tails with predicted flights whose expected arrival passed without an actual."""
    try:
        rows = conn.execute(
            f"""
            SELECT DISTINCT f.tail_num
            FROM predictions p
            JOIN flights f ON f.fa_flight_id = p.fa_flight_id
            LEFT JOIN actuals a ON a.fa_flight_id = f.fa_flight_id
            WHERE f.tail_num IS NOT NULL AND f.tail_num <> ''
              AND COALESCE(f.cancelled, 0) = 0
              AND a.actual_in_utc IS NULL
              AND COALESCE(f.estimated_in_utc, f.scheduled_in_utc) IS NOT NULL
              AND datetime(COALESCE(f.estimated_in_utc, f.scheduled_in_utc))
                    <= datetime('now', '-{int(grace_minutes)} minutes')
              AND datetime(COALESCE(f.estimated_in_utc, f.scheduled_in_utc))
                    >= datetime('now', '-{int(lookback_hours)} hours')
            """
        ).fetchall()
    except sqlite3.OperationalError:
        return set()
    return {str(row[0]).strip().upper() for row in rows if row[0]}


def pending_outcome_tail_order(
    conn: sqlite3.Connection,
    *,
    grace_minutes: int = config.PENDING_OUTCOME_GRACE_MINUTES,
    lookback_hours: int = config.PENDING_OUTCOME_LOOKBACK_HOURS,
) -> list[str]:
    """Pending tails ordered by the oldest unresolved expected arrival first."""
    try:
        rows = conn.execute(
            f"""
            SELECT f.tail_num,
                   MIN(datetime(COALESCE(f.estimated_in_utc, f.scheduled_in_utc))) AS due_at,
                   cache.hydrated_until AS last_refresh
            FROM predictions p
            JOIN flights f ON f.fa_flight_id = p.fa_flight_id
            LEFT JOIN actuals a ON a.fa_flight_id = f.fa_flight_id
            LEFT JOIN tail_lineage_cache cache ON cache.tail = f.tail_num
            WHERE f.tail_num IS NOT NULL AND f.tail_num <> ''
              AND COALESCE(f.cancelled, 0) = 0
              AND a.actual_in_utc IS NULL
              AND COALESCE(f.estimated_in_utc, f.scheduled_in_utc) IS NOT NULL
              AND datetime(COALESCE(f.estimated_in_utc, f.scheduled_in_utc))
                    <= datetime('now', '-{int(grace_minutes)} minutes')
              AND datetime(COALESCE(f.estimated_in_utc, f.scheduled_in_utc))
                    >= datetime('now', '-{int(lookback_hours)} hours')
            GROUP BY f.tail_num
            -- Fair queue: never/least-recently refreshed tails go first.  If we
            -- sorted only by due_at, permanently missing flights would consume
            -- the first budget slots every hour and starve newer long-hauls.
            ORDER BY COALESCE(last_refresh, '1970-01-01T00:00:00+00:00') ASC,
                     due_at ASC, f.tail_num ASC
            """
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [str(row[0]).strip().upper() for row in rows if row[0]]


def _adaptive_freshness_hours(
    conn: sqlite3.Connection, tail: str, default_hours: int
) -> int:
    """TTL adaptativo según frecuencia de operación del tail (Mejora #5).

    Tails con muchos legs/día → refresh más frecuente.
    Tails ocasionales → TTL largo (menos AeroAPI burn).

    Fórmula: clip(24 / legs_today, 2h, 24h). Si la query falla, fallback al
    default configurado.
    """
    try:
        today = _now().strftime("%Y-%m-%d")
        row = conn.execute(
            """SELECT COUNT(*) AS n FROM flights
               WHERE tail_num = ?
                 AND fl_date = ?""",
            (tail, today),
        ).fetchone()
        legs_today = int(row["n"] if row else 0)
    except Exception:
        return default_hours

    if legs_today >= 5:
        return 2   # high-frequency tail
    if legs_today >= 2:
        return default_hours  # 6h default
    return 24  # low-frequency / one-leg / never-seen-today


def maybe_hydrate_tail(
    conn: sqlite3.Connection,
    fr24_client: FR24Client,
    tail: str,
    *,
    freshness_hours: int = config.LINEAGE_FRESHNESS_HOURS,
    max_failures: int = config.LINEAGE_MAX_CONSECUTIVE_FAILURES,
    limit: int = config.FR24_HISTORY_DEFAULT_LIMIT,
    capture_future_legs: bool = config.CAPTURE_FUTURE_LEGS,
    force_refresh: bool = False,
) -> tuple[HydrationStatus, int, int]:
    """Hidrata el historial de un tail si está stale. Idempotente vía cache.

    Con capture_future_legs, además del historial retiene las piernas ATL futuras
    del itinerario (placeholders sintéticos) para adelantar el descubrimiento.

    Returns: (status, n_flights_upserted, n_actuals_upserted)
    """
    tail = tail.strip().upper()
    if not tail:
        return HydrationStatus.EMPTY, 0, 0

    # Adaptive TTL based on tail activity today (overrides freshness_hours arg).
    effective_freshness = _adaptive_freshness_hours(conn, tail, freshness_hours)

    row = conn.execute(
        "SELECT hydrated_until, consecutive_failures FROM tail_lineage_cache WHERE tail = ?",
        (tail,),
    ).fetchone()

    now = _now()
    if row is not None:
        try:
            hydrated_until = datetime.fromisoformat(row["hydrated_until"])
            if hydrated_until.tzinfo is None:
                hydrated_until = hydrated_until.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            hydrated_until = now - timedelta(days=365)

        if not force_refresh and hydrated_until > now - timedelta(hours=effective_freshness):
            return HydrationStatus.HIT_CACHE, 0, 0
        if (row["consecutive_failures"] or 0) >= max_failures:
            return HydrationStatus.SKIPPED_FAILURES, 0, 0

    # Fetch
    try:
        _raw, flights_rows, actuals_rows = fetch_aircraft_history(
            fr24_client, tail, limit=limit, capture_future_legs=capture_future_legs
        )
    except FR24EmptyResponseError:
        _bump_cache(conn, tail, ok=True, source="fr24", now=now)
        return HydrationStatus.EMPTY, 0, 0
    except FR24RateLimitedError as exc:
        _bump_cache(conn, tail, ok=False, source="fr24", now=now)
        log.warning("hydrate(%s) rate-limited: %s", tail, exc)
        return HydrationStatus.RATE_LIMITED, 0, 0
    except FR24Error as exc:
        _bump_cache(conn, tail, ok=False, source="fr24", now=now)
        log.warning("hydrate(%s) failed: %s", tail, exc)
        return HydrationStatus.FAILED, 0, 0

    n_flights = db.upsert_flights(conn, flights_rows)
    n_actuals = db.upsert_actuals(conn, actuals_rows)
    populate_inbound_chain(conn, tail)
    _bump_cache(conn, tail, ok=True, source="fr24", now=now)

    return HydrationStatus.HYDRATED, n_flights, n_actuals


def _bump_cache(
    conn: sqlite3.Connection, tail: str, *, ok: bool, source: str, now: datetime
) -> None:
    if ok:
        conn.execute(
            """
            INSERT INTO tail_lineage_cache (tail, hydrated_until, last_pull_source, last_pull_ok, consecutive_failures)
            VALUES (?, ?, ?, 1, 0)
            ON CONFLICT(tail) DO UPDATE SET
                hydrated_until = excluded.hydrated_until,
                last_pull_source = excluded.last_pull_source,
                last_pull_ok = 1,
                consecutive_failures = 0
            """,
            (tail, _iso(now), source),
        )
    else:
        conn.execute(
            """
            INSERT INTO tail_lineage_cache (tail, hydrated_until, last_pull_source, last_pull_ok, consecutive_failures)
            VALUES (?, ?, ?, 0, 1)
            ON CONFLICT(tail) DO UPDATE SET
                last_pull_source = excluded.last_pull_source,
                last_pull_ok = 0,
                consecutive_failures = COALESCE(tail_lineage_cache.consecutive_failures, 0) + 1
            """,
            (tail, _iso(now), source),
        )


def populate_inbound_chain(conn: sqlite3.Connection, tail: str, *, overwrite: bool = False) -> int:
    """Resuelve inbound_fa_flight_id para cada vuelo del tail.

    Estrategia: para cada vuelo F de tail T con origen ORIG y scheduled_out S:
        inbound = max(f2.fa_flight_id) tal que
                    f2.tail_num = T
                AND f2.dest = ORIG
                AND f2.actual_in_utc < S
                AND f2.fa_flight_id != F.fa_flight_id
        ordenado por actual_in_utc DESC LIMIT 1

    Devuelve cantidad de filas que recibieron un inbound_fa_flight_id.
    """
    tail = tail.upper()
    rows = conn.execute(
        """
        SELECT fa_flight_id, origin, scheduled_out_utc, inbound_fa_flight_id
        FROM flights
        WHERE tail_num = ?
          AND origin IS NOT NULL
          AND scheduled_out_utc IS NOT NULL
        ORDER BY scheduled_out_utc
        """,
        (tail,),
    ).fetchall()

    n_updated = 0
    for r in rows:
        if not overwrite and r["inbound_fa_flight_id"]:
            continue
        inbound_row = conn.execute(
            """
            SELECT f.fa_flight_id
            FROM flights f
            JOIN actuals a ON a.fa_flight_id = f.fa_flight_id
            WHERE f.tail_num = ?
              AND f.dest = ?
              AND a.actual_in_utc IS NOT NULL
              AND a.actual_in_utc < ?
              AND f.fa_flight_id != ?
            ORDER BY a.actual_in_utc DESC
            LIMIT 1
            """,
            (tail, r["origin"], r["scheduled_out_utc"], r["fa_flight_id"]),
        ).fetchone()
        if inbound_row is None:
            continue
        conn.execute(
            "UPDATE flights SET inbound_fa_flight_id = ?, last_updated_utc = ? "
            "WHERE fa_flight_id = ?",
            (inbound_row["fa_flight_id"], _iso(_now()), r["fa_flight_id"]),
        )
        n_updated += 1

    return n_updated


def salidas_sin_predecir_tail_order(
    conn: sqlite3.Connection,
    *,
    horizonte_horas: int = 8,
) -> list[str]:
    """Matriculas con una salida del anchor proxima que todavia no se predijo.

    Es el hueco medido: de 2.210 salidas de ATL sin prediccion entre el 17 y el
    20/09, 2.209 las operaba un avion que ya teniamos en la base. No eran
    inalcanzables; nunca les consultamos el itinerario a tiempo.

    Ordenadas por salida mas inminente primero. El resto de las categorias se
    recorren con `sorted(candidate_tails)`, o sea alfabeticamente, y con eso un
    avion que sale en media hora compite contra uno que sale maniana y decide
    el abecedario.
    """
    try:
        filas = conn.execute(
            """
            SELECT f.tail_num, MIN(f.scheduled_out_utc) AS sale
              FROM flights f
             WHERE f.tail_num IS NOT NULL AND f.tail_num <> ''
               AND f.origin IN ('ATL', 'KATL')
               AND COALESCE(f.cancelled, 0) = 0
               AND f.scheduled_out_utc > strftime('%Y-%m-%dT%H:%M:%S', 'now')
               AND f.scheduled_out_utc < strftime(
                     '%Y-%m-%dT%H:%M:%S', 'now', ?)
               AND NOT EXISTS (SELECT 1 FROM predictions p
                                WHERE p.fa_flight_id = f.fa_flight_id)
             GROUP BY f.tail_num
             ORDER BY sale
            """,
            (f"+{int(horizonte_horas)} hours",),
        ).fetchall()
    except Exception:  # noqa: BLE001 - base incompleta en bootstrap
        return []
    return [r[0].strip().upper() for r in filas if r[0]]


def select_tails_to_hydrate(
    conn: sqlite3.Connection,
    candidate_tails: set[str],
    *,
    budget: int = 30,
    freshness_hours: int = config.LINEAGE_FRESHNESS_HOURS,
) -> list[str]:
    """Selecciona hasta `budget` tails que necesitan hidratación.

    Priority order (highest -> lowest):
      1. bootstrap_pending - backend explicitly requested via placeholder rows
      2. sin_predecir      - salida del anchor proxima y todavia sin prediccion
      3. predicted_today   - in flights with a prediction scheduled today
      3. high_freq         - >= 5 legs today (adaptive TTL 2h)
      4. never             - never-hydrated tails (cache miss)
      5. expired           - stale cache (TTL elapsed)

    The first classes are "lineage-critical": they directly affect today's
    predictions. They jump ahead of the general high_freq queue so the limited
    per-tick budget always lands on tails the model is about to score.

    `sin_predecir` va antes que `predicted_today` a proposito. Sin ella el
    presupuesto se gastaba reforzando los aviones que YA estabamos prediciendo
    —un circulo que mantenia cubierto lo cubierto y dejaba el hueco intacto—.
    Subir el presupuesto de 30 a 70 no movio la cobertura por esto: los 40
    lugares nuevos fueron a mas de lo mismo.
    """
    if not candidate_tails:
        return []

    placeholders = ",".join("?" * len(candidate_tails))
    rows = conn.execute(
        f"""
        SELECT tail, hydrated_until, consecutive_failures, last_pull_source
        FROM tail_lineage_cache
        WHERE tail IN ({placeholders})
        """,
        tuple(sorted(candidate_tails)),
    ).fetchall()
    cached = {r["tail"]: r for r in rows}

    # Pull the set of tails that have predictions scheduled today.
    # These are the tails the backend *just* predicted on - the lineage data
    # for them is what powers today's AUC. Highest priority.
    predicted_today: set[str] = set()
    try:
        today_rows = conn.execute(
            """
            SELECT DISTINCT f.tail_num FROM predictions p
            JOIN flights f ON f.fa_flight_id = p.fa_flight_id
            WHERE substr(p.predicted_at_utc, 1, 10) = strftime('%Y-%m-%d', 'now')
              AND f.tail_num IS NOT NULL AND f.tail_num <> ''
            """
        ).fetchall()
        predicted_today = {r[0].strip().upper() for r in today_rows if r[0]}
    except Exception:  # noqa: BLE001 - tolerate missing table in early bootstrap
        pass

    pending_outcome_order = pending_outcome_tail_order(conn)
    pending_outcomes = set(pending_outcome_order)
    sin_predecir_order = salidas_sin_predecir_tail_order(conn)
    sin_predecir_set = set(sin_predecir_order)

    now = _now()
    bootstrap_pending: list[str] = []
    pending_outcome_list: list[str] = []
    sin_predecir_list: list[str] = []
    predicted_today_list: list[str] = []
    high_freq: list[str] = []
    never: list[str] = []
    expired: list[str] = []

    for t in sorted(candidate_tails):
        ttl_hours = _adaptive_freshness_hours(conn, t, freshness_hours)
        threshold = now - timedelta(hours=ttl_hours)
        crow = cached.get(t)

        # Bootstrap-pending = highest priority. The backend explicitly asked
        # for this tail; we should not let ADS-B noise outrank it.
        if crow is not None and (crow["last_pull_source"] or "") == "bootstrap-request":
            bootstrap_pending.append(t)
            continue

        # A predicted flight that should already have arrived bypasses the
        # adaptive 6-24h lineage TTL, but still observes a smaller retry interval
        # so a delayed flight does not consume one FR24 call every harvester tick.
        if t in pending_outcomes:
            if crow is None:
                pending_outcome_list.append(t)
                continue
            try:
                hu = datetime.fromisoformat(crow["hydrated_until"])
                if hu.tzinfo is None:
                    hu = hu.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                pending_outcome_list.append(t)
                continue
            refresh_before = now - timedelta(minutes=config.PENDING_OUTCOME_REFRESH_MINUTES)
            if (hu < refresh_before
                    and (crow["consecutive_failures"] or 0)
                    < config.LINEAGE_MAX_CONSECUTIVE_FAILURES):
                pending_outcome_list.append(t)
            continue

        # Salida proxima y sin predecir: es el hueco. Va antes que
        # predicted_today porque ese avion todavia no tiene prediccion y este
        # es el unico momento en que se puede conseguir.
        if t in sin_predecir_set:
            if crow is None:
                sin_predecir_list.append(t)
                continue
            try:
                hu = datetime.fromisoformat(crow["hydrated_until"])
                if hu.tzinfo is None:
                    hu = hu.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                sin_predecir_list.append(t)
                continue
            if (hu < threshold
                    and (crow["consecutive_failures"] or 0)
                    < config.LINEAGE_MAX_CONSECUTIVE_FAILURES):
                sin_predecir_list.append(t)
            continue

        # Predicted-today. We're going to score this tail
        # in the next backend tick and we want lineage data to drive that.
        if t in predicted_today:
            if crow is None:
                predicted_today_list.append(t)
                continue
            try:
                hu = datetime.fromisoformat(crow["hydrated_until"])
                if hu.tzinfo is None:
                    hu = hu.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                predicted_today_list.append(t)
                continue
            if (hu < threshold
                    and (crow["consecutive_failures"] or 0)
                    < config.LINEAGE_MAX_CONSECUTIVE_FAILURES):
                predicted_today_list.append(t)
            continue

        # Default classification for general candidate tails.
        if crow is None:
            (high_freq if ttl_hours <= 2 else never).append(t)
            continue
        try:
            hu = datetime.fromisoformat(crow["hydrated_until"])
            if hu.tzinfo is None:
                hu = hu.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            expired.append(t)
            continue
        if (hu < threshold
                and (crow["consecutive_failures"] or 0)
                < config.LINEAGE_MAX_CONSECUTIVE_FAILURES):
            (high_freq if ttl_hours <= 2 else expired).append(t)

    pending_rank = {tail: index for index, tail in enumerate(pending_outcome_order)}
    pending_outcome_list.sort(key=lambda tail: pending_rank.get(tail, len(pending_rank)))
    # Dentro de `sin_predecir`, la salida mas inminente primero: si el
    # presupuesto no alcanza para todas, que se gaste en las que estan por irse.
    rank_sp = {tail: i for i, tail in enumerate(sin_predecir_order)}
    sin_predecir_list.sort(key=lambda tail: rank_sp.get(tail, len(rank_sp)))
    ordered = (
        bootstrap_pending + pending_outcome_list + sin_predecir_list
        + predicted_today_list + high_freq + never + expired
    )
    return ordered[:budget]


def purge_stale_cache(
    conn: sqlite3.Connection,
    *,
    days: int = 30,
) -> int:
    """Delete tail_lineage_cache rows whose hydrated_until is older than `days`.

    Bootstrap-pending placeholder rows (hydrated_until = '1970-01-01...') are
    excluded — those are explicit requests from the backend waiting to be
    fulfilled and must not be dropped. Returns number of rows deleted.
    """
    cutoff_iso = _iso(_now() - timedelta(days=days))
    cur = conn.execute(
        """DELETE FROM tail_lineage_cache
           WHERE hydrated_until < ?
             AND COALESCE(last_pull_source, '') <> 'bootstrap-request'""",
        (cutoff_iso,),
    )
    conn.commit()
    return cur.rowcount or 0
