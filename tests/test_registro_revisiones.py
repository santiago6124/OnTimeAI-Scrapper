"""Cuando la aerolinea mueve su hora estimada, hay que anotarlo.

`flights.estimated_out_utc` guarda el estado actual y cada ciclo lo
sobreescribe. Sin la serie de cambios no se puede responder cuantos minutos
antes avisa el modelo que la aerolinea, que es el argumento central de la
tesis: falta saber cuando el carrier reconocio la demora. Ver Frontend #5.

El dato no es reconstruible hacia atras, asi que todo dia sin esto es un dia
de datos que no va a existir.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ontimeai_scrapper import db  # noqa: E402


@pytest.fixture
def con() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    db.ensure_schema(c)
    return c


def _vuelo(fid="ABC123", est=None, sched="2026-10-15T12:00:00+00:00"):
    return {"fa_flight_id": fid, "origin": "ATL", "dest": "MIA",
            "scheduled_out_utc": sched, "estimated_out_utc": est}


def _log(con):
    return con.execute(
        "SELECT fa_flight_id, estimated_out_utc FROM flight_estimate_log"
        " ORDER BY observed_at_utc, estimated_out_utc").fetchall()


def test_anota_la_primera_hora_estimada(con) -> None:
    """Es la linea base contra la que se miden las revisiones siguientes."""
    db.upsert_flights(con, [_vuelo(est="2026-10-15T12:20:00+00:00")])
    assert _log(con) == [("ABC123", "2026-10-15T12:20:00+00:00")]


def test_no_anota_si_el_valor_no_cambio(con) -> None:
    """El caso de la enorme mayoria de los ciclos: el harvester reescribe el
    mismo valor cada media hora. Anotarlo seria ~11.600 filas por dia en vez
    de ~100."""
    v = _vuelo(est="2026-10-15T12:20:00+00:00")
    db.upsert_flights(con, [v])
    for _ in range(5):
        db.upsert_flights(con, [dict(v)])
    assert len(_log(con)) == 1


def test_anota_cada_revision_distinta(con) -> None:
    for est in ("2026-10-15T12:10:00+00:00",
                "2026-10-15T12:40:00+00:00",
                "2026-10-15T13:05:00+00:00"):
        db.upsert_flights(con, [_vuelo(est=est)])
    assert [r[1] for r in _log(con)] == [
        "2026-10-15T12:10:00+00:00",
        "2026-10-15T12:40:00+00:00",
        "2026-10-15T13:05:00+00:00",
    ]


def test_un_vuelo_sin_hora_estimada_no_deja_rastro(con) -> None:
    """La mayoria de los vuelos nunca reciben una revision; no deben generar
    filas vacias."""
    db.upsert_flights(con, [_vuelo(est=None)])
    assert _log(con) == []


def test_una_estimada_que_llega_null_despues_no_borra_el_historial(con) -> None:
    """El upsert usa COALESCE, asi que un null entrante conserva el valor
    anterior en `flights`. El registro tiene que comportarse igual y no
    interpretar el null como una revision a nada."""
    db.upsert_flights(con, [_vuelo(est="2026-10-15T12:20:00+00:00")])
    db.upsert_flights(con, [_vuelo(est=None)])
    assert len(_log(con)) == 1
    assert con.execute(
        "SELECT estimated_out_utc FROM flights WHERE fa_flight_id='ABC123'"
    ).fetchone()[0] == "2026-10-15T12:20:00+00:00"


def test_se_anota_antes_de_sobreescribir(con) -> None:
    """Si se anotara despues del upsert, el valor anterior ya estaria perdido
    y la comparacion diria siempre que no hubo cambio."""
    db.upsert_flights(con, [_vuelo(est="2026-10-15T12:10:00+00:00")])
    db.upsert_flights(con, [_vuelo(est="2026-10-15T12:50:00+00:00")])
    assert len(_log(con)) == 2, (
        "la segunda revision no quedo anotada: se esta comparando contra el "
        "valor ya actualizado")


def test_dos_revisiones_en_el_mismo_segundo_quedan_las_dos(con) -> None:
    """Con la clave en `(fa_flight_id, observed_at_utc)` estas dos colisionaban
    y el `INSERT OR IGNORE` descartaba la segunda en silencio. Un vuelo puede
    llegar dos veces en el mismo tick desde capas distintas del harvester."""
    db.upsert_flights(con, [_vuelo(est="2026-10-15T12:10:00+00:00")])
    db.upsert_flights(con, [_vuelo(est="2026-10-15T12:50:00+00:00")])
    sellos = con.execute(
        "SELECT observed_at_utc FROM flight_estimate_log").fetchall()
    assert len(sellos) == 2
    # El escenario solo prueba algo si los sellos de tiempo coincidieron.
    assert sellos[0][0] == sellos[1][0], (
        "las dos escrituras cayeron en segundos distintos; el test no ejercita "
        "la colision que motivo quitar la clave primaria")


def test_una_vuelta_a_una_hora_ya_publicada_cuenta_como_cambio(con) -> None:
    """Deduplicar por valor —clave en `(fa_flight_id, estimated_out_utc)`—
    perderia esto, y es un cambio real: la aerolinea anuncio demora y despues
    la retiro."""
    for est in ("2026-10-15T12:10:00+00:00",
                "2026-10-15T12:50:00+00:00",
                "2026-10-15T12:10:00+00:00"):
        db.upsert_flights(con, [_vuelo(est=est)])
    assert len(_log(con)) == 3


def test_vuelos_distintos_no_se_mezclan(con) -> None:
    db.upsert_flights(con, [_vuelo("AAA", est="2026-10-15T12:10:00+00:00"),
                            _vuelo("BBB", est="2026-10-15T14:10:00+00:00")])
    assert {r[0] for r in _log(con)} == {"AAA", "BBB"}


def test_la_tabla_se_crea_en_una_base_que_ya_existia(con) -> None:
    """`CREATE TABLE IF NOT EXISTS` alcanza para una tabla nueva —no para una
    columna nueva—, asi que la base de produccion la gana al reaplicar el
    schema, sin migracion aparte."""
    con.execute("DROP TABLE flight_estimate_log")
    db.ensure_schema(con)
    db.upsert_flights(con, [_vuelo(est="2026-10-15T12:20:00+00:00")])
    assert len(_log(con)) == 1
