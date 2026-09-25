"""Capa 1B — el horario futuro del anchor, pedido directamente.

`get_airport_details` de la libreria FR24 acepta tres parametros —aeropuerto,
limite y pagina— y ninguno es la fecha. Por eso siempre devuelve el tablero en
vivo, y el tablero en vivo llega hasta ~1 h adelante:

    tablero vivo    100 salidas, de -0,0h a +1,2h

El endpoint por debajo si acepta `plugin-setting[schedule][timestamp]`, que es
lo que usa la propia web de FR24 cuando se mira el horario de maniana. Medido
contra la API real el 23/09:

    +6h    100 salidas, de  +6,0h a  +7,6h
    +12h   100 salidas, de +12,6h a +16,8h
    +24h   100 salidas, de +24,1h a +25,2h

O sea que el horario completo estaba disponible todo el tiempo. Toda la
maquinaria de seguir aviones uno por uno para deducir sus proximos tramos se
construyo para esquivar una limitacion que solo existe en el tablero en vivo.

`FUTURE_LEG_CAPTURE_DESIGN.md` dice "ambos proveedores solo muestran vuelos ~1 h
antes de salir". Es cierto del tablero en vivo; no del endpoint con fecha.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from FlightRadarAPI.core import Core

from . import config
from .fr24_client import FR24Client, FR24Error, normalize_actual, normalize_flight

log = logging.getLogger(__name__)

FR24_AIRPORT_URL = f"{Core.api_flightradar_base_url}/airport.json"

# Cada pagina trae 100 salidas y cubre ~1,5 h; la siguiente sigue avanzando en
# el tiempo desde donde termino la anterior:
#
#     +4h pagina 1   de +4,0h a +5,6h
#     +4h pagina 2   de +5,6h a +6,8h    (100 vuelos distintos)
#     +4h pagina 3   de +6,8h a +8,3h    (100 vuelos distintos)
#
# Con una sola pagina por marca quedaban huecos entre marcas consecutivas: la
# de +2h llegaba a +3,6h y la de +4h arrancaba en +4,0h. Ahi caian los vuelos
# que seguian sin predecir —74 de 82 eran de Delta, la aerolinea del hub, que
# obviamente publica su horario—. No era un limite de FR24: no le pediamos todo.
HORAS_ADELANTE_DEFECTO = (2, 4, 6, 8, 10, 12)

# Dos paginas por marca: cada una cubre ~3 h y las marcas van cada 2 h, asi que
# se solapan en vez de dejar hueco. Son 12 llamadas por ciclo en vez de 6.
PAGINAS_POR_MARCA_DEFECTO = 2


def _params(codigo: str, limite: int, pagina: int, cuando: int | None) -> dict[str, Any]:
    p: dict[str, Any] = {
        "code": codigo,
        "limit": int(limite),
        "page": int(pagina),
        "format": "json",
    }
    if cuando is not None:
        # El corchete va literal: es como lo arma la web de FR24. El cliente
        # HTTP se encarga de escaparlo.
        p["plugin-setting[schedule][timestamp]"] = int(cuando)
    return p


def pedir_tablero(
    cliente: FR24Client,
    *,
    codigo: str,
    horas_adelante: float,
    limite: int = 100,
    pagina: int = 1,
) -> dict[str, Any]:
    """El tablero del anchor tal como estara dentro de `horas_adelante`."""
    api = cliente.api_client
    if api is None:
        raise FR24Error("APIClient interno no disponible (¿lib FR24 instalada?)")

    cuando = int(datetime.now(timezone.utc).timestamp() + horas_adelante * 3600)

    def _pedir():
        resp = api.request(
            FR24_AIRPORT_URL,
            params=_params(codigo, limite, pagina, cuando),
            headers=Core.json_headers,
            timeout=int(config.FR24_TIMEOUT_SECONDS),
        )
        estado = resp.get_status_code()
        if estado >= 400:
            raise FR24Error(f"airport.json HTTP {estado} para {codigo} +{horas_adelante}h")
        return resp.get_json_content()

    return cliente.throttled_call(f"airport(+{horas_adelante}h,p{pagina})", _pedir)


def _salidas(payload: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        sched = (payload["result"]["response"]["airport"]["pluginData"]["schedule"])
    except (KeyError, TypeError):
        return []
    datos = (sched.get("departures") or {}).get("data") or []
    return [d.get("flight") or {} for d in datos if isinstance(d, dict)]


def horario_futuro(
    cliente: FR24Client,
    *,
    codigo: str = config.AIRPORT_CODE,
    horas: tuple[float, ...] = HORAS_ADELANTE_DEFECTO,
    limite: int = 100,
    paginas_por_marca: int = PAGINAS_POR_MARCA_DEFECTO,
) -> tuple[list[dict], list[dict]]:
    """Filas de `flights` y `actuals` del horario por delante del anchor.

    Devuelve filas ya normalizadas, listas para el mismo UPSERT que usa la
    capa 1. Se deduplica por `fa_flight_id` porque dos marcas de tiempo
    contiguas suelen solaparse.

    Un fallo en una marca no cancela las demas: media ventana capturada es
    mejor que ninguna, y el log deja constancia de cual falto.
    """
    vistos: set[str] = set()
    filas_f: list[dict] = []
    filas_a: list[dict] = []

    for h in horas:
        for pagina in range(1, paginas_por_marca + 1):
            try:
                payload = pedir_tablero(
                    cliente, codigo=codigo, horas_adelante=h,
                    limite=limite, pagina=pagina,
                )
            except FR24Error as exc:
                log.warning("horario +%sh pagina %s fallo: %s", h, pagina, exc)
                continue

            crudas = _salidas(payload)
            nuevas = 0
            for cruda in crudas:
                # `allow_synthetic_id` es lo que hace util esto: FR24 devuelve
                # los vuelos futuros con `identification.id` en null, porque
                # todavia no existe el vuelo como tal. Sin id sintetico se
                # descartarian justo los que venimos a buscar.
                fila = normalize_flight(
                    cruda, anchor_airport=codigo, is_arrival_side=False,
                    allow_synthetic_id=True,
                )
                if not fila:
                    continue
                fid = fila.get("fa_flight_id")
                if not fid or fid in vistos:
                    continue
                vistos.add(fid)
                filas_f.append(fila)
                nuevas += 1
                actual = normalize_actual(cruda)
                if actual:
                    filas_a.append(actual)

            log.info("horario: +%sh pagina=%s crudas=%s nuevas=%s",
                     h, pagina, len(crudas), nuevas)
            if len(crudas) < limite:
                break  # la marca no tiene mas paginas

    return filas_f, filas_a


def alcance_horas(filas: list[dict]) -> float | None:
    """Cuantas horas adelante llega lo capturado. Para medir si sirvio."""
    ahora = datetime.now(timezone.utc)
    futuros = []
    for f in filas:
        cuando = f.get("scheduled_out_utc")
        if not cuando:
            continue
        try:
            t = datetime.fromisoformat(str(cuando).replace("Z", "+00:00"))
        except ValueError:
            continue
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        if t > ahora:
            futuros.append((t - ahora) / timedelta(hours=1))
    return max(futuros) if futuros else None
