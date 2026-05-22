# Fase 0 — Hallazgos de validación empírica

**Fecha:** 2026-05-22
**Script:** `scripts/validate_sources.py`
**IP origen:** residencial (Argentina)

---

## Resumen ejecutivo

| Check | Estado | Detalle |
|---|---|---|
| 1. FR24 lib `get_airport_details("KATL")` | ✅ FUNCIONA | 200 vuelos por call, coverage 88.9% del mapeo |
| 2. FR24 HTTP directo `flight/list.json` | ❌ BLOQUEADO | Cloudflare challenge HTTP 403 desde IP residencial |
| 3. OpenSky `/flights/aircraft` anónimo | ❌ DENEGADO | `403: You cannot access historical flights` (política nueva 2025) |
| 4. FAA MASTER `ReleasableAircraft.zip` | ⏳ NO TESTEADO | Pendiente |
| 5. Mapeo de fields | ✅ DOCUMENTADO | Apéndice A del plan + corrección destination.code |

**Estado del plan:** **viable con ajustes**. Capa 1 está validada. Capa 2 requiere repensar antes de Fase 2.

---

## Hallazgo 1 — Capa 1 funcional pero destination.code.iata ausente en arrivals

### Evidencia

Call: `FlightRadar24API().get_airport_details(code="KATL", flight_limit=100, page=1)`
Latencia: ~2.7 s
Resultado: `arrivals=100 departures=100` (200 vuelos)
Coverage: 8/9 fields (88.9%)

### Detalle del field faltante

En el payload de un vuelo **arrival a KATL** (DL923 BOS→ATL), `flight.airport.destination` no contiene `code.iata`:

```json
"airport": {
  "origin": { "code": { "iata": "BOS", "icao": "KBOS" }, ... },
  "destination": {
    "timezone": {...},
    "info": { "terminal": "S", "baggage": "6", "gate": "C50" }
    // NO key "code"
  }
}
```

Esto es lógico: el endpoint está scoped a KATL → destination es implícitamente ATL en arrivals (y origin implícitamente ATL en departures).

### Impacto en el normalizer

En `harvester.py` cuando ingerimos vuelos de Capa 1:
- Para arrivals al airport ancla → `dest = "ATL"` (hardcoded del anchor)
- Para departures del airport ancla → `origin = "ATL"`

El field opuesto siempre viene completo en `airport.{origin|destination}.code.iata`.

### Acción

Documentar este patrón en el normalizer. No bloquea Fase 1.

---

## Hallazgo 2 — FR24 HTTP directo bloqueado por Cloudflare

### Evidencia

Call: `GET https://api.flightradar24.com/common/v1/flight/list.json?query=N301DQ&fetchBy=reg&limit=25&page=1`
Headers usados: UA browser real + Origin + Referer + Accept
Latencia: 0.11 s
Respuesta: HTTP 403 con HTML "Just a moment..." (Cloudflare interstitial)

### Análisis

Cloudflare bloquea en la primera request desde una IP residencial argentina. Era el riesgo §6.2 del plan. La diferencia con la lib (que sí funciona) es que la lib usa una sesión propia con configuración de browser que aparentemente evade el challenge en endpoints específicos (`airport.json`) pero no en otros (`flight/list.json`).

### Tres caminos de mitigación

1. **Probar desde Cloud Run us-central1** — las IPs de GCP son cleaner para Cloudflare que residenciales no-US. Es la opción del plan §6.2. **No verificable localmente** pero altamente probable que funcione (la mayoría de scrappers de FR24 corren desde DCs).

2. **Usar `cloudscraper` o `curl_cffi`** — librerías que emulan TLS/JA3 fingerprint de Chrome y resuelven el challenge JS. Funciona desde IP residencial pero agrega dependencia frágil que rompe cada vez que Cloudflare actualiza la heurística.

3. **Reusar la sesión interna de la lib FR24** — La lib tiene una clase `APIRequest` con su propio session. Custom-llamar el endpoint usando el mismo session de la lib podría evadir Cloudflare en HTTP directo. Requiere monkey-patching o subclasing.

### Recomendación

**Antes de Fase 2**: deploy un Cloud Run Job de prueba que solo haga el call `flight/list.json` desde us-central1 y mida success rate sobre 100 tails distintos durante 24h. Si success_rate > 95% → seguir con plan original. Si < 50% → priorizar camino 3 (sesión compartida con la lib).

