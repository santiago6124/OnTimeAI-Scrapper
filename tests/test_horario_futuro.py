"""
El horario del anchor se pide por adelantado, no solo en vivo.

`get_airport_details` de la libreria FR24 acepta aeropuerto, limite y pagina, y
ninguno es la fecha. Por eso devolvia siempre el tablero en vivo, que llega ~1 h
adelante, y la mitad de las salidas de ATL se veian recien despues de despegar.

El endpoint por debajo si acepta `plugin-setting[schedule][timestamp]`. Medido
contra la API real el 23/09:

    tablero vivo   100 salidas, de -0,0h a  +1,2h
    +6h            100 salidas, de +6,0h a  +7,6h
    +24h           100 salidas, de +24,1h a +25,2h
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from ontimeai_scrapper import fr24_horario
from ontimeai_scrapper.fr24_client import FR24Error


class _Respuesta:
    def __init__(self, payload, status=200):
        self._p, self._s = payload, status
    def get_status_code(self): return self._s
    def get_json_content(self): return self._p


class _ApiFalso:
    """Guarda cada pedido para poder revisar que parametros viajaron."""
    def __init__(self, payload, status=200):
        self.pedidos = []
        self._payload, self._status = payload, status
    def request(self, url, params=None, headers=None, timeout=None):
        self.pedidos.append({"url": url, "params": dict(params or {})})
        return _Respuesta(self._payload, self._status)


class _ClienteFalso:
    def __init__(self, api): self._api = api
    @property
    def api_client(self): return self._api
    def throttled_call(self, nombre, fn, *a, **k): return fn(*a, **k)


def _payload(salidas):
    return {"result": {"response": {"airport": {"pluginData": {"schedule": {
        "departures": {"data": [{"flight": f} for f in salidas],
                       "item": {"total": len(salidas)}}}}}}}}


def _vuelo(fid, en_horas):
    sale = datetime.now(timezone.utc) + timedelta(hours=en_horas)
    return {
        "identification": {"id": fid, "number": {"default": "DL100"}},
        "airline": {"code": {"iata": "DL"}},
        "airport": {"origin": {"code": {"iata": "ATL"}},
                    "destination": {"code": {"iata": "MIA"}}},
        "aircraft": {"registration": "N123DL", "model": {"code": "B739"}},
        "time": {"scheduled": {"departure": int(sale.timestamp())}},
    }


class TestElParametroDeFecha:
    def test_viaja_la_marca_de_tiempo(self) -> None:
        """Sin esto el endpoint devuelve el tablero vivo y no sirve de nada."""
        api = _ApiFalso(_payload([]))
        fr24_horario.pedir_tablero(_ClienteFalso(api), codigo="KATL", horas_adelante=6)

        p = api.pedidos[0]["params"]
        assert "plugin-setting[schedule][timestamp]" in p
        esperado = datetime.now(timezone.utc).timestamp() + 6 * 3600
        assert abs(p["plugin-setting[schedule][timestamp]"] - esperado) < 60

    def test_cada_marca_pide_su_momento(self) -> None:
        api = _ApiFalso(_payload([]))
        fr24_horario.horario_futuro(_ClienteFalso(api), codigo="KATL", horas=(2, 8))

        marcas = [p["params"]["plugin-setting[schedule][timestamp]"] for p in api.pedidos]
        assert len(marcas) == 2
        # Seis horas de diferencia entre una marca y la otra.
        assert abs((marcas[1] - marcas[0]) - 6 * 3600) < 60

    def test_un_error_http_se_reporta(self) -> None:
        api = _ApiFalso(_payload([]), status=503)
        with pytest.raises(FR24Error):
            fr24_horario.pedir_tablero(_ClienteFalso(api), codigo="KATL", horas_adelante=6)


class TestLoQueDevuelve:
    def test_trae_las_salidas_futuras(self) -> None:
        api = _ApiFalso(_payload([_vuelo("aaa111", 6), _vuelo("bbb222", 7)]))
        filas, _ = fr24_horario.horario_futuro(_ClienteFalso(api), codigo="KATL", horas=(6,))
        assert len(filas) == 2

    def test_no_repite_un_vuelo_que_aparece_en_dos_marcas(self) -> None:
        """Marcas contiguas se solapan: el mismo vuelo sale en las dos."""
        api = _ApiFalso(_payload([_vuelo("aaa111", 5)]))
        filas, _ = fr24_horario.horario_futuro(_ClienteFalso(api), codigo="KATL", horas=(4, 6))
        assert len(api.pedidos) == 2, "consulto las dos marcas"
        assert len(filas) == 1, "pero el vuelo entra una sola vez"

    def test_una_marca_que_falla_no_cancela_las_demas(self) -> None:
        """Media ventana capturada es mejor que ninguna."""
        class _Intermitente(_ApiFalso):
            def request(self, url, params=None, headers=None, timeout=None):
                self.pedidos.append({"url": url, "params": dict(params or {})})
                if len(self.pedidos) == 1:
                    return _Respuesta({}, 500)
                return _Respuesta(self._payload, 200)

        api = _Intermitente(_payload([_vuelo("aaa111", 8)]))
        filas, _ = fr24_horario.horario_futuro(_ClienteFalso(api), codigo="KATL", horas=(4, 8))
        assert len(filas) == 1, "la segunda marca se capturo igual"


class TestAlcance:
    def test_mide_cuantas_horas_adelante_llega(self) -> None:
        ahora = datetime.now(timezone.utc)
        filas = [{"scheduled_out_utc": (ahora + timedelta(hours=h)).isoformat()}
                 for h in (1, 9, 4)]
        assert fr24_horario.alcance_horas(filas) == pytest.approx(9, abs=0.1)

    def test_ignora_lo_que_ya_paso(self) -> None:
        ahora = datetime.now(timezone.utc)
        filas = [{"scheduled_out_utc": (ahora - timedelta(hours=3)).isoformat()}]
        assert fr24_horario.alcance_horas(filas) is None


class TestPaginacion:
    """
    Con una sola pagina por marca quedaban huecos entre marcas consecutivas.

    Cada pagina trae 100 salidas y cubre ~1,5 h; la siguiente sigue desde donde
    termino la anterior. Medido contra la API real:

        +4h pagina 1   de +4,0h a +5,6h
        +4h pagina 2   de +5,6h a +6,8h    100 vuelos distintos
        +4h pagina 3   de +6,8h a +8,3h    100 vuelos distintos

    La marca de +2h llegaba a +3,6h y la de +4h arrancaba en +4,0h: entre medio
    no mirabamos nada. Ahi caian los 82 vuelos que seguian sin predecir el
    24/09, de los cuales 74 eran de Delta —la aerolinea del hub, que obviamente
    publica su horario—. No era un limite de FR24: no le pediamos todo.
    """

    def test_pide_mas_de_una_pagina_por_defecto(self) -> None:
        api = _ApiFalso(_payload([_vuelo(f"x{i}", 4) for i in range(100)]))
        fr24_horario.horario_futuro(_ClienteFalso(api), codigo="KATL", horas=(4,))

        paginas = [p["params"]["page"] for p in api.pedidos]
        assert paginas == [1, 2], "una sola pagina deja hueco hasta la marca siguiente"

    def test_deja_de_paginar_si_la_pagina_viene_incompleta(self) -> None:
        """Menos de `limite` significa que no hay mas: no gastar la llamada."""
        api = _ApiFalso(_payload([_vuelo("x1", 4)]))
        fr24_horario.horario_futuro(_ClienteFalso(api), codigo="KATL", horas=(4,))
        assert len(api.pedidos) == 1

    def test_las_paginas_traen_vuelos_distintos(self) -> None:
        class _PorPagina(_ApiFalso):
            def request(self, url, params=None, headers=None, timeout=None):
                self.pedidos.append({"url": url, "params": dict(params or {})})
                pag = int((params or {}).get("page", 1))
                # Cada pagina, vuelos propios: es como responde FR24.
                return _Respuesta(_payload(
                    [_vuelo(f"p{pag}-{i}", 4 + pag) for i in range(100)]), 200)

        api = _PorPagina(_payload([]))
        filas, _ = fr24_horario.horario_futuro(
            _ClienteFalso(api), codigo="KATL", horas=(4,), paginas_por_marca=2)
        assert len(filas) == 200, "las dos paginas suman, no se pisan"
