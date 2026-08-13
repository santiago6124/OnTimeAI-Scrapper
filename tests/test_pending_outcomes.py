from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from ontimeai_scrapper import db
from ontimeai_scrapper.harvester import expand_candidate_tails
from ontimeai_scrapper.lineage_cache import (
    pending_outcome_tail_order,
    pending_outcome_tails,
    select_tails_to_hydrate,
)


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.ensure_schema(conn)
    conn.execute(
        """CREATE TABLE predictions(
             fa_flight_id TEXT, predicted_at_utc TEXT, proba_delay REAL,
             PRIMARY KEY(fa_flight_id, predicted_at_utc))"""
    )
    return conn


def _insert_pending(conn: sqlite3.Connection, *, flight_id: str, tail: str) -> None:
    now = datetime.now(timezone.utc)
    scheduled_out = now - timedelta(hours=12)
    scheduled_in = now - timedelta(hours=2)
    conn.execute(
        """INSERT INTO flights(
             fa_flight_id, stable_id, ident_iata, op_carrier, flight_number,
             tail_num, origin, dest, fl_date, scheduled_out_utc, scheduled_in_utc,
             first_seen_utc, last_updated_utc, cancelled)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
        (
            flight_id, flight_id, "DL105", "DL", "105", tail, "ATL", "GRU",
            scheduled_out.strftime("%Y-%m-%d"), scheduled_out.isoformat(),
            scheduled_in.isoformat(), now.isoformat(), now.isoformat(),
        ),
    )
    conn.execute(
        "INSERT INTO predictions VALUES (?,?,?)",
        (flight_id, (scheduled_out - timedelta(hours=1)).isoformat(), 0.8),
    )


def test_pending_outcome_tail_is_added_outside_airport_board() -> None:
    conn = _connection()
    _insert_pending(conn, flight_id="41205045", tail="N828NW")

    assert pending_outcome_tails(conn) == {"N828NW"}
    assert expand_candidate_tails(conn, set()) >= {"N828NW"}
    conn.close()


def test_pending_outcome_bypasses_24h_tail_ttl_but_respects_retry_interval() -> None:
    conn = _connection()
    _insert_pending(conn, flight_id="41205045", tail="N828NW")
    old_refresh = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    conn.execute(
        "INSERT INTO tail_lineage_cache VALUES (?,?,?,?,?)",
        ("N828NW", old_refresh, "fr24", 1, 0),
    )

    assert select_tails_to_hydrate(conn, {"N828NW"}, budget=30) == ["N828NW"]

    conn.execute(
        "UPDATE tail_lineage_cache SET hydrated_until=? WHERE tail='N828NW'",
        (datetime.now(timezone.utc).isoformat(),),
    )
    assert select_tails_to_hydrate(conn, {"N828NW"}, budget=30) == []
    conn.close()


def test_settled_flight_leaves_pending_queue() -> None:
    conn = _connection()
    _insert_pending(conn, flight_id="41205045", tail="N828NW")
    db.upsert_actuals(conn, [{
        "fa_flight_id": "41205045",
        "actual_in_utc": datetime.now(timezone.utc).isoformat(),
        "arr_delay_min": 105.0,
    }])

    assert pending_outcome_tails(conn) == set()
    conn.close()


def test_pending_queue_orders_oldest_expected_arrival_first() -> None:
    conn = _connection()
    _insert_pending(conn, flight_id="41205045", tail="N828NW")
    _insert_pending(conn, flight_id="41205046", tail="N999NW")
    conn.execute(
        "UPDATE flights SET scheduled_in_utc=datetime('now', '-5 hours') "
        "WHERE fa_flight_id='41205046'"
    )

    assert pending_outcome_tail_order(conn)[:2] == ["N999NW", "N828NW"]
    conn.close()


def test_pending_queue_does_not_starve_unrefreshed_tail() -> None:
    conn = _connection()
    _insert_pending(conn, flight_id="41205045", tail="N828NW")
    _insert_pending(conn, flight_id="41205046", tail="N999NW")
    conn.execute(
        "UPDATE flights SET scheduled_in_utc=datetime('now', '-10 hours') "
        "WHERE fa_flight_id='41205045'"
    )
    conn.execute(
        "INSERT INTO tail_lineage_cache VALUES (?,?,?,?,?)",
        ("N828NW", (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(), "fr24", 1, 0),
    )

    # N828NW is older, but N999NW has never received a refresh and must go first.
    assert pending_outcome_tail_order(conn)[:2] == ["N999NW", "N828NW"]
    conn.close()
