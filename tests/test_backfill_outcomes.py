"""ontimeai_scrapper/backfill_outcomes.py: el job dirigido de backfill (CP-COB-SCR-01).

Estaba al 0 % de cobertura. Se prueba la orquestación con dobles de FR24 y de
GCS: normalización y deduplicación de matrículas, verificación de integridad,
los tres modos de `_run_once` (base local, descarga y subida con la misma
generación, descarga sin subida) y los reintentos de `main` ante conflicto de
generación.
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import pytest

from ontimeai_scrapper import backfill_outcomes as bo
from ontimeai_scrapper import db


@pytest.fixture
def dobles(monkeypatch):
    """FR24 y las escrituras reemplazadas por contadores; la reconciliación, fija."""
    llamadas: list[str] = []
    monkeypatch.setattr(bo, "FR24Client", lambda: object())
    monkeypatch.setattr(bo, "fetch_aircraft_history",
                        lambda client, tail: (llamadas.append(tail) or None, [{"id": tail}, {"id": tail + "b"}], [{"id": tail}]))
    monkeypatch.setattr(db, "upsert_flights", lambda conn, rows: len(rows))
    monkeypatch.setattr(db, "upsert_actuals", lambda conn, rows: len(rows))
    monkeypatch.setattr(db, "reconcile_fr24_flight_aliases", lambda conn: {"reconciled": 2, "conflicts": 1})
    return llamadas


def test_backfill_tails_normaliza_deduplica_y_resume(dobles):
    # Arrange: matrículas con espacios, minúsculas, repetidas y vacías
    conn = sqlite3.connect(":memory:")
    # Act
    resultado = bo.backfill_tails(conn, [" n828nw", "N828NW", "", "N135EV "])
    # Assert: dos llamadas (una por matrícula distinta), conteos y reconciliación
    assert dobles == ["N828NW", "N135EV"]
    assert resultado == {"calls": 2, "flights_written": 4, "actuals_written": 2,
                         "reconciled": 2, "conflicts": 1}


def test_backfill_tails_falla_cerrado_si_quick_check_no_es_ok(dobles):
    class ConexionRota:
        def execute(self, sql):
            class R:
                def fetchone(self_inner):
                    return ("database disk image is malformed",)
            return R()

    with pytest.raises(RuntimeError, match="quick_check"):
        bo.backfill_tails(ConexionRota(), ["N828NW"])


def _args(**kw) -> argparse.Namespace:
    base = {"tail": ["N828NW"], "local_db": None, "skip_upload": False}
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.fixture
def gcs(monkeypatch, tmp_path):
    """Descarga y subida reemplazadas; registran lo que reciben."""
    eventos: list[tuple] = []
    descargado = tmp_path / "descargada.db"

    def descargar(destino):
        eventos.append(("download", Path(destino)))
        return descargado, 7

    def subir(path, *, expected_generation):
        eventos.append(("upload", Path(path), expected_generation))
        return expected_generation + 1

    monkeypatch.setattr(db, "download_db_snapshot_from_gcs", descargar)
    monkeypatch.setattr(db, "upload_db_to_gcs", subir)
    monkeypatch.setattr(bo, "backfill_tails", lambda conn, tails: {
        "calls": 1, "flights_written": 0, "actuals_written": 0, "reconciled": 0, "conflicts": 0})
    return eventos


def test_run_once_con_base_local_no_toca_gcs(gcs, tmp_path):
    local = tmp_path / "local.db"
    assert bo._run_once(_args(local_db=str(local))) == 0
    assert gcs == []  # ni descarga ni subida
    assert local.exists()  # open_db creó el esquema en la ruta indicada


def test_run_once_desde_gcs_sube_con_la_generacion_que_descargo(gcs):
    assert bo._run_once(_args()) == 0
    tipos = [e[0] for e in gcs]
    assert tipos == ["download", "upload"]
    assert gcs[1][2] == 7  # la subida exige la generación que se descargó


def test_run_once_con_skip_upload_descarga_pero_no_sube(gcs):
    assert bo._run_once(_args(skip_upload=True)) == 0
    assert [e[0] for e in gcs] == ["download"]


def test_main_reintenta_ante_conflicto_y_termina_bien(monkeypatch):
    intentos = []

    def correr(args):
        intentos.append(1)
        if len(intentos) < 3:
            raise db.GCSGenerationConflict("otro escritor ganó")
        return 0

    monkeypatch.setenv("GCS_GENERATION_RETRIES", "2")
    monkeypatch.setattr(bo, "parse_args", lambda: _args())
    monkeypatch.setattr(bo, "_run_once", correr)
    assert bo.main() == 0
    assert len(intentos) == 3


def test_main_devuelve_3_si_el_conflicto_persiste(monkeypatch):
    monkeypatch.setenv("GCS_GENERATION_RETRIES", "1")
    monkeypatch.setattr(bo, "parse_args", lambda: _args())

    def siempre_conflicto(args):
        raise db.GCSGenerationConflict("otro escritor ganó")

    monkeypatch.setattr(bo, "_run_once", siempre_conflicto)
    assert bo.main() == 3


def test_parse_args_exige_al_menos_una_matricula(monkeypatch):
    monkeypatch.setattr("sys.argv", ["backfill", "--tail", "N828NW", "--tail", "N135EV", "--skip-upload"])
    args = bo.parse_args()
    assert args.tail == ["N828NW", "N135EV"] and args.skip_upload is True and args.local_db is None
