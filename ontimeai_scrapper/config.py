"""Configuración global del scrapper. Valores leídos de env con defaults."""

from __future__ import annotations

import os

AIRPORT_CODE: str = os.getenv("AIRPORT_CODE", "KATL")

FR24_THROTTLE_SECONDS: float = float(os.getenv("FR24_THROTTLE_SECONDS", "1.5"))
FR24_THROTTLE_JITTER_SECONDS: float = float(os.getenv("FR24_THROTTLE_JITTER_SECONDS", "0.5"))
FR24_MAX_RETRIES: int = int(os.getenv("FR24_MAX_RETRIES", "3"))
FR24_TIMEOUT_SECONDS: float = float(os.getenv("FR24_TIMEOUT_SECONDS", "20.0"))

FR24_HISTORY_BASE_URL: str = "https://api.flightradar24.com/common/v1/flight/list.json"
FR24_HISTORY_DEFAULT_LIMIT: int = 25
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

LINEAGE_FRESHNESS_HOURS: int = int(os.getenv("LINEAGE_FRESHNESS_HOURS", "6"))
LINEAGE_MAX_CONSECUTIVE_FAILURES: int = int(os.getenv("LINEAGE_MAX_CONSECUTIVE_FAILURES", "5"))
LINEAGE_HYDRATION_BUDGET: int = int(os.getenv("LINEAGE_HYDRATION_BUDGET", "30"))
LINEAGE_ENABLED: bool = os.getenv("LINEAGE_ENABLED", "true").lower() in ("1", "true", "yes")

USER_AGENTS: tuple[str, ...] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
)

LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()
