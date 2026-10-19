"""Configuración global del scrapper. Valores leídos de env con defaults."""

from __future__ import annotations

import os

AIRPORT_CODE: str = os.getenv("AIRPORT_CODE", "KATL")

FR24_THROTTLE_SECONDS: float = float(os.getenv("FR24_THROTTLE_SECONDS", "1.5"))
FR24_THROTTLE_JITTER_SECONDS: float = float(os.getenv("FR24_THROTTLE_JITTER_SECONDS", "0.5"))
FR24_MAX_RETRIES: int = int(os.getenv("FR24_MAX_RETRIES", "3"))
FR24_TIMEOUT_SECONDS: float = float(os.getenv("FR24_TIMEOUT_SECONDS", "20.0"))

FR24_HISTORY_BASE_URL: str = "https://api.flightradar24.com/common/v1/flight/list.json"
FR24_HISTORY_DEFAULT_LIMIT: int = int(os.getenv("FR24_HISTORY_LIMIT", "50"))
FR24_HISTORY_DAYS_LOOKBACK: int = 7

OPENSKY_BASE_URL: str = "https://opensky-network.org/api"
OPENSKY_USERNAME: str = os.getenv("OPENSKY_USERNAME", "")
OPENSKY_PASSWORD: str = os.getenv("OPENSKY_PASSWORD", "")
OPENSKY_MAX_WINDOW_SECONDS: int = 2 * 24 * 3600  # 2 días, hard limit del endpoint

FAA_RELEASABLE_ZIP_URL: str = "https://registry.faa.gov/database/ReleasableAircraft.zip"

GCP_PROJECT: str = os.getenv("GCP_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT", "")
GCS_BUCKET: str = os.getenv("GCS_BUCKET", "ontimeai-live-db")
GCS_DB_BLOB: str = os.getenv("GCS_DB_BLOB", "live_data.db")
LOCAL_DB_PATH: str = os.getenv("LOCAL_DB_PATH", "/tmp/live_data.db")

# Future-leg capture (Fix #1): keep a tail's not-yet-id'd ATL legs from the chain
# walk so the backend discovers departures hours ahead instead of ~1h. Default OFF
# until the dry-run lead-time histogram validates the horizon gain. See
# FUTURE_LEG_CAPTURE_DESIGN.md.
CAPTURE_FUTURE_LEGS: bool = os.getenv("CAPTURE_FUTURE_LEGS", "false").lower() in ("1", "true", "yes")

# Pedir el horario del anchor por adelantado en vez de solo el tablero en
# vivo. El tablero vivo llega ~1 h adelante; con `plugin-setting[schedule]
# [timestamp]` el mismo endpoint devuelve el horario de maniana. Ver
# ontimeai_scrapper/fr24_horario.py.
HORARIO_FUTURO: bool = os.getenv("HORARIO_FUTURO", "true").lower() in ("1", "true", "yes")
# Marcas de tiempo a consultar, en horas adelante, separadas por coma.
HORARIO_PAGINAS: int = int(os.getenv("HORARIO_PAGINAS", "2"))
HORARIO_HORAS: tuple[float, ...] = tuple(
    float(x) for x in os.getenv("HORARIO_HORAS", "2,4,6,8,10,12").split(",") if x.strip()
)
# Horas hacia adelante que se conservan del itinerario de un avion.
#
# El chain-walk pide `flight/list.json` por matricula y FR24 devuelve el
# itinerario COMPLETO en la misma respuesta: medido el 19/10 sobre cuatro
# matriculas de ATL, entre 12 y 18 piernas futuras que llegan a 50-72 horas.
#
# Con el horizonte en 6 se conservaban 1 o 2 de esas 12-18, o sea el 6-14%:
#
#     N960AT   12 piernas futuras, la mas lejana a 50,7h  ->  1  ( 8%)
#     N135EV   16 piernas,                     a 72,2h   ->  1  ( 6%)
#     N893AT   14 piernas,                     a 50,4h   ->  2  (14%)
#     N915AT   18 piernas,                     a 51,6h   ->  2  (11%)
#
# Subirlo no cuesta ninguna llamada extra: el dato ya viene. Se tiraba.
#
# Esto es lo que limitaba la cobertura de rotaciones, no el presupuesto de
# hidratacion. El reparto de la cola, medido en produccion el 19/10, muestra
# que las clases criticas suman ~27 y entran completas en los 30 que hay:
#
#     bootstrap 0, pendiente_resultado 18, sin_predecir 1, predicho_hoy 8,
#     vencido 3759, necesitan 3786, presupuesto 30, alcanzados 30
#
# Con 3.786 vencidos y 30 por ciclo cada 15 minutos, un avion se rehidrata
# cada ~31 horas. Con horizonte de 6 h la chance de pegarle a la ventana justa
# era ~19%, del mismo orden que el 33% de rotaciones conocidas a tiempo.
#
# Va en 24 y no en 72 para acotar el crecimiento de la base, que fue el costo
# que hundio el intento de subir el presupuesto a 70 (de 14 a 30 MB/dia).
FUTURE_LEG_HORIZON_HOURS: int = int(os.getenv("FUTURE_LEG_HORIZON_HOURS", "24"))

LINEAGE_FRESHNESS_HOURS: int = int(os.getenv("LINEAGE_FRESHNESS_HOURS", "6"))
LINEAGE_MAX_CONSECUTIVE_FAILURES: int = int(os.getenv("LINEAGE_MAX_CONSECUTIVE_FAILURES", "5"))
LINEAGE_HYDRATION_BUDGET: int = int(os.getenv("LINEAGE_HYDRATION_BUDGET", "30"))
LINEAGE_ENABLED: bool = os.getenv("LINEAGE_ENABLED", "true").lower() in ("1", "true", "yes")

# Flights that have predictions but still lack a completed outcome need a tighter
# refresh loop than the general tail-lineage cache.  This is especially important
# for long-haul departures: by the time they land they have fallen outside the
# backend's rolling AeroAPI departure window, and FR24 may assign a new id when it
# closes the flight.
PENDING_OUTCOME_GRACE_MINUTES: int = int(os.getenv("PENDING_OUTCOME_GRACE_MINUTES", "30"))
PENDING_OUTCOME_REFRESH_MINUTES: int = int(os.getenv("PENDING_OUTCOME_REFRESH_MINUTES", "60"))
PENDING_OUTCOME_LOOKBACK_HOURS: int = int(os.getenv("PENDING_OUTCOME_LOOKBACK_HOURS", "36"))

USER_AGENTS: tuple[str, ...] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
)

LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()
