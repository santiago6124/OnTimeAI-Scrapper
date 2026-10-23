"""Valores límite de la cola de outcomes pendientes (CP-SCR-01..04).

`pending_outcome_tails` encola la matrícula de un vuelo predicho cuya llegada
esperada venció hace al menos `grace_minutes` (30 por defecto) y no hace más
de `lookback_hours` (36). Dos límites, cada uno con el valor exacto y sus dos
vecinos inmediatos, como pide el análisis de valores límite: el error típico
es un `<` donde iba un `<=`, y sólo el valor exacto lo detecta.

Las llegadas se escriben relativas al reloj del test; SQLite evalúa
`datetime('now')` unos milisegundos después, así que un vuelo "hace 30 min"
queda del lado vencido del límite y uno "hace 29 min" del lado vigente.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from ontimeai_scrapper import db
from ontimeai_scrapper.lineage_cache import pending_outcome_tails

GRACIA_MIN = 30
LOOKBACK_H = 36


def _conexion() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.ensure_schema(conn)
    conn.execute(
        """CREATE TABLE predictions(
             fa_flight_id TEXT, predicted_at_utc TEXT, proba_delay REAL,
             PRIMARY KEY(fa_flight_id, predicted_at_utc))"""
    )
    return conn


def _vuelo_predicho(conn: sqlite3.Connection, *, flight_id: str, tail: str,
                    llegada_hace: timedelta) -> None:
    """Un vuelo con predicción, sin actual, cuya llegada programada fue hace `llegada_hace`."""
    ahora = datetime.now(timezone.utc)
    llegada = ahora - llegada_hace
    salida = llegada - timedelta(hours=2)
    conn.execute(
        """INSERT INTO flights(
             fa_flight_id, stable_id, ident_iata, op_carrier, flight_number,
             tail_num, origin, dest, fl_date, scheduled_out_utc, scheduled_in_utc,
             first_seen_utc, last_updated_utc, cancelled)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
        (flight_id, flight_id, "DL105", "DL", "105", tail, "ATL", "GRU",
         salida.strftime("%Y-%m-%d"), salida.isoformat(), llegada.isoformat(),
         ahora.isoformat(), ahora.isoformat()),
    )
    conn.execute("INSERT INTO predictions VALUES (?,?,?)",
                 (flight_id, (salida - timedelta(hours=1)).isoformat(), 0.8))


@pytest.mark.parametrize("minutos, en_cola", [
    (GRACIA_MIN - 1, False),   # 29 min: todavía en gracia
    (GRACIA_MIN, True),        # 30 min exactos: el límite entra en la cola (<=)
    (GRACIA_MIN + 1, True),    # 31 min: vencido
])
def test_scr_01_limite_de_la_gracia(minutos, en_cola):
    conn = _conexion()
    _vuelo_predicho(conn, flight_id="f1", tail="N828NW", llegada_hace=timedelta(minutes=minutos))
    assert (pending_outcome_tails(conn, grace_minutes=GRACIA_MIN, lookback_hours=LOOKBACK_H) == {"N828NW"}) is en_cola
    conn.close()


@pytest.mark.parametrize("horas, en_cola", [
    (LOOKBACK_H - 1, True),        # 35 h: dentro de la ventana
    (LOOKBACK_H + 1 / 60, False),  # 36 h y 1 min: ya fuera, no se insiste más
])
def test_scr_02_limite_de_la_ventana_de_busqueda(horas, en_cola):
    conn = _conexion()
    _vuelo_predicho(conn, flight_id="f2", tail="N135EV", llegada_hace=timedelta(hours=horas))
    assert (pending_outcome_tails(conn, grace_minutes=GRACIA_MIN, lookback_hours=LOOKBACK_H) == {"N135EV"}) is en_cola
    conn.close()


def test_scr_03_un_vuelo_con_actual_no_entra_aunque_haya_vencido():
    # Clase de equivalencia: el outcome ya existe, no hay nada que cerrar.
    conn = _conexion()
    _vuelo_predicho(conn, flight_id="f3", tail="N893AT", llegada_hace=timedelta(hours=2))
    ahora = datetime.now(timezone.utc).isoformat()
    conn.execute("INSERT INTO actuals(fa_flight_id, actual_in_utc, settled_at_utc) VALUES (?, ?, ?)",
                 ("f3", ahora, ahora))
    assert pending_outcome_tails(conn, grace_minutes=GRACIA_MIN, lookback_hours=LOOKBACK_H) == set()
    conn.close()


def test_scr_04_un_vuelo_cancelado_no_entra():
    conn = _conexion()
    _vuelo_predicho(conn, flight_id="f4", tail="N915AT", llegada_hace=timedelta(hours=2))
    conn.execute("UPDATE flights SET cancelled = 1 WHERE fa_flight_id = 'f4'")
    assert pending_outcome_tails(conn, grace_minutes=GRACIA_MIN, lookback_hours=LOOKBACK_H) == set()
    conn.close()
