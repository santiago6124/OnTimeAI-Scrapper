# Design — Future-leg capture (extend departure lead time)

**Status:** implemented behind the `CAPTURE_FUTURE_LEGS` env flag (default **off**).
Pending: run `scripts/analyze_future_legs.py` to validate the horizon gain (gate: median
captured lead ≥ 120 min) before flipping the flag on in production.
**Goal:** give the backend a multi-hour pre-departure prediction horizon, instead of the current ~75 min.

---

## 1. Problem

Both providers' *airport boards* (AeroAPI `scheduled_departures`, FR24 `get_airport_details`)
only surface flights ~1 h before departure. Evidence (harvester's own 01:47 upload):
124 k FR24 flights ingested, yet the furthest-future ATL departure was **02:50 — ~75 min
ahead**. So a flight is first discovered (and predicted) minutes before pushback.

The backend already re-predicts every still-upcoming flight each cycle
(`live_pull.py`, decoupled target + `COALESCE(estimated_out_utc, scheduled_out_utc)` guard),
so the only thing missing is **earlier discovery**.

## 2. The free source we currently discard

Capa 2 chain-walk (`fr24_http.fetch_aircraft_history`) pulls each tail's full
`flight/list.json` itinerary — **future + past**. Its own docstring:

> "Devuelve el itinerario completo del tail (futuro + pasado)… **Skipeamos vuelos
> futuros con id==null** porque Capa 1 los capturará cuando se acerquen al anchor."

That skip is the trap. A tail landing at ATL now has its **next ATL departure**
(turnaround, 1–3 h out) sitting in the same response we already paid for. ~30 tails are
chain-walked per tick → tens of future ATL legs discoverable **for free** (no extra FR24
calls, no new endpoints, no extra TOS exposure).

`normalize_flight` returns `None` for these because FR24 leaves `identification.id == null`
until close to departure (`fr24_client.py:229`). Everything else we need is present:
`identification.number.default`, `airport.origin/destination`, `time.scheduled`,
`aircraft.registration`.

## 3. The hard part: dedup / identity

If we persist a future leg under a made-up id and Capa 1 later inserts the **same physical
flight** under FR24's real id, we get **two `flights` rows** → double prediction + duplicate
entries in `/flights`. The PK is `fa_flight_id`, so an upsert can't merge them.

**Identity key** of a physical flight (stable across the null→real id transition):

```
(op_carrier, flight_number, fl_date, origin, dest)
```

Both the synthetic leg and the real-id row compute this identically inside `normalize_flight`
(`fl_date` is derived from origin-tz the same way in both paths).

### Chosen approach: synthetic placeholder + reconcile-and-delete

1. **Synthesize a deterministic id** for captured future legs:
   ```
   SYN-{op_carrier}{flight_number}-{origin}-{dest}-{fl_date}
   ```
   Set both `fa_flight_id` and `stable_id` to this. The `SYN-` prefix makes placeholders
   trivially identifiable.

2. **Reconcile each tick** (new `db.reconcile_synthetic_flights(conn)`, called at the end of
   `harvester.main()` after Capa 1): find every `SYN-…` row whose identity key now also has a
   **real-id** row, then repoint history and drop the placeholder:
   ```sql
   -- pairs (syn_id, real_id) sharing identity, real-id present
   WITH ident AS (
     SELECT fa_flight_id, op_carrier, flight_number, fl_date, origin, dest,
            (fa_flight_id LIKE 'SYN-%') AS is_syn
     FROM flights
     WHERE fl_date >= date('now','-1 day')
   )
   SELECT s.fa_flight_id syn_id, r.fa_flight_id real_id
   FROM ident s JOIN ident r
     ON  s.op_carrier=r.op_carrier AND s.flight_number=r.flight_number
     AND s.fl_date=r.fl_date AND s.origin=r.origin AND s.dest=r.dest
   WHERE s.is_syn=1 AND r.is_syn=0;
   ```
   For each pair (predictions/actuals may already reference the SYN id):
   ```sql
   UPDATE OR IGNORE predictions SET fa_flight_id = :real WHERE fa_flight_id = :syn;
   UPDATE OR IGNORE actuals     SET fa_flight_id = :real WHERE fa_flight_id = :syn;
   DELETE FROM predictions WHERE fa_flight_id = :syn;   -- drop rows that collided (OR IGNORE)
   DELETE FROM actuals     WHERE fa_flight_id = :syn;
   DELETE FROM flights     WHERE fa_flight_id = :syn;
   ```
   `UPDATE OR IGNORE` preserves the real-id prediction when both ids already predicted at the
   same `predicted_at_utc` (the PK is `(fa_flight_id, predicted_at_utc)`); the leftover SYN
   duplicate is then deleted. Net: one row, full prediction history retained under the real id.

3. **TTL cleanup** for placeholders that never reconcile (cancelled, or never appeared on the
   board): in the same function, delete `SYN-` rows with
   `datetime(scheduled_out_utc) < datetime('now','-3 hours')` and no actuals.

### Alternatives considered
- **Backend-side grouping** by identity in the prediction-target + serving queries — avoids a
  harvester reconciliation step but leaves predictions split across two ids and pushes
  identity logic into two backend queries. Rejected: more places to keep in sync.
- **Rewrite `stable_id` globally** to the identity key so real+syn link via `stable_id` — too
  invasive; the backend joins `actuals` by `stable_id` and AeroAPI rows rely on the current
  semantics.

## 4. What to capture (scope)

In `fetch_aircraft_history`, for items with `id == null`:
- keep only **ATL-relevant** legs (`origin == 'ATL' OR dest == 'ATL'`),
- with a `scheduled_out` in the **future** (and within, say, the next 12 h),
- not cancelled.

Both directions are worth it: `origin=ATL` gives early departure forecasts; `dest=ATL` lets
us predict an arrival's delay *before it leaves its origin* — exactly the "predict before
departure" goal for arrivals too.

## 5. Code touch-points

| File | Change |
|---|---|
| `fr24_client.py` | `normalize_flight(…, allow_synthetic_id=False)`. When `True` and `id` is null but `(op_carrier, flight_number, fl_date, scheduled_out)` are present, set `fa_flight_id = stable_id = _synthetic_id(...)`. Default path unchanged. Add `_synthetic_id(row)` helper. |
| `fr24_http.py` | In `fetch_aircraft_history`, normalize `id==null` items with `allow_synthetic_id=True`; keep only future ATL-relevant legs; count them (`n_future_legs`). |
| `db.py` | New `reconcile_synthetic_flights(conn) -> dict` (counts: reconciled, ttl_purged). No schema change — `SYN-` lives in existing `flights`. |
| `harvester.py` | Call `reconcile_synthetic_flights` after Capa 1 (and after Capa 2); log counts; record in `harvester_runs`. |

No backend changes required — `live_pull.py` Fix 1 already targets `origin='ATL'` upcoming
flights, so `SYN-` ATL departures get predicted as soon as they're discovered.

## 6. Validation plan (before enabling writes)

1. **Dry-run**: run chain-walk with `--dry-run`, log per tick: `# future ATL legs found`,
   and the distribution of `scheduled_out - now`. Confirm it actually reaches hours, not
   minutes. (Gate: median lead of captured legs ≥ 2 h.)
2. **Reconciliation correctness** on a copy of `live_data.db`: seed a `SYN-` row + a matching
   real-id row, run `reconcile_synthetic_flights`, assert one row remains (real id) and
   predictions were repointed.
3. **No duplicates** in `/flights` after a full tick (group by identity key, expect count 1).
4. **TTL**: a `SYN-` row past `scheduled_out + 3h` with no real match is purged.

## 7. Risks

- **Codeshare / flight-number reuse** → mis-merge. Mitigated by including `origin`+`dest` in
  the identity key; residual risk low for ATL mainline ops.
- **Partial features** on synthetic rows (no tail/lineage until hydrated) → slightly weaker
  early predictions, which refine each cycle as real data arrives. Acceptable.
- **Extra prediction volume** (more rows predicted/cycle) — bounded by the 12 h horizon;
  local inference, no API cost.
- **TOS**: reuses existing chain-walk calls only; no new endpoints. Neutral.

## 8. Rollout

Behind `CAPTURE_FUTURE_LEGS` env flag (default off). Validate with the dry-run first, inspect
the lead-time histogram, then turn on writes. Reconciliation (`db.reconcile_synthetic_flights`)
runs every tick **regardless** of the flag, so any stray `SYN-` rows are always cleaned even
after turning it off.

## 9. Measured results (2026-06-01)

Dry-run `python -m scripts.analyze_future_legs --tails 30` against live FR24:

```
Future ATL legs captured: 31 (from 30 tails, ~1.0/tail)
Lead time (min ahead of now): min=338 p25=458 median=521 p75=608 p90=647 max=717
Legs >2h ahead: 100%  |  >4h ahead: 100%
Gate: median >= 120 min -> PASS
```

**Median lead ≈ 8.7 h** vs the ~75 min airport board. End-to-end test (capture →
`upsert_flights` → `reconcile_synthetic_flights` on a throwaway DB copy) landed 12 synthetic
ATL departures with no crashes; reconcile clean (0 collapsed — none on the board yet).

### Interaction with the backend horizon (important)
The captured legs sit at ~6–11 h out, but the backend Fix 1 target query defaults to a **6 h**
horizon (`PREDICT_HORIZON_HOURS`). Measured on the test DB:

| Backend horizon | SYN ATL deps predicted |
|---|---|
| 6 h  | 1 |
| 8 h  | 3 |
| 12 h | 12 (all) |

**To capitalise on capture, the two flags must be enabled together:**
1. Harvester job: `CAPTURE_FUTURE_LEGS=true`
2. Backend job: `PREDICT_HORIZON_HOURS=12`

Otherwise legs at 6–12 h are discovered but not predicted until they slip inside 6 h.

## 10. Status / files

Implemented, validated, **flag off** pending enablement.

| File | Change |
|---|---|
| `fr24_client.py` | `_synthetic_id` + `normalize_flight(allow_synthetic_id=)` |
| `fr24_http.py` | `fetch_aircraft_history(capture_future_legs=)` + `_is_future_atl_leg` |
| `lineage_cache.py` | thread flag into `maybe_hydrate_tail` |
| `db.py` | `reconcile_synthetic_flights` |
| `harvester.py` | call reconcile each tick |
| `config.py` | `CAPTURE_FUTURE_LEGS`, `FUTURE_LEG_HORIZON_HOURS` |
| `scripts/analyze_future_legs.py` | dry-run lead-time histogram (writes nothing) |
| `tests/test_future_legs.py` | synthetic-id, filter, reconciliation |
