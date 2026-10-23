"""Clientes ADS-B (OpenSky y airplanes.live): degradación ante errores (CP-COB-SCR-02).

Los dos clientes prometen "lista vacía ante cualquier falla", porque ADS-B es
enriquecimiento y no puede tirar el ciclo. Se prueba cada rama de error con
una sesión HTTP falsa, más la normalización de un estado válido y el filtro
de aproximación a ATL.
"""
from __future__ import annotations

import requests

from ontimeai_scrapper import airplanes_live_client as alc
from ontimeai_scrapper import opensky_client as osc


class RespuestaFalsa:
    def __init__(self, status=200, payload=None, invalido=False, text=""):
        self.status_code = status
        self._payload = payload
        self._invalido = invalido
        self.text = text

    def json(self):
        if self._invalido:
            raise ValueError("no es JSON")
        return self._payload


class SesionFalsa:
    def __init__(self, respuesta=None, excepcion=None):
        self.respuesta, self.excepcion, self.llamadas = respuesta, excepcion, []

    def get(self, url, **kw):
        self.llamadas.append((url, kw))
        if self.excepcion:
            raise self.excepcion
        return self.respuesta


def _opensky(monkeypatch, **kw) -> tuple[osc.OpenSkyClient, SesionFalsa]:
    monkeypatch.setattr(osc.time, "sleep", lambda s: None)
    cliente = osc.OpenSkyClient(username="", password="")
    sesion = SesionFalsa(**kw)
    cliente._session = sesion
    return cliente, sesion


ESTADO_VALIDO = ["ABC123", "DAL105 ", "United States", 1, 2, -84.0, 33.5, 1000.0,
                 False, 200.0, 90.0, -2.0, None, 1100.0, "1200", False, 0]


def test_opensky_429_devuelve_vacio(monkeypatch):
    cliente, _ = _opensky(monkeypatch, respuesta=RespuestaFalsa(429))
    assert cliente.get_states_bbox() == []


def test_opensky_error_http_devuelve_vacio(monkeypatch):
    cliente, _ = _opensky(monkeypatch, respuesta=RespuestaFalsa(503, text="down"))
    assert cliente.get_states_bbox() == []


def test_opensky_falla_de_red_devuelve_vacio(monkeypatch):
    cliente, _ = _opensky(monkeypatch, excepcion=requests.ConnectionError("sin red"))
    assert cliente.get_states_bbox() == []


def test_opensky_json_invalido_devuelve_vacio(monkeypatch):
    cliente, _ = _opensky(monkeypatch, respuesta=RespuestaFalsa(200, invalido=True))
    assert cliente.get_states_bbox() == []


def test_opensky_normaliza_los_estados_validos_y_descarta_los_cortos(monkeypatch):
    # Arrange: un estado completo y uno truncado
    cliente, sesion = _opensky(monkeypatch, respuesta=RespuestaFalsa(200, {"states": [ESTADO_VALIDO, ["X", "Y"]]}))
    # Act
    filas = cliente.get_states_bbox()
    # Assert: sólo el válido, con los campos mapeados
    assert len(filas) == 1
    fila = filas[0]
    assert fila["icao24"] == "abc123" and fila["callsign"] == "DAL105"
    assert fila["lat"] == 33.5 and fila["lon"] == -84.0 and fila["on_ground"] == 0
    assert fila["source"] == "opensky" and fila["registration"] is None
    assert sesion.llamadas[0][1]["auth"] is None  # anónimo


def test_opensky_respeta_el_maximo_de_llamadas_por_tick(monkeypatch):
    cliente, sesion = _opensky(monkeypatch, respuesta=RespuestaFalsa(200, {"states": []}))
    cliente._calls_this_run = osc._OPENSKY_MAX_CALLS_PER_TICK
    assert cliente.get_states_bbox() == []
    assert sesion.llamadas == []  # ni siquiera pegó


def test_opensky_usa_auth_si_hay_credenciales(monkeypatch):
    monkeypatch.setattr(osc.time, "sleep", lambda s: None)
    cliente = osc.OpenSkyClient(username="u", password="p")
    sesion = SesionFalsa(respuesta=RespuestaFalsa(200, {"states": []}))
    cliente._session = sesion
    cliente.get_states_bbox()
    assert sesion.llamadas[0][1]["auth"] == ("u", "p")


def test_filtro_de_aproximacion_a_atl():
    estados = [
        {"on_ground": 1, "lat": 33.6, "lon": -84.4},          # en tierra
        {"on_ground": 0, "lat": None, "lon": -84.4},          # sin posición
        {"on_ground": 0, "lat": 45.0, "lon": -84.4},          # lejos
        {"on_ground": 0, "lat": 34.0, "lon": -85.0},          # cerca, en el aire
    ]
    assert list(osc.iter_atl_arrivals_in_air(estados)) == [estados[3]]


def _airplanes(monkeypatch, **kw) -> tuple[alc.AirplanesLiveClient, SesionFalsa]:
    monkeypatch.setattr(alc.time, "sleep", lambda s: None)
    cliente = alc.AirplanesLiveClient()
    sesion = SesionFalsa(**kw)
    cliente._session = sesion
    return cliente, sesion


def test_airplanes_live_error_http_devuelve_vacio(monkeypatch):
    cliente, _ = _airplanes(monkeypatch, respuesta=RespuestaFalsa(500, text="boom"))
    assert cliente.get_aircraft_point() == []


def test_airplanes_live_falla_de_red_devuelve_vacio(monkeypatch):
    cliente, _ = _airplanes(monkeypatch, excepcion=requests.Timeout("lento"))
    assert cliente.get_aircraft_point() == []


def test_airplanes_live_json_invalido_devuelve_vacio(monkeypatch):
    cliente, _ = _airplanes(monkeypatch, respuesta=RespuestaFalsa(200, invalido=True))
    assert cliente.get_aircraft_point() == []


def test_airplanes_live_normaliza_y_salta_los_sin_hex(monkeypatch):
    payload = {"ac": [
        {"hex": "A1B2C3", "r": "N828NW", "t": "A321", "lat": 33.9, "lon": -84.2,
         "alt_baro": 3000, "gs": 250, "baro_rate": -800, "flight": "DAL105  "},
        {"hex": "", "lat": 1, "lon": 1},
        {"hex": "D4E5F6", "alt_baro": "ground", "lat": 33.64, "lon": -84.43},
    ]}
    cliente, sesion = _airplanes(monkeypatch, respuesta=RespuestaFalsa(200, payload))
    filas = cliente.get_aircraft_point()
    assert [f["icao24"] for f in filas] == ["a1b2c3", "d4e5f6"]
    volando, en_tierra = filas
    assert volando["registration"] == "N828NW" and volando["on_ground"] == 0
    assert round(volando["velocity_mps"], 1) == round(250 * 0.5144, 1)
    assert en_tierra["on_ground"] == 1 and en_tierra["baro_altitude_m"] is None
    assert "33.6367/-84.4281/250" in sesion.llamadas[0][0]
