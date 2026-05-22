"""Entrypoint del Cloud Run Job — Capa 1 (ATL anchor).

Flow:
  1. Descargar live_data.db desde GCS (o usar --local-db)
  2. Asegurar schema (idempotente)
  3. Pull arrivals + departures de KATL paginados via FR24Client
  4. Normalizar y UPSERT en flights + actuals
  5. Record en harvester_runs
  6. Subir live_data.db a GCS

Capa 2 y Capa 3 son módulos separados que se invocan tras Capa 1.
Para correr Capa 1 sola:  python -m ontimeai_scrapper.harvester
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config, db
from .fr24_client import (
    FR24Client,
    FR24Error,
    FR24Page,
    FR24RateLimitedError,
    normalize_actual,
    normalize_flight,
)
from .lineage_cache import (
    HydrationStatus,
    maybe_hydrate_tail,
    select_tails_to_hydrate,
)

logging.basicConfig(
    level=config.LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("harvester")


# -- Capa 1 --------------------------------------------------------------------


@dataclass
class HarvestStats:
    layer: str = "atl_anchor"
    n_calls: int = 0
    n_flights_upserted: int = 0
    n_actuals_upserted: int = 0
    n_tails_hydrated: int = 0
    duration_seconds: float = 0.0
    status: str = "ok"
    error: str | None = None
    pages_visited: list[int] = field(default_factory=list)
    unique_tails: set[str] = field(default_factory=set)
    # Capa 2 metrics
    n_chain_walk_calls: int = 0
    n_chain_walk_flights_upserted: int = 0
    n_chain_walk_actuals_upserted: int = 0
    cache_hits: int = 0
    cache_misses_hydrated: int = 0
    cache_misses_failed: int = 0
    cache_skipped: int = 0


def harvest_atl_anchor(
    conn,
    *,
    airport_code: str = config.AIRPORT_CODE,
    max_pages: int = 7,
    flight_limit: int = 100,
    dry_run: bool = False,
    client: FR24Client | None = None,
) -> HarvestStats:
    """Capa 1 — pull paginado de arrivals + departures del anchor + UPSERT."""
    stats = HarvestStats()
    started = time.monotonic()
    if client is None:
        client = FR24Client()

    try:
        for page in client.iter_airport_flights(
            airport_code, max_pages=max_pages, flight_limit=flight_limit
        ):
            stats.n_calls += 1
            stats.pages_visited.append(page.page)
            log.info(
                "page=%d arrivals=%d departures=%d has_more=%s",
                page.page, len(page.arrivals), len(page.departures), page.has_more,
            )
            f_rows, a_rows = _normalize_page(page, anchor_airport=airport_code)

            if dry_run:
                log.info("DRY-RUN: page=%d would upsert %d flights / %d actuals",
                         page.page, len(f_rows), len(a_rows))
            else:
                stats.n_flights_upserted += db.upsert_flights(conn, f_rows)
                stats.n_actuals_upserted += db.upsert_actuals(conn, a_rows)

            for r in f_rows:
                if r.get("tail_num"):
                    stats.unique_tails.add(r["tail_num"])
    except FR24RateLimitedError as exc:
        stats.status = "partial" if stats.n_calls > 0 else "failed"
        stats.error = f"FR24 rate-limited: {exc}"
        log.error(stats.error)
    except FR24Error as exc:
        stats.status = "failed"
        stats.error = f"FR24 error: {exc}"
        log.exception("FR24 error")
    except Exception as exc:  # noqa: BLE001 — proteger el job de romper Cloud Run
        stats.status = "failed"
        stats.error = f"{type(exc).__name__}: {exc}"
        log.exception("unexpected error")

    stats.duration_seconds = time.monotonic() - started
    return stats


def harvest_chain_walk(
    conn,
    client: FR24Client,
    candidate_tails: set[str],
    *,
    budget: int = config.LINEAGE_HYDRATION_BUDGET,
    freshness_hours: int = config.LINEAGE_FRESHNESS_HOURS,
    dry_run: bool = False,
) -> HarvestStats:
    """Capa 2 — chain walk lazy. Itera tails sin cache fresco y los hidrata."""
    stats = HarvestStats(layer="chain_walk")
    started = time.monotonic()

    to_hydrate = select_tails_to_hydrate(
        conn, candidate_tails, budget=budget, freshness_hours=freshness_hours
    )
    stats.cache_hits = len(candidate_tails) - len(to_hydrate)

    log.info(
        "chain_walk: candidates=%d to_hydrate=%d (cache_hits=%d, budget=%d, freshness_h=%d)",
        len(candidate_tails), len(to_hydrate), stats.cache_hits, budget, freshness_hours,
    )

    if dry_run:
        stats.duration_seconds = time.monotonic() - started
        return stats

    for tail in to_hydrate:
        try:
            status, n_f, n_a = maybe_hydrate_tail(
                conn, client, tail, freshness_hours=freshness_hours
            )
        except Exception as exc:  # noqa: BLE001 — proteger el loop del job
            log.exception("chain_walk(%s) crashed: %s", tail, exc)
            stats.cache_misses_failed += 1
            continue
        stats.n_chain_walk_calls += 1
        if status == HydrationStatus.HYDRATED:
            stats.cache_misses_hydrated += 1
            stats.n_chain_walk_flights_upserted += n_f
            stats.n_chain_walk_actuals_upserted += n_a
        elif status == HydrationStatus.EMPTY:
            stats.cache_misses_hydrated += 1  # cuenta como "no falló"
        elif status in (HydrationStatus.SKIPPED_FAILURES,):
            stats.cache_skipped += 1
        elif status in (HydrationStatus.RATE_LIMITED, HydrationStatus.FAILED):
            stats.cache_misses_failed += 1

    stats.n_tails_hydrated = stats.cache_misses_hydrated
    stats.duration_seconds = time.monotonic() - started

    if stats.cache_misses_failed > 0 and stats.cache_misses_hydrated == 0 and stats.n_chain_walk_calls > 0:
        stats.status = "failed"
    elif stats.cache_misses_failed > stats.cache_misses_hydrated:
        stats.status = "partial"
    return stats


def _normalize_page(page: FR24Page, *, anchor_airport: str) -> tuple[list[dict], list[dict]]:
    """Devuelve (rows_flights, rows_actuals)."""
    flights_rows: list[dict] = []
    actuals_rows: list[dict] = []

    for flight in page.arrivals:
        f = normalize_flight(flight, anchor_airport=anchor_airport, is_arrival_side=True)
        if f:
            flights_rows.append(f)
        a = normalize_actual(flight)
        if a:
            actuals_rows.append(a)

    for flight in page.departures:
        f = normalize_flight(flight, anchor_airport=anchor_airport, is_arrival_side=False)
        if f:
            flights_rows.append(f)
        a = normalize_actual(flight)
        if a:
            actuals_rows.append(a)

    return flights_rows, actuals_rows


# -- CLI -----------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--airport", default=config.AIRPORT_CODE)
    p.add_argument("--max-pages", type=int, default=7,
                   help="Max páginas a iterar; ~4-7 cubren ventana ±6h en ATL")
    p.add_argument("--flight-limit", type=int, default=100)
    p.add_argument("--local-db", default=None,
                   help="Path local — saltea download/upload GCS")
    p.add_argument("--dry-run", action="store_true",
                   help="No escribe en DB, no sube a GCS — solo logea")
    p.add_argument("--skip-upload", action="store_true",
                   help="Escribe en local pero no sube a GCS")
    p.add_argument("--skip-chain-walk", action="store_true",
                   help="Skipea Capa 2 (chain walk lazy)")
    p.add_argument("--lineage-budget", type=int, default=config.LINEAGE_HYDRATION_BUDGET,
                   help="Máx. tails a hidratar por tick (Capa 2)")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    if args.local_db:
        local_path = Path(args.local_db)
        if not local_path.parent.exists():
            local_path.parent.mkdir(parents=True, exist_ok=True)
        if not local_path.exists():
            with db.open_db(local_path):
                pass  # crea schema en archivo vacío
        log.info("using local DB: %s", local_path)
    else:
        local_path = db.download_db_from_gcs()

    # Cliente FR24 compartido entre Capa 1 y Capa 2 — reusa la sesión curl_cffi
    # (cookies + TLS impersonation) que pasa Cloudflare en un solo warm-up.
    client = FR24Client()

    stats: HarvestStats
    chain_stats: HarvestStats | None = None
    with db.open_db(local_path) as conn:
        stats = harvest_atl_anchor(
            conn,
            airport_code=args.airport,
            max_pages=args.max_pages,
            flight_limit=args.flight_limit,
            dry_run=args.dry_run,
            client=client,
        )
        stats.n_tails_hydrated = len(stats.unique_tails)
        if not args.dry_run:
            db.record_harvester_run(
                conn,
                layer=stats.layer,
                n_calls=stats.n_calls,
                n_flights_upserted=stats.n_flights_upserted,
                n_actuals_upserted=stats.n_actuals_upserted,
                n_tails_hydrated=stats.n_tails_hydrated,
                duration_seconds=stats.duration_seconds,
                status=stats.status,
                error=stats.error,
            )

        # Capa 2 — chain walk lazy
        if config.LINEAGE_ENABLED and not args.skip_chain_walk and stats.unique_tails:
            chain_stats = harvest_chain_walk(
                conn,
                client,
                stats.unique_tails,
                budget=args.lineage_budget,
                dry_run=args.dry_run,
            )
            if not args.dry_run:
                db.record_harvester_run(
                    conn,
                    layer=chain_stats.layer,
                    n_calls=chain_stats.n_chain_walk_calls,
                    n_flights_upserted=chain_stats.n_chain_walk_flights_upserted,
                    n_actuals_upserted=chain_stats.n_chain_walk_actuals_upserted,
                    n_tails_hydrated=chain_stats.n_tails_hydrated,
                    duration_seconds=chain_stats.duration_seconds,
                    status=chain_stats.status,
                    error=chain_stats.error,
                )

    log.info(
        "DONE capa1=%s pages=%s calls=%d flights=%d actuals=%d tails_seen=%d duration=%.1fs status=%s",
        stats.layer,
        stats.pages_visited,
        stats.n_calls,
        stats.n_flights_upserted,
        stats.n_actuals_upserted,
        stats.n_tails_hydrated,
        stats.duration_seconds,
        stats.status,
    )
    if chain_stats is not None:
        log.info(
            "DONE capa2=%s calls=%d flights=%d actuals=%d hydrated=%d cache_hits=%d failed=%d skipped=%d duration=%.1fs status=%s",
            chain_stats.layer,
            chain_stats.n_chain_walk_calls,
            chain_stats.n_chain_walk_flights_upserted,
            chain_stats.n_chain_walk_actuals_upserted,
            chain_stats.cache_misses_hydrated,
            chain_stats.cache_hits,
            chain_stats.cache_misses_failed,
            chain_stats.cache_skipped,
            chain_stats.duration_seconds,
            chain_stats.status,
        )

    if args.local_db or args.dry_run or args.skip_upload:
        log.info("skip upload (local_db=%s dry_run=%s skip_upload=%s)",
                 bool(args.local_db), args.dry_run, args.skip_upload)
    else:
        db.upload_db_to_gcs(local_path)

    if stats.status == "failed":
        return 2
    if stats.status == "partial":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
