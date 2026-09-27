"""Un avion, un vuelo — aunque FR24 lo liste bajo dos aerolineas.

FR24 lista el mismo servicio una vez bajo el codigo comercial y otra bajo el
que lo opera. Como `_synthetic_id` se arma con `(op_carrier, flight_number,
origin, dest, fl_date)`, eso generaba dos placeholders para un solo avion:

    SYN-AA4607-ATL-LGA-2026-09-27   American
    SYN-YX4607-ATL-LGA-2026-09-27   Republic, que lo opera

Y cuando el vuelo real aparecia bajo el operador, el placeholder con el codigo
comercial no emparejaba —la reconciliacion exigia mismo `op_carrier`— y
sobrevivia hasta que el TTL lo purgaba. Medido el 27/09 sobre una ventana de
20 h: seis vuelos duplicados asi, uno de ellos (AM979 -> MTY) con el id real
ya presente y el placeholder al lado sin colapsar.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ontimeai_scrapper.db import reconcile_synthetic_flights  # noqa: E402

ESQUEMA = """
CREATE TABLE flights (
    fa_flight_id TEXT PRIMARY KEY,
    op_carrier TEXT, flight_number TEXT, origin TEXT, dest TEXT,
    fl_date TEXT, scheduled_out_utc TEXT,
    first_seen_utc TEXT NOT NULL, last_updated_utc TEXT NOT NULL
);
CREATE TABLE predictions (
    fa_flight_id TEXT, predicted_at_utc TEXT,
    PRIMARY KEY (fa_flight_id, predicted_at_utc)
);
CREATE TABLE actuals (fa_flight_id TEXT PRIMARY KEY);
"""

HOY = datetime.now(timezone.utc).date()
MANIANA = HOY + timedelta(days=1)


def _con(filas) -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(ESQUEMA)
    con.executemany(
        """INSERT INTO flights (fa_flight_id, op_carrier, flight_number, origin,
                                dest, fl_date, scheduled_out_utc,
                                first_seen_utc, last_updated_utc)
           VALUES (?,?,?,?,?,?,?,?,?)""", filas)
    con.commit()
    return con


def _ids(con) -> set[str]:
    return {r[0] for r in con.execute("SELECT fa_flight_id FROM flights")}


def test_el_placeholder_comercial_colapsa_contra_el_operador() -> None:
    """El caso AM979: el real llego bajo otro codigo y el placeholder quedaba."""
    con = _con([
        (f"SYN-AA4607-ATL-LGA-{MANIANA}", "AA", "4607", "ATL", "LGA",
         str(MANIANA), f"{MANIANA}T20:55:00", "x", "x"),
        # El real viene bajo Republic, que es quien lo opera.
        ("41dd4906", "YX", "4607", "ATL", "LGA",
         str(MANIANA), f"{MANIANA}T20:55:00", "y", "y"),
    ])

    reconcile_synthetic_flights(con)

    assert _ids(con) == {"41dd4906"}


def test_los_dos_placeholders_colapsan_en_el_mismo_vuelo() -> None:
    """FR24 puede dejar los dos: ninguno debe sobrevivir."""
    con = _con([
        (f"SYN-AA4607-ATL-LGA-{MANIANA}", "AA", "4607", "ATL", "LGA",
         str(MANIANA), f"{MANIANA}T20:55:00", "x", "x"),
        (f"SYN-YX4607-ATL-LGA-{MANIANA}", "YX", "4607", "ATL", "LGA",
         str(MANIANA), f"{MANIANA}T20:55:00", "x", "x"),
        ("41dd4906", "YX", "4607", "ATL", "LGA",
         str(MANIANA), f"{MANIANA}T20:55:00", "y", "y"),
    ])

    reconcile_synthetic_flights(con)

    assert _ids(con) == {"41dd4906"}


def test_las_predicciones_de_los_dos_llegan_al_vuelo_real() -> None:
    con = _con([
        (f"SYN-AM979-ATL-MTY-{MANIANA}", "AM", "979", "ATL", "MTY",
         str(MANIANA), f"{MANIANA}T19:30:00", "x", "x"),
        ("41dd4906", "5D", "979", "ATL", "MTY",
         str(MANIANA), f"{MANIANA}T19:30:00", "y", "y"),
    ])
    con.execute("INSERT INTO predictions VALUES (?,?)",
                (f"SYN-AM979-ATL-MTY-{MANIANA}", f"{MANIANA}T14:00:00"))
    con.commit()

    reconcile_synthetic_flights(con)

    filas = con.execute("SELECT fa_flight_id FROM predictions").fetchall()
    assert [r[0] for r in filas] == ["41dd4906"]


def test_no_fusiona_vuelos_con_horarios_lejanos() -> None:
    """
    La red de seguridad. Sin `op_carrier` en la llave, lo unico que separa dos
    servicios distintos que compartan numero y ruta el mismo dia es el horario.
    """
    con = _con([
        (f"SYN-AA4607-ATL-LGA-{MANIANA}", "AA", "4607", "ATL", "LGA",
         str(MANIANA), f"{MANIANA}T06:00:00", "x", "x"),
        ("41dd4906", "YX", "4607", "ATL", "LGA",
         str(MANIANA), f"{MANIANA}T20:55:00", "y", "y"),
    ])

    reconcile_synthetic_flights(con)

    # Catorce horas de diferencia: no se tocan.
    assert f"SYN-AA4607-ATL-LGA-{MANIANA}" in _ids(con)
    assert "41dd4906" in _ids(con)


def test_sigue_colapsando_el_caso_de_siempre() -> None:
    """Mismo carrier en los dos lados: el comportamiento anterior no se rompe."""
    con = _con([
        (f"SYN-DL1234-ATL-BOS-{MANIANA}", "DL", "1234", "ATL", "BOS",
         str(MANIANA), f"{MANIANA}T14:00:00", "x", "x"),
        ("41cda084", "DL", "1234", "ATL", "BOS",
         str(MANIANA), f"{MANIANA}T14:00:00", "y", "y"),
    ])

    reconcile_synthetic_flights(con)

    assert _ids(con) == {"41cda084"}
