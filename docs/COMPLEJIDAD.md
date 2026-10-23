# Gate de complejidad ciclomática

Desde el 2026-10-22 el CI rechaza funciones nuevas con complejidad ciclomática
mayor a **10** en el código que se despliega. Este documento explica qué mide,
por qué ese umbral, qué quedó exento y cómo se achica la deuda. Es la réplica
del gate del Backend (`OnTimeAI-Backend/docs/COMPLEJIDAD.md`): mismo script,
mismo umbral y mismas versiones de las herramientas, para que los dos repos se
midan igual.

## Qué mide y con qué

La complejidad ciclomática (McCabe, 1976) cuenta los caminos independientes de
una función: 1 más una unidad por cada decisión (`if`, `elif`, `for`, `while`,
`except`, `with`, comprensiones, `and`/`or` en condiciones). A más caminos, más
casos de prueba hacen falta para cubrirla y más fácil es que un cambio rompa un
camino que nadie ejercita.

Se usan dos herramientas con dos roles distintos, las dos fijadas en
`requirements-dev.txt` para que local y CI cuenten igual:

| Herramienta | Rol | Qué cuenta |
|---|---|---|
| `ruff` regla C901 | **gate** (bloquea) | McCabe estándar por función |
| `radon cc` | **reporte** (informa) | McCabe más operadores booleanos; incluye clases y métodos |

Radon da valores más altos que ruff para la misma función porque suma cada
`and`/`or`: `lineage_cache.py:select_tails_to_hydrate` es 24 para ruff y 41
para radon, y `fr24_client.py:normalize_flight` —una cascada de `or` para
elegir entre campos alternativos del JSON de FR24— es 9 para ruff y 35 para
radon. El gate usa ruff porque es el conteo estándar; radon queda para seguir
la evolución y para el informe.

## Alcance

Sólo el código productivo, definido en `PRODUCTIVO` de
`scripts/complejidad.py`: el paquete `ontimeai_scrapper/`, que es lo único que
`Dockerfile.harvester` copia a la imagen del Cloud Run Job. Los scripts de
`scripts/` (`validate_sources.py`, `analyze_future_legs.py`) y los tests quedan
fuera: son herramientas de desarrollo, no se despliegan y reescribirlos no
reduce ningún riesgo operativo.

`requirements.txt` sigue siendo sólo runtime, como exige el Dockerfile. Las
herramientas del gate y pytest viven en `requirements-dev.txt`, que el CI
instala junto con el runtime.

## Umbral y baseline

Umbral 10, el clásico: hasta 10 la función es simple; de 11 a 20 moderada; más
de 20 compleja y difícil de probar exhaustivamente.

Al instalar el gate había **4 funciones productivas por encima de 10**. Están
en `.complejidad-baseline.json`, con el valor con el que entraron. La baseline
es un trinquete:

- una función nueva por encima de 10 **falla** el CI, aunque lleve `# noqa`;
  el check corre con `--ignore-noqa` justamente para eso;
- una función exenta que **crece** por encima de su valor de entrada falla;
- una función exenta que baja de 10 se saca con `--actualizar-baseline`, que
  sólo acepta listas más chicas.

Las funciones exentas llevan `# noqa: C901` en el `def` para que `ruff check`
local pase limpio; la marca no exime nada por sí sola.

## Cómo se usa

```bash
python scripts/complejidad.py --check                  # lo mismo que corre CI
python scripts/complejidad.py --reporte complejidad.json --resumen resumen.md
python scripts/complejidad.py --actualizar-baseline    # tras bajar una función
```

Para bajar una función de la lista: extraer bloques con nombre propio (cada
fase del ciclo, cada validación), reemplazar cascadas de `if` por tablas o
despacho, y cubrir con tests **antes** de mover código. El orden importa: las
dos funciones más complejas están en los archivos con menos cobertura de la
suite actual (`harvester.py` 25 % de líneas, `lineage_cache.py` 47 %, medido
con pytest-cov), así que primero se les escriben tests de caracterización y
recién después se parten. `db.py` está al 80 %, lo que hace de
`reconcile_fr24_flight_aliases` la candidata más barata para empezar.

## Deuda inicial (2026-10-22)

| Función | CC (ruff) | Archivo |
|---|---|---|
| `_run_once` | 24 | `ontimeai_scrapper/harvester.py` |
| `select_tails_to_hydrate` | 24 | `ontimeai_scrapper/lineage_cache.py` |
| `reconcile_fr24_flight_aliases` | 16 | `ontimeai_scrapper/db.py` |
| `fetch_aircraft_history` | 12 | `ontimeai_scrapper/fr24_http.py` |

Reporte radon del código productivo al instalar el gate: 87 bloques, promedio
6,03 (rango B); 12 bloques por encima de 10 y 4 por encima de 20 según su
conteo. Distribución por rango: A=56, B=19, C=8, D=1, E=2, F=1.
