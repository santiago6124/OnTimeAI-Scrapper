"""
El presupuesto de hidratacion tiene que ir al hueco, no a lo ya cubierto.

Medido sobre el archivo en BigQuery: de 2.210 salidas de ATL sin prediccion
entre el 17 y el 20/09, **2.209 las operaba un avion que ya teniamos en la
base**. Solo una era de un avion nunca visto. O sea: los vuelos que perdemos no
son inalcanzables, nunca les consultamos el itinerario a tiempo.

La causa estaba en el orden de prioridad. `predicted_today` —aviones que YA
tienen prediccion hoy— iba antes que todo lo demas, asi que el presupuesto se
gastaba reforzando lo cubierto. Subirlo de 30 a 70 no movio la cobertura
(46,2% -> 49,0%, dentro de una variacion diaria que va de 39% a 51%) porque los
40 lugares nuevos fueron a mas de lo mismo.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from ontimeai_scrapper import db
from ontimeai_scrapper.lineage_cache import (
    salidas_sin_predecir_tail_order,
    select_tails_to_hydrate,
)


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    db.ensure_schema(c)
    c.execute(
        """CREATE TABLE predictions(
             fa_flight_id TEXT, predicted_at_utc TEXT, proba_delay REAL,
             PRIMARY KEY(fa_flight_id, predicted_at_utc))"""
    )
    return c


def _salida(conn, *, fid, tail, en_horas, con_prediccion, origen="ATL"):
    """Una salida del anchor dentro de `en_horas`, con o sin prediccion."""
    ahora = datetime.now(timezone.utc)
    sale = ahora + timedelta(hours=en_horas)
    conn.execute(
        """INSERT INTO flights(
             fa_flight_id, stable_id, ident_iata, op_carrier, flight_number,
             tail_num, origin, dest, fl_date, scheduled_out_utc, scheduled_in_utc,
             first_seen_utc, last_updated_utc, cancelled)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
        (fid, fid, "DL1", "DL", "1", tail, origen, "MIA",
         sale.strftime("%Y-%m-%d"), sale.strftime("%Y-%m-%dT%H:%M:%S"),
         (sale + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S"),
         ahora.isoformat(), ahora.isoformat()),
    )
    if con_prediccion:
        conn.execute("INSERT INTO predictions VALUES (?,?,?)",
                     (fid, ahora.isoformat(), 0.3))
    conn.commit()


class TestQuienEntraEnLaCola:
    def test_una_salida_proxima_sin_predecir_entra(self, conn) -> None:
        _salida(conn, fid="F1", tail="N111AA", en_horas=2, con_prediccion=False)
        assert salidas_sin_predecir_tail_order(conn) == ["N111AA"]

    def test_una_salida_que_ya_tiene_prediccion_no_entra(self, conn) -> None:
        _salida(conn, fid="F1", tail="N111AA", en_horas=2, con_prediccion=True)
        assert salidas_sin_predecir_tail_order(conn) == []

    def test_una_salida_que_no_es_del_anchor_no_entra(self, conn) -> None:
        _salida(conn, fid="F1", tail="N111AA", en_horas=2,
                con_prediccion=False, origen="MIA")
        assert salidas_sin_predecir_tail_order(conn) == []

    def test_una_salida_pasada_no_entra(self, conn) -> None:
        """Ya despego: consultarle el itinerario no sirve para predecirla."""
        _salida(conn, fid="F1", tail="N111AA", en_horas=-2, con_prediccion=False)
        assert salidas_sin_predecir_tail_order(conn) == []

    def test_una_salida_lejana_no_entra(self, conn) -> None:
        _salida(conn, fid="F1", tail="N111AA", en_horas=20, con_prediccion=False)
        assert salidas_sin_predecir_tail_order(conn, horizonte_horas=8) == []

    def test_la_mas_inminente_va_primero(self, conn) -> None:
        """Si el presupuesto no alcanza, que se gaste en las que estan por irse."""
        _salida(conn, fid="F1", tail="N999ZZ", en_horas=1, con_prediccion=False)
        _salida(conn, fid="F2", tail="N111AA", en_horas=6, con_prediccion=False)
        # Alfabeticamente seria N111AA primero; por urgencia va N999ZZ.
        assert salidas_sin_predecir_tail_order(conn) == ["N999ZZ", "N111AA"]


class TestElPresupuestoVaAlHueco:
    def test_el_hueco_le_gana_a_lo_ya_cubierto(self, conn) -> None:
        """
        El circulo vicioso que este cambio rompe: con presupuesto para uno solo,
        antes se lo llevaba el avion que YA tenia prediccion.
        """
        _salida(conn, fid="CUBIERTO", tail="N111AA", en_horas=3, con_prediccion=True)
        _salida(conn, fid="HUECO", tail="N999ZZ", en_horas=3, con_prediccion=False)

        elegidos = select_tails_to_hydrate(conn, {"N111AA", "N999ZZ"}, budget=1)
        assert elegidos == ["N999ZZ"], (
            "el presupuesto tiene que ir al vuelo sin predecir, no a reforzar "
            "el que ya esta cubierto"
        )

    def test_con_presupuesto_para_los_dos_entran_los_dos(self, conn) -> None:
        _salida(conn, fid="CUBIERTO", tail="N111AA", en_horas=3, con_prediccion=True)
        _salida(conn, fid="HUECO", tail="N999ZZ", en_horas=3, con_prediccion=False)

        elegidos = select_tails_to_hydrate(conn, {"N111AA", "N999ZZ"}, budget=5)
        assert set(elegidos) == {"N111AA", "N999ZZ"}
        assert elegidos[0] == "N999ZZ", "el hueco sigue yendo primero"

    def test_no_repite_una_matricula(self, conn) -> None:
        """Un avion con dos salidas sin predecir es una sola consulta."""
        _salida(conn, fid="F1", tail="N111AA", en_horas=2, con_prediccion=False)
        _salida(conn, fid="F2", tail="N111AA", en_horas=5, con_prediccion=False)
        assert salidas_sin_predecir_tail_order(conn) == ["N111AA"]
        assert select_tails_to_hydrate(conn, {"N111AA"}, budget=10) == ["N111AA"]
