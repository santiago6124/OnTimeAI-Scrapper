# Harvester — Plan de mejoras y aplicadas

**Fecha**: 2026-05-25
**Contexto**: el harvester corre estable (Fase 1+2 deployed desde 2026-05-22, 92.3%
inbound coverage, ~$0.06/día). Esta sesión agrega data sources nuevas para que
el modelo backend tenga señales que hoy no existen y mejora la calidad del
cache de lineage.

**Constraint**: todo gratis. Verificado:
- OpenSky anonymous: 400 req/día (usamos ~96 = 1/tick) ✅
- IEM (TAFs futuras): sin auth, sin rate-limit declarado ✅
- FAA MASTER (futuras mejoras): public domain ✅

---

## Lo que YA estaba bien (no requiere fix)

### `actual_off` capture en Capa 1
Originalmente sospeché que el harvester FR24 no capturaba `actual_off`
para flights en aire (despegaron pero no aterrizaron). Tras leer
`fr24_client.py:296-339` confirmo que `normalize_actual` **sí captura**:
el gate (`if not actual_off_epoch and not actual_in_epoch: return None`)
solo rechaza si NINGUNA actual está set. Vuelos en aire con
`real.departure` populado entran a `actuals` con `actual_off_utc` y sin
`actual_in_utc`.

→ Esto se complementa perfectamente con el backend Tier 3 #1
(intermediate_dep_delay_adjust), que aprovecha estas filas para boost de
proba.

---

## Aplicado esta sesión

### Mejora #5 — TTL adaptativo por tail frequency
**Archivo**: `ontimeai_scrapper/lineage_cache.py`

Antes: TTL fijo 6h para todos los tails.

Ahora: `_adaptive_freshness_hours(conn, tail, default_hours)`:
- Tails con ≥5 legs hoy → TTL **2h** (high-frequency airlines como F9/DL en hubs)
- Tails con 2-4 legs → TTL **6h** (default)
- Tails con ≤1 leg → TTL **24h** (regionales, charters)

`select_tails_to_hydrate` también prioriza: `high_freq > never > expired`.
Los tails activos vuelven a refrescarse rápido; los ocasionales no
queman budget AeroAPI/FR24.

Impacto esperado: `lineage_hit_rate` más alto donde importa (los tails
con muchos legs son los que más predicciones target).

### Mejora #1 — OpenSky ADS-B real-time bbox
**Archivos**: `ontimeai_scrapper/opensky_client.py` (nuevo),
`ontimeai_scrapper/db.py` (schema + helper), `ontimeai_scrapper/harvester.py`

Captura de posiciones de aircraft en una bbox alrededor de KATL
(28.6°..38.6° N, -89.5°..-79.5° W, aprox ±300nm). Tabla nueva:

```sql
CREATE TABLE aircraft_position (
    icao24 TEXT NOT NULL,
    captured_at_utc TEXT NOT NULL,
    callsign TEXT,
    lat REAL, lon REAL,
    baro_altitude_m REAL, geo_altitude_m REAL,
    velocity_mps REAL,
    true_track_deg REAL,
    vertical_rate_mps REAL,
    on_ground INTEGER,
    origin_country TEXT,
    PRIMARY KEY (icao24, captured_at_utc)
);
```

Endpoint: `https://opensky-network.org/api/states/all?lamin=..&...`
con bbox de ATL ±5°. Anonymous (gratis), bbox query = 1 call por tick.

Backend Tier 3 #3 (TODO): podrá usar esta tabla para calcular ETAs:

```python
time_to_atl_min = great_circle_distance(lat, lon, ATL_lat, ATL_lon) \
                  / velocity_mps / 60
# Si time_to_atl significativamente > minutos_hasta_scheduled_in → boost delay
```

Toggle: `OPENSKY_ENABLED=1` env (default ON; set 0 to disable).

Throttle interno: `_OPENSKY_THROTTLE_SECONDS=12`, `_OPENSKY_MAX_CALLS_PER_TICK=3`.
Failure mode: silent degradation — si OpenSky timea out o rate-limita,
retorna lista vacía, harvester sigue normal.

---

## Aplicado 2026-06-01 — lead time de predicción

**Contexto**: el backend predecía cada vuelo ~1 vez, mediana ~4 min antes del pushback,
porque el descubrimiento (airport boards de AeroAPI **y** FR24) sólo expone vuelos ~75 min
antes de salir. Dos fixes en el scrapper para destrabar lead time real. Detalle backend:
`OnTimeAI-Backend/PREDICTION_LEAD_TIME_FIX.md`.

### Fix #2 — capturar tiempos `estimated` de FR24
**Archivos**: `fr24_client.py`, `db.py`.

`normalize_flight` descartaba el bloque `time.estimated` de FR24. Ahora lo lee →
`estimated_out_utc` / `estimated_in_utc`, columnas nuevas en `flights` (con
`_migrate_estimated_times`, no-op en la DB de prod que ya las tiene por el backend) y
cableadas en `upsert_flights`. Alimenta el predicado delay-aware del backend
`COALESCE(estimated_out_utc, scheduled_out_utc)`.

### Fix #1 — capturar future-legs del chain-walk (detrás de flag)
**Archivos**: `fr24_client.py`, `fr24_http.py`, `lineage_cache.py`, `db.py`, `harvester.py`,
`config.py`. Diseño completo: **`FUTURE_LEG_CAPTURE_DESIGN.md`**.

