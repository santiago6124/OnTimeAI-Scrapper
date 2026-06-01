"""Dry-run: measure the departure lead time that future-leg capture would add.

Calls the chain walk (`flight/list.json`) for a sample of recently-seen ATL tails with
``capture_future_legs=True`` and prints the distribution of ``scheduled_out - now`` for the
future ATL legs it WOULD persist. **Writes nothing** — no DB upsert, no GCS upload. Use it
to validate the horizon gain before enabling ``CAPTURE_FUTURE_LEGS`` in production.

Gate to enable the flag: median captured lead >= 120 min (vs the ~75 min airport board).

Usage (from repo root, with the FR24 lib installed):
    python -m scripts.analyze_future_legs --tails 40
    python -m scripts.analyze_future_legs --local-db /tmp/live_data.db --tails 60
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
from datetime import datetime, timezone

from ontimeai_scrapper import config, db
from ontimeai_scrapper.fr24_client import SYNTHETIC_ID_PREFIX, FR24Client, FR24Error
from ontimeai_scrapper.fr24_http import fetch_aircraft_history

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("analyze_future_legs")


def _candidate_tails(conn: sqlite3.Connection, limit: int) -> list[str]:
    """Most-recently-updated tails with an ATL leg in the last day (read-only)."""
    rows = conn.execute(
        """
        SELECT DISTINCT tail_num FROM flights
        WHERE tail_num IS NOT NULL AND tail_num <> ''
          AND (origin = 'ATL' OR dest = 'ATL')
          AND fl_date >= date('now', '-1 day')
        ORDER BY last_updated_utc DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [r[0].strip().upper() for r in rows if r[0]]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--local-db", default=None, help="Path local; si se omite, descarga de GCS")
    p.add_argument("--tails", type=int, default=40, help="Cuántos tails muestrear")
    p.add_argument("--horizon-hours", type=int, default=config.FUTURE_LEG_HORIZON_HOURS)
    args = p.parse_args()

    local = args.local_db or str(db.download_db_from_gcs())

    # Read-only: raw connection, no schema mutation / no commit / no upload.
    conn = sqlite3.connect(local)
    conn.row_factory = sqlite3.Row
    tails = _candidate_tails(conn, args.tails)
    conn.close()
    log.info("sampling %d tails (horizon=%dh)", len(tails), args.horizon_hours)

    client = FR24Client()
    now = datetime.now(timezone.utc)
    leads: list[float] = []  # minutes ahead of now
    n_calls = 0
    for tail in tails:
        try:
            _raw, flights_rows, _act = fetch_aircraft_history(
                client, tail, capture_future_legs=True, future_horizon_hours=args.horizon_hours
            )
        except FR24Error as exc:
            log.warning("tail %s failed: %s", tail, exc)
            continue
        n_calls += 1
        for f in flights_rows:
            if not str(f.get("fa_flight_id", "")).startswith(SYNTHETIC_ID_PREFIX):
                continue
            sout = f.get("scheduled_out_utc")
            if not sout:
                continue
            dep = datetime.fromisoformat(sout)
            if dep.tzinfo is None:
                dep = dep.replace(tzinfo=timezone.utc)
            leads.append((dep - now).total_seconds() / 60.0)

    if not leads:
        log.info("no future ATL legs captured from %d tails — horizon gain inconclusive", n_calls)
        return 0

    leads.sort()
    n = len(leads)
    pc = lambda q: leads[int(q / 100 * (n - 1))]
    median = pc(50)
    print(f"\nFuture ATL legs captured: {n} (from {n_calls} tails, ~{n / max(n_calls, 1):.1f}/tail)")
    print(
        f"Lead time (min ahead of now): min={leads[0]:.0f} p25={pc(25):.0f} "
        f"median={median:.0f} p75={pc(75):.0f} p90={pc(90):.0f} max={leads[-1]:.0f}"
    )
    print(
        f"Legs >2h ahead: {100 * sum(1 for x in leads if x > 120) / n:.0f}%  |  "
        f">4h ahead: {100 * sum(1 for x in leads if x > 240) / n:.0f}%"
    )
    verdict = "PASS - enable CAPTURE_FUTURE_LEGS" if median >= 120 else "BELOW GATE - keep flag off"
    print(f"Airport-board horizon ~75 min. Gate: median >= 120 min -> {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
