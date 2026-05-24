# OnTimeAI-Scrapper

Harvester continuo 24/7 de actuals + lineage para alimentar el buffer `live_data.db` de [OnTimeAI-Backend](https://github.com/santiago6124/OnTimeAI-Backend).

**Objetivo**: mantener `lineage_hit_rate ≥ 0.85` todos los días sin depender de AeroAPI ($30-60/mes → ~$2/mes).

---

## Arquitectura (3 capas)

| Capa | Cron | Fuente | Qué hace |
|---|---|---|---|
| **1 — ATL anchor** | `*/15 * * * *` | FR24 `get_airport_details("KATL")` | Pull scheduled + actual + tail de vuelos en ventana ±6h de ATL |
| **2 — Chain walk** | inline en Capa 1 | HTTP directo `flight/list.json?query={REG}` | Para cada tail nuevo visto en Capa 1, traer sus últimos ~7 días |
| **3 — Refresh activo** | `0 */6 * * *` | mismo que Capa 2 | Re-pullear tails con vuelos pendientes en ATL |
| **FAA sync** | `0 3 * * *` | `registry.faa.gov/database/ReleasableAircraft.zip` | Lookup hex→N-number para fallback OpenSky |

Detalle completo: ver `OnTimeAI-Backend/PLAN_HARVESTER_LINEAGE.md`.

---

## Estado actual (2026-05-24)

- [x] **Fase 0 — Validación local** (`scripts/validate_sources.py`) — `FASE_0_HALLAZGOS.md`
- [x] **Fase 1 — Harvester ATL anchor (Capa 1)** — DEPLOYED 2026-05-22, en producción 24/7
- [x] **Fase 2 — Chain walk lazy (Capa 2)** — DEPLOYED 2026-05-22, FR24 inbound coverage 92.3%
- [ ] **Fase 3 — Refresh activo + FAA + fallback OpenSky** — `[TODO]` no urgente
- [~] **Fase 4 — Switch del live_pull.py** — código deployed, BLOCKED por bug LightGBM SIGSEGV preexistente en backend

Documentación completa de hallazgos: `SESSION_HALLAZGOS.md`

### Producción a la fecha

| Métrica | Valor |
|---|---|
| Vuelos FR24 ingeridos | 16,207 |
| Tails en `tail_lineage_cache` | 879 |
| FR24 inbound coverage | 92.3% (criterio ≥0.90 ✅) |
| Failures del harvester en 42h | 0 |
| Costo observado/día | ~$0.06 USD |

---

## Setup local

```bash
python -m venv .venv
.venv\Scripts\activate          # PowerShell
pip install -r requirements.txt
python scripts/validate_sources.py --all
```

## Variables de entorno

| Variable | Default | Uso |
|---|---|---|
| `GCS_BUCKET` | `ontimeai-live-db` | Bucket de `live_data.db` (compartido con backend) |
| `AIRPORT_CODE` | `KATL` | Anchor de Capa 1 |
| `FR24_THROTTLE_SECONDS` | `1.5` | Delay mínimo entre calls a FR24 |
| `OPENSKY_USERNAME` | (vacío = anónimo) | Cuenta OpenSky para fallback (recomendado) |
| `OPENSKY_PASSWORD` | (vacío) | Password OpenSky |
| `LOG_LEVEL` | `INFO` | Nivel de logging |

---

## Riesgo conocido

FR24 endpoints son **TOS-grey**. Pin de versión `FlightRadarAPI==1.5.1` para no romper con auto-upgrades. Fallback OpenSky activo para Capa 2 (sin scheduled times → features parciales). Ver `PLAN_HARVESTER_LINEAGE.md` §6 para detalle.
