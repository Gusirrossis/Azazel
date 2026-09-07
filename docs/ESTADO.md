# Estado del proyecto — medido el 2026-09-05

Este documento tiene **fecha en el título a propósito**. Todo lo de aquí son cifras
medidas contra producción, y las cifras caducan. Si lo lees mucho después, vuelve a
medir con los comandos de la última sección antes de fiarte de un número.

Lo que **no** cambia con el tiempo —cómo está construido el sistema— vive en
[`ARQUITECTURA.md`](ARQUITECTURA.md). Cómo operar el servidor, en
[`OPERACION_VPS.md`](OPERACION_VPS.md).

---

## 1. El corpus

| | Filas | % |
|---|---:|---:|
| `archivos` catalogados | 28.829.247 | 100 % |
| ├ `PENDIENTE` | 28.017.883 | 97,19 % |
| ├ `ERROR` | 411.293 | 1,43 % |
| ├ `INDEXADO` | 362.702 | 1,26 % |
| ├ `HECHO` | 27.309 | 0,09 % |
| └ `COLD` | 10.060 | 0,03 % |
| Documentos en el índice | 390.011 | 1,35 % del catálogo |
| └ de esos, con `texto_indexable` | 307.181 | |

> **El 97 % del corpus no se ha procesado nunca.** No es un fallo: es trabajo
> pendiente de pipeline, y corre en `mac-01`, no en el VPS. Pero condiciona todo lo
> demás — cualquier cifra de entidades, nombres o cobertura sale de ese 1,35 %.

`contexto_anclas` está **vacío en el 100 % de los documentos**. El campo existe en el
mapping pero ningún documento se ha reindexado desde que se añadió.

---

## 2. Entidades

| | |
|---|---:|
| Personas activas | 89.652 |
| ├ ancladas en CURP | 82.408 |
| └ ancladas en RFC | 7.244 |
| **Con `nombre_completo`** | **1.204** (1,34 %) |

Los nombres no se declaran: se extraen del texto y se **verifican contra las siete
letras que la propia CURP codifica** del nombre (iniciales y vocal interna del
paterno, inicial del materno, inicial del pila, y las tres consonantes internas).
Solo se guarda lo que verifica — ver `entidades/nombres.py`.

**Por qué solo el 1,34 %, y por qué no sube ajustando el extractor.** Medido sobre
muestra aleatoria: el 100 % de las anclas sale de `texto_indexable`, pero en la
mayoría de documentos el nombre no está junto a la CURP. Se comprobó ampliando la
ventana de contexto de ±200 a ±5.000 caracteres: **cero nombres más en los cuatro
radios**. El nombre no está lejos, no está.

Donde sí está —ficheros estructurados tipo padrón— el acierto es del **83–86 %**.

> Cuidado con una medición sesgada que se hizo primero y dio 86 % global: muestreaba
> documentos que mencionan la palabra «curp», que son justo los ricos en nombres. La
> cifra buena es la de muestra aleatoria.

---

## 3. Base de datos

| | |
|---|---|
| Revisión de esquema | **0011** |
| Tabla `archivos` | 19 GB (11 GB datos + 7.897 MB índices) |

Migraciones desde la 0007:

| | Qué |
|---|---|
| `0008` | `usuarios` y `sesiones` — login de personas |
| `0009` | `extracciones` — caché de extracción por `hash_contenido` + versión |
| `0010` | `pg_trgm` e índices parciales sobre `entidades` (nombre, CURP, RFC) |
| `0011` | `ix_archivos_estado_id` → parcial que excluye `PENDIENTE` |

**Sobre la 0011.** El índice pesaba 3.795 MB y el planificador **no lo usaba** para
`PENDIENTE` —el 97,19 % de las filas—, porque con un filtro que no discrimina la
clave primaria encuentra 50 coincidencias enseguida. El parcial cubre solo el 2,81 %
selectivo y ocupa **77 MB**: 3,7 GB recuperados sin que ningún plan empeore,
verificado con `EXPLAIN ANALYZE` antes y después.

---

## 4. Qué corre solo en el VPS

| Tarea | Estado |
|---|---|
| Respaldo diario de Postgres | **activo** — cron 09:30 UTC (03:30 en México) |
| Réplica del índice | **NO configurada** |

El respaldo se montó el 2026-09-05; antes no existía ninguno automático. Último
éxito verificado: `2026-09-05T00:27:27Z`, 1,83 GB, contenido comprobado **desde
fuera** bajando el fichero de MinIO y contando filas (89.652 entidades dentro, no
solo un «OK» del script).

**La réplica nunca ha corrido en este VPS**: no hay timers de systemd, no hay
WireGuard y no hay cursor de réplica en `control`. Los datos de `mac-01` que hay en
el índice llegaron por otra vía.

**El respaldo no alerta.** Deja el resultado en `/var/lib/azazel/respaldo-estado` y
en `/var/log/azazel-respaldo.log`, pero nadie recibe aviso.

---

## 5. Federación con Lilith

Clave con nombre `lilith`, dada de alta el **2026-09-04 20:53 UTC**. Verificado el
2026-09-05 con una clave de consumidor creada y borrada para la prueba:

| Petición | Código |
|---|---|
| `POST /buscar` | 200 |
| `GET /archivo/{id}/contenido` | **403** |
| `GET /sistema/carpetas` | **403** |
| `GET /seguridad/claves-busqueda` | **403** |