El chain-walk (`flight/list.json`) ya trae el itinerario **futuro** de cada tail, pero se
descartaba porque FR24 deja `id==null` hasta ~1h antes. Ahora, con
`CAPTURE_FUTURE_LEGS=true`, esas legs ATL futuras se retienen con un id sintético
determinístico `SYN-{carrier}{num}-{origin}-{dest}-{fl_date}`; `reconcile_synthetic_flights`
las colapsa contra el row con id real cuando aparece en el board (repunta predicciones/actuals
al id real, TTL-purga las que nunca matchean). Corre cada tick **aunque el flag esté off**.

**Dry-run validado (real FR24)**: 30 tails → 31 future-legs ATL, **lead mediana 521 min
(~8.7h)**, 100% >4h. Gate (≥120 min) **PASS**. Herramienta: `scripts/analyze_future_legs.py`
(no escribe nada). Tests: `tests/test_future_legs.py`.

> ⚠️ **Activar junto con el backend**: `CAPTURE_FUTURE_LEGS=true` **y**
> `PREDICT_HORIZON_HOURS=12` en el job del backend. Con 6h sólo se predice ~1 leg por lote
> (las demás caen a 6–12h). Ver `FUTURE_LEG_CAPTURE_DESIGN.md` §9.

---

## Pendientes (priorizado por ROI)

### Tier H1 — Datos nuevos para el modelo
| # | Item | Esfuerzo | Por qué |
|---|---|---|---|
| #2 | TAF forecasts (IEM) | 3-4h | Backend usa METAR observed; TAF mira al futuro, mejor para flights con sched_in en +2-6h |
| #3 | Resolver ICAO24 → N-number (FAA MASTER) | 4-6h | Hoy aircraft_position guarda icao24 pero no tail_num; matching contra flights es por callsign solamente |
| — | NAS state redundante (también escribir desde harvester) | 1h | Backup si backend está caído; ya lo escribe backend Tier 2 #K |

### Tier H2 — Eficiencia
| # | Item | Esfuerzo |
|---|---|---|
| #7 | Adaptive budget durante quiet ticks | 1h |
| #8 | Failure recovery con exp backoff (vs 5-strike-out hoy) | 2h |
| — | Pre-warm cache en off-peak UTC | 1h |

### Tier H3 — Reliability
| # | Item | Esfuerzo |
|---|---|---|
| #9 | OpenSky fallback completo (cuando FR24 falla) — Fase 3 TODO | 4-6h |
| #10 | Schema versioning (vs migrate-every-open hoy) | 2h |
| #11 | Dedup at source (UPSERT por tail+sched_off) | 4-6h |

### Tier H4 — Coordinación con backend
| # | Item | Esfuerzo | Bloquea |
|---|---|---|---|
| #12 | **Bucket separation** — `live-db` (harvester) + `predictions-db` (backend) | 6-8h | Schedulers 24/7 sin race condition |
| #13 | Schema unificado en repo común | 3-4h | Evita drift entre los dos sides |

### Tier H5 — Observability
| # | Item | Esfuerzo |
|---|---|---|
| #15 | Logs estructurados JSON para Cloud Logging | 2h |
| #16 | Alert si lineage_hit_rate <0.70 por >3 ticks | 3-4h |

---

## Variables de entorno nuevas

| Variable | Default | Uso |
|---|---|---|
| `OPENSKY_ENABLED` | `1` | Toggle de Mejora #1 (set 0 para desactivar) |
| `OPENSKY_USERNAME` | (vacío = anonymous) | Para subir cuota a 4000 req/día (gratis pero requiere registro) |
| `OPENSKY_PASSWORD` | (vacío) | idem |

---

## Validación post-deploy

```sql
-- Mejora #1 — ADS-B capture working
SELECT COUNT(*) total, COUNT(DISTINCT icao24) unique_aircraft,
       MIN(captured_at_utc) min_t, MAX(captured_at_utc) max_t,
       SUM(CASE WHEN on_ground = 1 THEN 1 ELSE 0 END) on_ground_count
FROM aircraft_position
WHERE captured_at_utc >= datetime('now', '-1 day');

-- Mejora #5 — TTL adaptativo: distribución de hydration triggers
SELECT
  CASE
    WHEN (SELECT COUNT(*) FROM flights WHERE tail_num = c.tail AND fl_date = strftime('%Y-%m-%d', 'now')) >= 5
      THEN 'high_freq_2h'
    WHEN (SELECT COUNT(*) FROM flights WHERE tail_num = c.tail AND fl_date = strftime('%Y-%m-%d', 'now')) >= 2
      THEN 'med_freq_6h'
    ELSE 'low_freq_24h'
  END AS bucket,
  COUNT(*) tails
FROM tail_lineage_cache c
GROUP BY 1;

-- Combine ADS-B + flights for "in air arrivals to ATL"
SELECT
  ap.callsign, ap.icao24, ap.velocity_mps, ap.baro_altitude_m,
  f.scheduled_in_utc, f.op_carrier
FROM aircraft_position ap
LEFT JOIN flights f ON UPPER(REPLACE(ap.callsign, ' ', '')) =
                       UPPER(f.op_carrier || f.flight_number)
WHERE ap.captured_at_utc >= datetime('now', '-30 minutes')
  AND ap.on_ground = 0
  AND f.dest = 'ATL'
ORDER BY ap.captured_at_utc DESC LIMIT 20;
```

---

## Commits relacionados

Implementación queda en working tree del repo `OnTimeAI-Scrapper`.
Commitear con: `feat: adaptive TTL + OpenSky ADS-B real-time positions`.
