"""Targeted FR24 outcome backfill with guarded GCS publication.

Use this for a small, explicit set of aircraft tails when a completed flight
escaped the normal rolling collectors.  It downloads one immutable DB generation,
refreshes the requested histories, reconciles changed FR24 flight ids, validates
SQLite, and uploads only if the selected generation is still current.
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import tempfile
from pathlib import Path

from . import db
from .fr24_client import FR24Client
from .fr24_http import fetch_aircraft_history

log = logging.getLogger("backfill_outcomes")


def backfill_tails(conn: sqlite3.Connection, tails: list[str]) -> dict[str, int]:
    client = FR24Client()
    flights_written = 0
    actuals_written = 0
    calls = 0
    for tail in dict.fromkeys(tail.strip().upper() for tail in tails if tail.strip()):
        _raw, flight_rows, actual_rows = fetch_aircraft_history(client, tail)
        calls += 1
        flights_written += db.upsert_flights(conn, flight_rows)
        actuals_written += db.upsert_actuals(conn, actual_rows)
    reconciliation = db.reconcile_fr24_flight_aliases(conn)
    check = conn.execute("PRAGMA quick_check").fetchone()
    if check is None or check[0] != "ok":
        raise RuntimeError(f"SQLite quick_check failed: {check[0] if check else 'empty'}")
    conn.commit()
    return {
        "calls": calls,
        "flights_written": flights_written,
        "actuals_written": actuals_written,
        "reconciled": reconciliation["reconciled"],
        "conflicts": reconciliation["conflicts"],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tail", action="append", required=True)
    parser.add_argument("--local-db")
    parser.add_argument("--skip-upload", action="store_true")
    return parser.parse_args()


def _run_once(args: argparse.Namespace) -> int:
    if args.local_db:
        local_path = Path(args.local_db)
        generation = None
    else:
        work_dir = Path(tempfile.mkdtemp(prefix="ontimeai-outcome-backfill-"))
        local_path, generation = db.download_db_snapshot_from_gcs(work_dir / "live_data.db")

    with db.open_db(local_path) as conn:
        result = backfill_tails(conn, args.tail)
    log.info(
        "backfill complete tails=%d calls=%d flights=%d actuals=%d reconciled=%d conflicts=%d",
        len(set(args.tail)), result["calls"], result["flights_written"],
        result["actuals_written"], result["reconciled"], result["conflicts"],
    )

    if not args.local_db and not args.skip_upload:
        if generation is None:
            raise RuntimeError("missing GCS generation")
        db.upload_db_to_gcs(local_path, expected_generation=generation)
    return 0


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    args = parse_args()
    retries = max(0, int(os.getenv("GCS_GENERATION_RETRIES", "2")))
    for attempt in range(retries + 1):
        try:
            return _run_once(args)
        except db.GCSGenerationConflict as exc:
            if attempt >= retries:
                log.error("generation conflict after %d attempts: %s", attempt + 1, exc)
                return 3
            log.warning(
                "generation conflict: %s; retrying from winner (%d/%d)",
                exc, attempt + 2, retries + 1,
            )
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