### Decisión inmediata

**Fase 1 (Capa 1) NO está bloqueada** — usa solo la lib. Avanzar.
**Fase 2 (Capa 2)** queda en blocked hasta validar desde Cloud Run.

---

## Hallazgo 3 — OpenSky histórico requiere auth desde día 1

### Evidencia

Call: `GET https://opensky-network.org/api/flights/aircraft?icao24=a06b4f&begin=...&end=...` (sin auth)
Latencia: 0.63 s
Respuesta: `HTTP 403: You cannot access historical flights`

### Análisis

El plan original (§5.1) asumía:
- Anónimo: 400 cred/día (para histórico)
- Auth: 4,000 cred/día

La realidad **a 2026-05**: OpenSky restringió `/flights/aircraft` y `/flights/{arrival,departure}` a **solo usuarios autenticados**. El anónimo ahora solo accede a `/states/all` (tiempo real, no histórico).

Esto matchea con el cambio de política que OpenSky comunicó en 2025 (transición a freemium con tiers).

### Impacto en el plan

La **D2 del plan** ("¿Tener cuenta OpenSky autenticada desde el día 1?") deja de ser una decisión opcional:
- Sin cuenta OpenSky → **no hay fallback** para el chain walk si FR24 cae
- El plan §3 Capa 2B (fallback OpenSky) requiere cuenta antes de Fase 3

### Acción

1. **Inmediato**: registrar cuenta gratuita en https://opensky-network.org/ (el form oficial).
2. **Documentar**: la cuenta debe verificar email + posiblemente esperar aprobación manual (24-48h).
3. **Guardar creds**: en Secret Manager `opensky-username` y `opensky-password`.
4. **Decisión D2 actualizada**: **obligatoria, no opcional**.

### Tier nuevo gratuito (a confirmar tras registro)

Según docs OpenSky 2026: usuarios autenticados free tier reciben:
- 4,000 cred/día
- Ventana max 2 días por call (sin cambios)
- 2-3 segundos entre calls

Esto sigue siendo suficiente para Capa 2 fallback (<100 cred/incidente).

---

## Decisiones revisadas

### D2 (revisada)

**Antes:** "Recomendación: registrar cuenta gratuita desde día 1, guardar credenciales en Secret Manager."
**Ahora:** **Obligatoria.** Sin cuenta, no hay fallback funcional para Capa 2. Si OpenSky no aprueba la cuenta a tiempo, el fallback no existe.

### D6 (nueva)

**¿Cómo resolver el bloqueo Cloudflare para Capa 2A?**

Opción A — Asumir que Cloud Run resuelve. Validar con experimento controlado de 24h tras Fase 1.
Opción B — Empaquetar `curl_cffi` o `cloudscraper` desde día 1 como segunda línea.
Opción C — Implementar Capa 2 usando el session interno de la lib FR24 (custom request).

**Recomendación:** Opción A primero (no agrega deps frágiles). Si falla en Cloud Run, Opción C. Opción B solo si las dos anteriores fallan.

---

## Próximos pasos

1. ✅ Fase 0 docs (este archivo) - hecho
2. Registrar cuenta OpenSky (espera 24-48h) - **acción del usuario**
3. Continuar con Fase 1 (Capa 1 — usa solo la lib, no bloqueada)
4. Antes de Fase 2: experimento controlado de `flight/list.json` desde Cloud Run us-central1
5. Re-correr `validate_sources.py --check fr24-history --tails N301DQ,N821DN,N371DA` desde un Cloud Run efímero para confirmar/refutar el bloqueo Cloudflare

---

## Anexo — Outputs raw

### Check 1 (success)

```
arrivals=100 departures=100
sample tail: N582DN (Delta A321neo, DL923 BOS→ATL)
fields_found: identification.id, aircraft.registration, airline.code.iata,
  airport.origin.code.iata, time.scheduled.{departure,arrival},
  time.real.{departure,arrival}
fields_missing: airport.destination.code.iata (esperado en arrivals al anchor)
```

### Check 2 (blocked)

```
status_code: 403
error: HTTP 403 Cloudflare interstitial
body preview: <!DOCTYPE html>...<title>Just a moment...</title>...
```

### Check 3 (auth required)

```
status_code: 403
error: HTTP 403: You cannot access historical flights
```