Es decir: **Lilith consulta, no descarga**, que es la decisión que se tomó.

Medido con la llamada real: `campos` ahorra el 98 % del tráfico (564.150 B →
11.337 B en la misma consulta de 20 resultados); una CURP exacta devuelve
`total: 0` con **una entidad**, así que quien federa no puede leer `total` sin mirar
`entidades`.

---

## 6. Lo que está pendiente, por orden

1. **Rotar la clave estática de `NORM_API_KEYS`.** Se imprimió en un transcript de
   trabajo el 2026-09-05. No es una clave de consumidor: entra como **`admin`** y
   puede descargar cualquier archivo.
2. **Reconstruir la imagen en `mac-01`** para que los documentos nuevos salgan con
   nombre verificado. En el VPS no hace falta: la API sirve los nombres leyéndolos
   de la base.
3. **Decidir sobre la réplica**: montarla de verdad, o quitar de la documentación la
   promesa de que existe (esto último ya está hecho).
4. **Alerta de respaldo fallido** — exponer el marcador de estado como métrica para
   que salte en el Grafana que ya se mira.
5. **T4 del filtro** — bloqueado por el set etiquetado.
6. **De Fase 2**: resolución difusa, grafo de relaciones, control de acceso por
   campo (PII).
7. Los **411.293 archivos en `ERROR`** no son recuperables desde el VPS: no tienen
   `hash_contenido` y el disco de origen no está montado aquí.

### Un riesgo abierto que conviene mirar: HTTP/3 anunciado

Medido el 2026-09-05 desde fuera:

```
$ curl -sI https://162-35-188-181.sslip.io/salud | grep -i alt-svc
Alt-Svc: h3=":443"; ma=2592000
```

Caddy publica `443/udp` y con él anuncia HTTP/3 con **30 días** de vigencia
(`ma=2592000`). Un navegador guarda ese anuncio por perfil durante todo ese tiempo,
y si la red del usuario no deja pasar UDP las peticiones **mueren antes de salir**:
sin código de estado, sin cabeceras y **sin una sola línea en los registros del
servidor**. En Lilith este mismo fallo costó una tarde entera y obligó a apagar
HTTP/3 y mandar `Alt-Svc: clear`.

No afecta a la federación —Lilith llama de servidor a servidor— pero **sí a quien
use el panel desde un navegador**. No se ha reproducido aquí; queda como riesgo
identificado, no como incidente.

### Lo que se evaluó y se decidió NO hacer

**Normalizar las columnas repetidas de `archivos`.** Se dimensionó en 8–12 GB y al
medirlo son **314 MB** (`senales` 197 MB, `error_motivo` 86 MB, `version_filtro`
18 MB, `motivo` 13 MB). La estimación original estaba mal por un factor de 25:
extrapolaba bytes por fila sin contar que `pg_column_size` sobre JSONB ya devuelve
el tamaño comprimido y que 28 de los 28,8 millones de filas tienen esas columnas
vacías. No compensa el trabajo.

De los índices restantes, `ix_archivos_claim` (3.794 MB / 177.482 barridos) y la
clave primaria (3.443 MB / 29 M) se ganan el sitio de sobra.

---

## 7. Pruebas

La suite unitaria pasa salvo **tres fallos preexistentes**, verificados contra un
árbol limpio en `HEAD` para confirmar que no son regresiones:

```
tests/unit/test_contenedores.py::TestRar::test_rar5_truncado_se_marca_sin_reventar
tests/unit/test_pipeline_destino.py::TestResolverWorkers::test_prioridad_front_sobre_perilla_sobre_auto
tests/unit/test_reglas.py::TestDeteccionT1::test_texto_queda_para_t2
```

**Los tests de integración no pueden correr contra producción**, y hay tres barreras
independientes: la imagen no trae `pytest`, los tests no están en la imagen, y la
guarda del `conftest` aborta si `archivos` supera 1.000 filas (producción tiene
28.829.247). Importa porque la fixture hace `TRUNCATE` de once tablas, incluidas
`entidades` y `usuarios`.

---

## 8. Cómo volver a medir todo esto

```bash
ssh azazel
cd /srv/azazel/normalizacion-backend
PG=$(grep -E '^NORM_PG_USER=' .env.prod | cut -d= -f2-)
Q() { docker exec normalizacion-postgres-1 psql -U "$PG" -d normalizacion -c "$1"; }

# corpus y entidades
Q "select estado, count(*) from archivos group by estado order by count(*) desc"
Q "select count(*) total, count(*) filter (where campos ? 'nombre_completo') con_nombre
   from entidades where activo"

# tamaño y esquema
Q "select pg_size_pretty(pg_total_relation_size('archivos')) total,
          pg_size_pretty(pg_indexes_size('archivos')) indices"
Q "select version_num from alembic_version"

# qué corre solo
crontab -l; systemctl list-timers 'azazel-*'
cat /var/lib/azazel/respaldo-estado

# índice
docker exec normalizacion-api-1 python -c "
from normalizacion.core.config import cargar_config
from normalizacion.core.indexador.opensearch import crear_cliente
c=cargar_config(); print(crear_cliente(c).count(index=c.indice_alias))"
```
