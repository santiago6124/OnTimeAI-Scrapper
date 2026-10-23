"""Integración real con Google Cloud Storage: el contrato de compare-and-swap.

Los tests de `test_gcs_generation.py` prueban el mismo contrato con dobles.
Este lo prueba contra el servicio real, en un bucket de QA aparte
(`ontimeai-prod-qa`, con borrado automático al día), con un objeto de nombre
único por corrida. Sólo corre cuando `ONTIMEAI_QA_BUCKET` está definida: en
local con las credenciales de `gcloud auth application-default login`, y en
CI con Workload Identity (`integration.yml`).

Secuencia (CP-INT-01), con el resultado esperado escrito antes de correr:

1. el objeto no existe → la descarga devuelve generación 0 y un archivo vacío;
2. subir con generación esperada 0 → crea el objeto y devuelve una generación > 0;
3. volver a subir con la generación vieja (0) → GCS responde 412 y el cliente
   lo traduce a `GCSGenerationConflict`, sin pisar el objeto;
4. descargar de nuevo → trae la generación actual;
5. subir con esa generación → la acepta y devuelve una generación mayor;
6. el contenido remoto es el de la última subida aceptada, no el de la rechazada.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from ontimeai_scrapper import config, db

BUCKET = os.getenv("ONTIMEAI_QA_BUCKET", "")

pytestmark = pytest.mark.skipif(
    not BUCKET, reason="integración real: definir ONTIMEAI_QA_BUCKET (p. ej. ontimeai-prod-qa)"
)


@pytest.fixture
def objeto_qa(monkeypatch):
    """Apunta el cliente al bucket de QA y a un objeto único; lo borra al final."""
    nombre = f"cas-test/{uuid.uuid4()}.db"
    monkeypatch.setattr(config, "GCS_BUCKET", BUCKET)
    monkeypatch.setattr(config, "GCS_DB_BLOB", nombre)
    yield nombre
    try:
        db._gcs_blob().delete()
    except Exception:  # el lifecycle del bucket lo borra igual al día
        pass


def test_int_01_compare_and_swap_contra_gcs_real(tmp_path: Path, objeto_qa: str) -> None:
    local = tmp_path / "live.db"

    # 1. no existe todavía
    ruta, generacion_inicial = db.download_db_snapshot_from_gcs(local)
    assert ruta == local and generacion_inicial == 0

    # 2. primera subida: crea
    local.write_bytes(b"generacion uno")
    gen_1 = db.upload_db_to_gcs(local, expected_generation=0)
    assert gen_1 > 0

    # 3. un escritor que se quedó con la generación vieja no puede pisar
    stale = tmp_path / "stale.db"
    stale.write_bytes(b"escritor atrasado")
    with pytest.raises(db.GCSGenerationConflict):
        db.upload_db_to_gcs(stale, expected_generation=0)

    # 4. la descarga trae la generación actual
    de_nuevo = tmp_path / "de_nuevo.db"
    _, gen_leida = db.download_db_snapshot_from_gcs(de_nuevo)
    assert gen_leida == gen_1
    assert de_nuevo.read_bytes() == b"generacion uno"

    # 5. con la generación correcta, la subida avanza
    de_nuevo.write_bytes(b"generacion dos")
    gen_2 = db.upload_db_to_gcs(de_nuevo, expected_generation=gen_1)
    assert gen_2 > gen_1

    # 6. el remoto es la última aceptada, no la rechazada
    blob = db._gcs_blob()
    blob.reload()
    assert int(blob.generation) == gen_2
    assert blob.download_as_bytes() == b"generacion dos"
