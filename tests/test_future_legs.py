"""Tests for Fix #1 — future-leg capture + synthetic-id reconciliation.

No network: exercises the normalizer's synthetic-id path, the future-ATL-leg filter,
and db.reconcile_synthetic_flights against a temp SQLite DB.
"""

from __future__ import annotations

import sqlite3
import tempfile
import os
from datetime import datetime, timedelta, timezone

import pytest

from ontimeai_scrapper import db
from ontimeai_scrapper.fr24_client import SYNTHETIC_ID_PREFIX, _synthetic_id, normalize_flight
from ontimeai_scrapper.fr24_http import _is_future_atl_leg


def _epoch(dt: datetime) -> int:
    return int(dt.timestamp())


def _fr24_item(fid, *, dep_dt, origin="ATL", dest="JFK", carrier="DL", number="DL456"):
    return {
        "identification": {"id": fid, "number": {"default": number}},
        "aircraft": {"registration": "N123DL", "model": {"code": "B739"}},
        "airline": {"code": {"iata": carrier}},
        "airport": {
            "origin": {"code": {"iata": origin}, "timezone": {"offset": -14400}},
            "destination": {"code": {"iata": dest}},
        },
        "time": {
            "scheduled": {"departure": _epoch(dep_dt), "arrival": _epoch(dep_dt + timedelta(hours=2))},
            "real": {"departure": None, "arrival": None},
            "estimated": {},
        },
        "status": {"text": "scheduled"},
    }


# -- synthetic id --------------------------------------------------------------

def test_synthetic_id_deterministic():
    a = _synthetic_id("DL", "456", "ATL", "JFK", "2026-06-01")
    b = _synthetic_id("DL", "456", "ATL", "JFK", "2026-06-01")
    assert a == b == "SYN-DL456-ATL-JFK-2026-06-01"


def test_synthetic_id_missing_component_returns_none():
    assert _synthetic_id("DL", "456", "ATL", None, "2026-06-01") is None
    assert _synthetic_id(None, "456", "ATL", "JFK", "2026-06-01") is None


# -- normalize_flight synthetic path -------------------------------------------

def test_null_id_dropped_by_default():
    item = _fr24_item(None, dep_dt=datetime.now(timezone.utc) + timedelta(hours=3))
    assert normalize_flight(item) is None


def test_null_id_synthesized_when_allowed():
    item = _fr24_item(None, dep_dt=datetime.now(timezone.utc) + timedelta(hours=3))
    f = normalize_flight(item, allow_synthetic_id=True)
    assert f is not None
    assert f["fa_flight_id"].startswith(SYNTHETIC_ID_PREFIX)
    assert f["fa_flight_id"] == f["stable_id"]
    assert f["origin"] == "ATL" and f["dest"] == "JFK"


def test_real_id_unaffected_by_flag():
    item = _fr24_item("3ffreal1", dep_dt=datetime.now(timezone.utc) + timedelta(hours=3))
    f = normalize_flight(item, allow_synthetic_id=True)
    assert f["fa_flight_id"] == "3ffreal1"


# -- future ATL leg filter -----------------------------------------------------

def test_is_future_atl_leg():
    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    base = {"origin": "ATL", "dest": "JFK", "cancelled": 0}
    future = {**base, "scheduled_out_utc": (now + timedelta(hours=3)).isoformat()}
    past = {**base, "scheduled_out_utc": (now - timedelta(hours=1)).isoformat()}
    nonatl = {"origin": "LAX", "dest": "SFO", "cancelled": 0,
              "scheduled_out_utc": (now + timedelta(hours=3)).isoformat()}
    cancelled = {**future, "cancelled": 1}
    assert _is_future_atl_leg(future, horizon_hours=12, now=now) is True
    assert _is_future_atl_leg(past, horizon_hours=12, now=now) is False
    assert _is_future_atl_leg(nonatl, horizon_hours=12, now=now) is False
    assert _is_future_atl_leg(cancelled, horizon_hours=12, now=now) is False
    # outside horizon
    far = {**base, "scheduled_out_utc": (now + timedelta(hours=20)).isoformat()}
    assert _is_future_atl_leg(far, horizon_hours=12, now=now) is False


# -- reconciliation ------------------------------------------------------------

@pytest.fixture()
def conn():
    tf = tempfile.mktemp(suffix=".db")
    with db.open_db(tf) as c:
        # predictions is backend-owned; create a minimal one for the repoint test.
        c.execute(
            """CREATE TABLE IF NOT EXISTS predictions(
                 fa_flight_id TEXT, predicted_at_utc TEXT, proba_delay REAL,
                 PRIMARY KEY(fa_flight_id, predicted_at_utc))"""
        )
        yield c
    os.remove(tf)


def _insert_flight(c, fid, *, carrier, number, origin, dest, fl_date, sched_out):
    c.execute(
        """INSERT INTO flights (fa_flight_id, stable_id, op_carrier, flight_number,
             origin, dest, fl_date, scheduled_out_utc, first_seen_utc, last_updated_utc)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (fid, fid, carrier, number, origin, dest, fl_date, sched_out, "2026-06-01T00:00:00+00:00",
         "2026-06-01T00:00:00+00:00"),
    )


def test_reconcile_collapses_syn_into_real(conn):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    future = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    syn = f"SYN-DL456-ATL-JFK-{today}"
    real = "3ffreal1"
    _insert_flight(conn, syn, carrier="DL", number="456", origin="ATL", dest="JFK", fl_date=today, sched_out=future)
    _insert_flight(conn, real, carrier="DL", number="456", origin="ATL", dest="JFK", fl_date=today, sched_out=future)
    conn.execute("INSERT INTO predictions VALUES (?,?,?)", (syn, "2026-06-01T01:00:00+00:00", 0.4))
    conn.commit()

    res = db.reconcile_synthetic_flights(conn)
    assert res["reconciled"] == 1
    # SYN flight gone, real remains
    ids = {r[0] for r in conn.execute("SELECT fa_flight_id FROM flights")}
    assert syn not in ids and real in ids
    # prediction repointed to real id
    pred_ids = {r[0] for r in conn.execute("SELECT fa_flight_id FROM predictions")}
    assert pred_ids == {real}


def test_reconcile_ttl_purges_stale_syn(conn):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    stale = (datetime.now(timezone.utc) - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    syn = f"SYN-DL999-ATL-JFK-{today}"
    _insert_flight(conn, syn, carrier="DL", number="999", origin="ATL", dest="JFK", fl_date=today, sched_out=stale)
    conn.commit()
    res = db.reconcile_synthetic_flights(conn, ttl_hours=3)
    assert res["purged"] == 1
    assert conn.execute("SELECT COUNT(*) FROM flights WHERE fa_flight_id=?", (syn,)).fetchone()[0] == 0


def test_reconcile_keeps_unmatched_future_syn(conn):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    future = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    syn = f"SYN-DL777-ATL-JFK-{today}"
    _insert_flight(conn, syn, carrier="DL", number="777", origin="ATL", dest="JFK", fl_date=today, sched_out=future)
    conn.commit()
    res = db.reconcile_synthetic_flights(conn)
    assert res["reconciled"] == 0 and res["purged"] == 0
    assert conn.execute("SELECT COUNT(*) FROM flights WHERE fa_flight_id=?", (syn,)).fetchone()[0] == 1
