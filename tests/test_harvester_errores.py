"""harvester.harvest_atl_anchor: cómo degrada ante errores de FR24 (CP-COB-SCR-03).

El job corre en Cloud Run y no puede morir por una página fallida: el ciclo
marca el estado (`partial` si ya trajo algo, `failed` si no) y sigue. Se prueba
cada rama con un cliente FR24 falso y `_normalize_page` reemplazada.
"""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from ontimeai_scrapper import db, harvester
from ontimeai_scrapper.fr24_client import FR24Error, FR24RateLimitedError


def _pagina(n: int):
    return SimpleNamespace(page=n, arrivals=[], departures=[], has_more=False)


class ClienteFalso:
    def __init__(self, paginas_antes_de_fallar: int, error: Exception | None):
        self.n, self.error = paginas_antes_de_fallar, error

    def iter_airport_flights(self, airport, *, max_pages, flight_limit):
        for i in range(1, self.n + 1):
            yield _pagina(i)
        if self.error is not None:
            raise self.error


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    db.ensure_schema(c)
    return c


@pytest.fixture(autouse=True)
def pagina_normalizada(monkeypatch):
    monkeypatch.setattr(harvester, "_normalize_page",
                        lambda page, anchor_airport: ([{"tail_num": f"N{page.page}"}], []))
    monkeypatch.setattr(db, "upsert_flights", lambda conn, rows: len(rows))
    monkeypatch.setattr(db, "upsert_actuals", lambda conn, rows: len(rows))


def test_rate_limit_tras_una_pagina_deja_el_ciclo_parcial(conn):
    stats = harvester.harvest_atl_anchor(conn, client=ClienteFalso(1, FR24RateLimitedError("429")))
    assert stats.status == "partial" and "rate-limited" in stats.error
    assert stats.n_calls == 1 and stats.unique_tails == {"N1"}


def test_rate_limit_sin_paginas_es_fallo(conn):
    stats = harvester.harvest_atl_anchor(conn, client=ClienteFalso(0, FR24RateLimitedError("429")))
    assert stats.status == "failed" and stats.n_calls == 0


def test_error_de_fr24_es_fallo_con_mensaje(conn):
    stats = harvester.harvest_atl_anchor(conn, client=ClienteFalso(2, FR24Error("html inesperado")))
    assert stats.status == "failed" and stats.error == "FR24 error: html inesperado"
    assert stats.n_flights_upserted == 2


def test_un_error_inesperado_no_tira_el_job(conn):
    stats = harvester.harvest_atl_anchor(conn, client=ClienteFalso(1, KeyError("campo")))
    assert stats.status == "failed" and stats.error.startswith("KeyError")
    assert stats.duration_seconds >= 0


def test_dry_run_cuenta_matriculas_pero_no_escribe(conn):
    stats = harvester.harvest_atl_anchor(conn, client=ClienteFalso(3, None), dry_run=True)
    assert stats.n_flights_upserted == 0 and stats.n_actuals_upserted == 0
    assert stats.unique_tails == {"N1", "N2", "N3"} and stats.pages_visited == [1, 2, 3]
