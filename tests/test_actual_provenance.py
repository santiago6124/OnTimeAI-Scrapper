from __future__ import annotations

import sqlite3

from ontimeai_scrapper.db import ensure_schema, upsert_actuals


def test_actual_upsert_records_provider_and_migrates_legacy_table() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE actuals (
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
        )"""
    )

    ensure_schema(conn)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(actuals)")}
    assert "source_provider" in columns

    assert upsert_actuals(
        conn,
        [
            {
                "fa_flight_id": "fr24-flight",
                "stable_id": "fr24-flight",
                "actual_off_utc": "2026-08-11T12:00:00Z",
                "actual_in_utc": "2026-08-11T14:00:00Z",
                "arr_delay_min": 22.0,
                "cancelled": 0,
                "diverted": 0,
            }
        ],
    ) == 1

    assert conn.execute(
        "SELECT source_provider FROM actuals WHERE fa_flight_id='fr24-flight'"
    ).fetchone() == ("fr24",)
    conn.close()
