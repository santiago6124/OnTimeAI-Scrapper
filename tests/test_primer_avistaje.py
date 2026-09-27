"""Cuando dos ids del mismo vuelo se colapsan, el primer avistaje sobrevive.

FR24 deja `identification.id` en null hasta ~1 h antes de salir, asi que el
horario futuro se captura con un id sintetico `SYN-...`. Cuando el vuelo
aparece con su id real, las dos filas se colapsan: se migran predicciones,
SHAP y actuals, y se borra el placeholder.

Todo lo que APUNTA al vuelo se migraba. `first_seen_utc` no apunta a ningun
lado —vive en la fila— y era lo unico que solo el placeholder sabia, asi que
se perdia. La fila superviviente quedaba declarando que el vuelo aparecio
~1 h antes de salir.

Eso rompia la unica forma de medir la captura por separado de la prediccion:
de 2.703 salidas de ATL con prediccion (24-26/09), 2.694 tenian
`first_seen_utc` posterior a su propia primera prediccion.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ontimeai_scrapper.db import (  # noqa: E402
    reconcile_synthetic_flights,
)


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


# La consulta de emparejamiento filtra por `fl_date >= date('now','-1 day')`,
# asi que una fecha fija en el test deja de emparejar apenas pasa un dia y los
# casos pasan sin ejercitar nada. Todo se ancla al reloj.
HOY = datetime.now(timezone.utc).date()
MANIANA = HOY + timedelta(days=1)


def _t(hhmm: str, dia=None) -> str:
    return f"{dia or HOY}T{hhmm}:00"


def _base(visto_syn: str, visto_real: str) -> sqlite3.Connection:
    """Un vuelo en dos filas: el placeholder temprano y el id real tardio."""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(ESQUEMA)
    con.executemany(
        """INSERT INTO flights (fa_flight_id, op_carrier, flight_number, origin,
                                dest, fl_date, scheduled_out_utc,
                                first_seen_utc, last_updated_utc)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        [
            (f"SYN-DL1234-ATL-BOS-{MANIANA}", "DL", "1234", "ATL", "BOS",
             str(MANIANA), _t("14:00", MANIANA), visto_syn, visto_syn),
            ("41cda084", "DL", "1234", "ATL", "BOS",
             str(MANIANA), _t("14:00", MANIANA), visto_real, visto_real),
        ],
    )
    con.commit()
    return con


def _first_seen(con: sqlite3.Connection, fid: str) -> str | None:
    fila = con.execute(
        "SELECT first_seen_utc FROM flights WHERE fa_flight_id=?", (fid,)
    ).fetchone()
    return fila["first_seen_utc"] if fila else None


def test_la_fila_que_queda_conserva_el_avistaje_temprano() -> None:
    con = _base(visto_syn=_t("06:00"), visto_real=_t("13:05", MANIANA))

    reconcile_synthetic_flights(con)

    assert _first_seen(con, f"SYN-DL1234-ATL-BOS-{MANIANA}") is None
    # Ocho horas antes de salir, no cincuenta y cinco minutos.
    assert _first_seen(con, "41cda084") == _t("06:00")


def test_la_prediccion_nunca_queda_antes_del_avistaje() -> None:
    """
    La incoherencia que destapo el problema: una prediccion no puede ser
    anterior a la existencia de la fila que predice.
    """
    con = _base(visto_syn=_t("06:00"), visto_real=_t("13:05", MANIANA))
    con.execute(
        "INSERT INTO predictions (fa_flight_id, predicted_at_utc) VALUES (?,?)",
        (f"SYN-DL1234-ATL-BOS-{MANIANA}", _t("08:10", MANIANA)),
    )
    con.commit()

    reconcile_synthetic_flights(con)

    fila = con.execute(
        """SELECT f.first_seen_utc visto, MIN(p.predicted_at_utc) prim
             FROM flights f JOIN predictions p ON p.fa_flight_id = f.fa_flight_id
            WHERE f.fa_flight_id = '41cda084'"""
    ).fetchone()
    assert fila["visto"] <= fila["prim"]


def test_no_retrocede_si_el_placeholder_es_mas_nuevo() -> None:
    """Se toma el mas antiguo de los dos, no siempre el del placeholder."""
    con = _base(visto_syn=_t("13:30", MANIANA), visto_real=_t("05:00"))

    reconcile_synthetic_flights(con)

    assert _first_seen(con, "41cda084") == _t("05:00")


def test_compara_por_fecha_y_no_por_texto() -> None:
    """
    Los escritores guardan formatos distintos. Como texto, la `T` (0x54) le
    gana al espacio (0x20) en la posicion 11, asi que

        '2026-09-24 06:00:00'  >  '2026-09-24T13:05:00'   es FALSO como texto
        '2026-09-24T13:05:00'  <  '2026-09-24 06:00:00'   es lo que da MIN()

    y un avistaje de las 06:00 parece posterior a uno de las 13:05. Ordenar
    con `datetime()` lo evita.
    """
    con = _base(visto_syn=f"{HOY} 06:00:00",  # con espacio
                visto_real=_t("13:05", MANIANA))     # con T

    reconcile_synthetic_flights(con)

    assert _first_seen(con, "41cda084") == f"{HOY} 06:00:00"


def test_tolera_offset_y_microsegundos() -> None:
    """El backend escribe con offset y microsegundos; el scrapper no."""
    con = _base(visto_syn=_t("06:00") + ".123456+00:00",
                visto_real=_t("13:05", MANIANA))

    reconcile_synthetic_flights(con)

    assert _first_seen(con, "41cda084") == _t("06:00") + ".123456+00:00"
