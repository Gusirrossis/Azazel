# Compactar la tabla `archivos` (matriz)

**Cuándo:** DESPUÉS del reproceso, con la replicación pausada. Nunca a la vez que otra
carga pesada de disco: el disco de la matriz es uno solo y mecánico (~400
operaciones/s).

**Fuera de 09:00–11:30 UTC.** A las 09:30 UTC arranca el respaldo diario (cron →
`deploy/respaldo-cron.sh` → `pg_dump`). Medido en `/var/log/azazel-respaldo.log`: 1 min
el 23-09, 33 min el 24-09 y 1 h 27 min el 25-09, con el disco saturado. Un `pg_dump` en
curso tiene `archivos` abierta: el VACUUM FULL fallaría a los 10 s y se perdería la
ventana. Y si el VACUUM FULL va primero, el respaldo espera detrás y luego compite por
el disco.

**Qué se gana:** que las lecturas completas de `archivos` (exportador, `/panel`) dejen
de recorrer 11 GB de heap, y que cada pasada de autovacuum deje de leer 8 GB de
índices. Compactada, la tabla quedaría en ~1–1,5 GB en total (estimación).

Nada de esto borra filas. VACUUM FULL es transaccional: si se cancela, la tabla queda
exactamente como estaba.

## Por qué hace falta (medido el 25-09)

| | medido |
|---|---|
| filas vivas | 853.382 (en su día llegó a 28,8 M; se borraron 28,5 M) |
| heap | 11 GB (1.459.519 páginas, 0,58 filas por página) |
| índices | 8 GB: `ix_archivos_claim` 3.794 MB, `archivos_pkey` 3.443 MB, el resto < 250 MB cada uno |
| páginas borradas en índices (log de autovacuum) | `ix_archivos_claim`: 464.973 de 485.665 (96 %); `archivos_pkey`: 177.734 de 440.733 (40 %) |
| pasadas de autovacuum (log de PG, 23 al 25-09) | 24 min a 6,4 h cada una; ~1 M de páginas leídas por pasada a 0,3–4 MB/s |
| consulta del exportador por ruta | Parallel Seq Scan de los 11 GB, 2,7 s de media |

El inflado lo dejó el DELETE masivo. El vacuum normal marca ese espacio como
reutilizable, pero no lo devuelve al disco y tampoco estrecha los índices. Cada pasada
de autovacuum sigue leyendo los índices enteros, con sus 8 GB.

## Qué opción usar

| | VACUUM FULL | REINDEX CONCURRENTLY | pg_repack |
|---|---|---|---|
| Heap (11 GB) | lo reescribe (~0,75 GB) | no lo toca | lo reescribe |
| Índices (8 GB) | los reconstruye (~0,3–0,6 GB) | los reconstruye | los reconstruye |
| Bloqueo | ACCESS EXCLUSIVE durante toda la operación: nadie lee ni escribe `archivos` | SHARE UPDATE EXCLUSIVE: lecturas y escrituras siguen | exclusivo solo al principio y al final |
| Lecturas del heap | 1 pasada secuencial | 2 pasadas completas **por índice** (4 para los dos grandes, 16 para los 8) | 1 pasada |
| Disponible hoy | sí | sí | **no**: no aparece en `pg_available_extensions` de `postgres:16-alpine` (comprobado el 25-09) |

**Recomendado: VACUUM FULL en una ventana corta.** Mientras la tabla siga inflada,
REINDEX CONCURRENTLY es la opción que MÁS disco gasta: cada índice recorre dos veces
el heap de 11 GB. Queda como plan B (abajo) para cuando no haya ventana. pg_repack
exigiría una imagen de Postgres propia con la extensión compilada, y cambiar de imagen
obliga a reiniciar Postgres, es decir, también hay corte. No compensa para una
compactación puntual.

**Qué se queda bloqueado durante el VACUUM FULL:** todo lo que lee `archivos`
(`/panel`, `/resumen`, `/cobertura`, `/cola/*`, workers, exportador). `/buscar` y
`/archivo/*` no la leen (van a OpenSearch y MinIO) y siguen funcionando, **siempre que
no se reinicie ni se reconstruya `api` durante la ventana**: su arranque ejecuta
`alembic upgrade head`, y una migración pendiente sobre `archivos` (la 0013) esperaría
al VACUUM FULL. Hay otra trampa: los endpoints de la API son síncronos y comparten el
pool de hilos de anyio (40 hilos), y el front pide `/panel` con `setInterval` cada 8 y
12 s aunque la petición anterior no haya vuelto. Sin protección, cada petición se queda
esperando el lock con un hilo tomado. El 24-09 se midieron 37–39 `GET /panel` en 3 min
desde un solo navegador. A ese ritmo, los 40 hilos se agotan en unos 3 min (cálculo, no
observado) y a partir de ahí `/buscar` también se para. El paso 4 lo evita.

**Duración (estimación, no medida):** el coste es leer 11 GB en secuencial y escribir
~2–3 GB (heap nuevo, índices y WAL). Si el heap está en la caché del SO, son pocos
minutos. Hoy lo está porque el exportador lo relee sin parar; con el exportador nuevo
puede dejar de estarlo. Si se lee del disco:
3–8 min con el disco tranquilo y 20–30 min si compite con restauraciones. El paso 6
mide el avance real y dice si hay que abortar.

**Espacio libre necesario:** ~3 GB (tabla nueva + índices + WAL). Los ficheros viejos
se liberan al terminar. Medido: 20 TB libres.

## Procedimiento (VACUUM FULL)

```bash
cd /srv/azazel/normalizacion-backend
C="docker compose -f deploy/docker-compose.prod.yml --env-file .env.prod --profile datos --profile app --profile obs"
PG="docker exec -i normalizacion-postgres-1 psql -U norm -d normalizacion -v ON_ERROR_STOP=1"
```

### 1. Precondiciones (solo lectura)

```bash
date -u   # fuera de 09:00–11:30 UTC

# Ningún respaldo en curso. pg_dump se presenta con su nombre.
$PG -tAc "SELECT pid, now() - backend_start FROM pg_stat_activity WHERE application_name = 'pg_dump'"   # vacío

# La ventana es para DESPUÉS del reproceso: con una corrida EN_CURSO, espera a que acabe.
$PG -tAc "SELECT id, disco_id, iniciada_en FROM corridas WHERE estado = 'EN_CURSO'"   # vacío

# Quién está conectado. Los workers llevan su worker_id como application_name
# (pipeline-wN@<host>:<pid>); la API y el exportador van sin nombre.
$PG -tAc "SELECT application_name, state, count(*) FROM pg_stat_activity
          WHERE datname = current_database() AND backend_type = 'client backend'
          GROUP BY 1, 2 ORDER BY 1, 2"
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}'   # léela ENTERA; norm-* son corridas

# Transacciones largas (VACUUM FULL esperaría detrás de ellas). Un autovacuum sobre
# archivos no cuenta: Postgres lo cancela ~1 s después de que VACUUM FULL pida el lock,
# salvo que sea anti-wraparound, y no lo será mientras esta edad quede lejos de 200 M
# (el 25-09 era 14,8 M).
$PG -tAc "SELECT pid, now() - xact_start, state, left(query, 60) FROM pg_stat_activity
          WHERE datname = current_database() AND xact_start < now() - interval '1 minute'"
$PG -tAc "SELECT age(relfrozenxid) FROM pg_class WHERE relname = 'archivos'"

# Estado de la pausa ANTES de tocar nada: el paso 8 solo reanuda si no estaba pausado.
# Sin fila en `control` = nunca se pausó (así estaba el 25-09).
$PG -tAc "SELECT coalesce((SELECT valor FROM control WHERE clave = 'pausado'), 'false')" \
  | tee /root/pausa_antes.txt

# Espacio
df -h /var/lib/docker/volumes/normalizacion_pgdata/_data
```

No sirve mirar si quedan filas PENDIENTE o PRECALIFICADO: pueden quedar cientos de
miles aparcadas sin que nadie escriba (el 25-09 había 362.245 PENDIENTE). Si alguien
escribe lo dice la huella del paso 2.

Mira también que la replicación siga pausada: el cron de las lunas debe estar comentado.

### 2. Pausar, dejar drenar, parar lo que quede y comprobar que nadie escribe

```bash
$C exec -T api norm pausar
```

Con la pausa, cada worker termina su lote y sale. Espera a que no quede ningún lote
reclamado; con archivos grandes, un lote puede tardar. Repite cada minuto hasta que las
dos cuentas den 0:

```bash
# Filas reclamadas con lease vivo (va por ix_archivos_lease). Las que están en backoff
# (worker_id NULL, lease_hasta futuro) no cuentan: nadie trabaja en ellas.
$PG -tAc "SELECT count(*) FROM archivos WHERE worker_id IS NOT NULL AND lease_hasta > now()"
$PG -tAc "SELECT count(*) FROM pg_stat_activity WHERE application_name LIKE 'pipeline-%'"
```

Si a los 15 min sigue habiendo lotes, puedes parar igualmente, pero el trabajo de esos
lotes se pierde: sus filas conservan el lease hasta que vence (300 s). Las que estaban
EN_PROCESO vuelven a PRECALIFICADO con un intento más cuando arranque el siguiente
worker (`recuperar_huerfanos`).

Después, para por nombre lo que siga vivo y escriba en la cola: los contenedores de
corrida (`norm-*`) y el vigilante, si alguien lo activó. El vigilante vive en el profile
`vigilante`, que `$C` no activa, así que `$C stop vigilante` no lo encontraría:

```bash
ESCRITORES=$(docker ps --format '{{.Names}}' | grep -E '^norm-|^normalizacion-vigilante-1$')
echo "$ESCRITORES" | tee /root/escritores_parados.txt   # vacío si todo acabó con la pausa
[ -z "$ESCRITORES" ] || docker stop $ESCRITORES; echo "codigo: $?"   # 0
docker ps --format '{{.Names}}\t{{.Status}}'   # léela ENTERA: ni norm-* ni vigilante
```

Y ahora la comprobación que manda: **nadie escribe en `archivos`**. Es la misma huella
que usa el exportador: los contadores de INSERT, UPDATE y DELETE de la tabla, leídos de
las estadísticas (no tocan la tabla). Se lee dos veces con 60 s de separación y tiene
que salir **igual**. Una corrida la mueve sin parar (el 25-09, `n_tup_upd` subió 289 en
unos segundos). Las estadísticas se publican con hasta ~10 s de retraso: por eso 60 s y
no 5.

```bash
H="SELECT n_tup_ins, n_tup_upd, n_tup_del FROM pg_stat_user_tables WHERE relid = 'archivos'::regclass"
$PG -tAc "$H"; sleep 60; $PG -tAc "$H"   # las dos líneas IGUALES

# Quién sigue haciendo algo (tú aparte). Lo normal: nada, o una consulta suelta del
# exportador o del panel (application_name vacío, pocos segundos de antigüedad).
$PG -tAc "SELECT pid, application_name, client_addr, state, now() - xact_start, left(query, 50)
          FROM pg_stat_activity WHERE datname = current_database()
            AND backend_type = 'client backend' AND state <> 'idle' AND pid <> pg_backend_pid()"
```

Si la huella cambia, alguien escribe: **no sigas**. Búscalo en la lista de arriba. Para
saber qué contenedor es una IP de `client_addr`, usa `docker network inspect
normalizacion_interna`. Con un escritor vivo, el paso 4 haría fallar sus UPDATE a mitad
de lote, y el `diff` del paso 7 saldría distinto sin que se sepa por qué.

### 3. Foto de antes (con la cola ya quieta: la huella del paso 2 no se movió)

```bash
$PG -tAc "SELECT pg_size_pretty(pg_relation_size('archivos')), pg_size_pretty(pg_indexes_size('archivos')),
                 pg_size_pretty(pg_total_relation_size('archivos'))"
# Conteo por disco y estado. Va por index-only scan de ix_archivos_disco_estado (181 MB): se puede hacer en la ventana.
$PG -tAc "SELECT disco_id, estado, count(*) FROM archivos GROUP BY 1, 2 ORDER BY 1, 2" > /root/archivos_antes.txt
wc -l /root/archivos_antes.txt
```

### 4. Proteger la API

Las sesiones NUEVAS del rol `norm` (el panel, el exportador y la cola abren una
conexión por petición) fallarán al segundo de esperar un lock en lugar de acumular
hilos. El panel no cargará sus datos durante la ventana, y eso es lo esperado. Las
conexiones ya abiertas no se ven afectadas. El exportador seguirá publicando la pausa
y los discos; `norm_exportador_ultima_ok_timestamp{nivel="backlog"}` dejará de avanzar
hasta el final de la ventana, y eso también es lo esperado.

```bash
$PG -c "ALTER ROLE norm IN DATABASE normalizacion SET lock_timeout = '1s'"
```

**Esto se deshace en el paso 8, pase lo que pase**, también si abortas.

### 5. Lanzar el VACUUM FULL (en segundo plano, sobrevive a que se caiga el SSH)

```bash
cat > /root/compactar_archivos.sql <<'SQL'
\timing on
-- No ponerse a la cola: mientras VACUUM FULL espera su lock, TODAS las consultas nuevas
-- sobre archivos esperan detrás de él. Si no lo consigue en 10 s, falla sin tocar nada.
SET lock_timeout = '10s';
-- Techo duro: si se pasa, se cancela y la tabla queda como estaba.
SET statement_timeout = '45min';
VACUUM (FULL, VERBOSE) archivos;
RESET statement_timeout;
-- 0 y no RESET: RESET volvería al 1 s que el paso 4 puso al rol. Un VACUUM normal que
-- espera no bloquea a nadie, y si un autovacuum le estorba, Postgres cancela el
-- autovacuum al segundo.
SET lock_timeout = 0;
-- VACUUM FULL deja el mapa de visibilidad sin marcar. Sin esta pasada, el backlog del
-- exportador (index-only scan) tendría que ir al heap en cada fila.
VACUUM (ANALYZE, VERBOSE) archivos;
SQL
nohup sh -c "docker exec -i normalizacion-postgres-1 psql -U norm -d normalizacion -v ON_ERROR_STOP=1 \
  < /root/compactar_archivos.sql" > /root/compactar_archivos.log 2>&1 &
```

Si el log dice `canceling statement due to lock timeout`, alguien tenía la tabla
ocupada. Vuelve al paso 1 y busca quién era: no se tocó nada.

### 6. Seguimiento y decisión de abortar

```bash
$PG -tAc "SELECT phase, heap_blks_scanned, heap_blks_total,
                 round(100.0 * heap_blks_scanned / nullif(heap_blks_total, 0), 1), index_rebuild_count
          FROM pg_stat_progress_cluster WHERE relid = 'archivos'::regclass"
```

Repítelo al minuto. Con `heap_blks_total` = 1.459.519, las páginas avanzadas por
minuto te dan el tiempo que falta de la fase `seq scanning heap`. Después vienen
`swapping relation files`, `rebuilding index` (hasta `index_rebuild_count` = 8) y
`performing final cleanup`, que sobre la tabla ya compacta deberían tardar pocos
minutos (estimación). **Si el total estimado se sale de la ventana, aborta.**

### Abortar

```bash
$PG -tAc "SELECT pg_cancel_backend(pid) FROM pg_stat_progress_cluster WHERE relid = 'archivos'::regclass"
```

La transacción se deshace, los ficheros a medio escribir se borran y `archivos` queda
intacta. Si en un minuto no ha soltado, usa `pg_terminate_backend(pid)` con el mismo
filtro. **No reinicies el contenedor de Postgres ni mates el proceso con kill -9**: una
caída a mitad deja ficheros huérfanos que ocupan disco y nadie limpia. Después de
abortar, sigue igualmente con el paso 8.

### 7. Comprobar el resultado

```bash
tail -30 /root/compactar_archivos.log        # sin ERROR; el VERBOSE da páginas e índices
$PG -tAc "SELECT pg_size_pretty(pg_relation_size('archivos')), pg_size_pretty(pg_indexes_size('archivos')),
                 pg_size_pretty(pg_total_relation_size('archivos'))"
$PG -tAc "SELECT i.indexrelid::regclass, pg_size_pretty(pg_relation_size(i.indexrelid))
          FROM pg_index i WHERE i.indrelid = 'archivos'::regclass ORDER BY pg_relation_size(i.indexrelid) DESC"
$PG -tAc "SELECT relpages, relallvisible, reltuples::bigint FROM pg_class WHERE relname = 'archivos'"
$PG -tAc "SELECT disco_id, estado, count(*) FROM archivos GROUP BY 1, 2 ORDER BY 1, 2" > /root/archivos_despues.txt
diff /root/archivos_antes.txt /root/archivos_despues.txt && echo "MISMAS FILAS"
# La consulta que antes leía 11 GB (ahora es barata de medir):
$PG -c "EXPLAIN (ANALYZE, BUFFERS) SELECT COALESCE(ruta_decision, 'SIN_DECIDIR'), COUNT(*), COALESCE(SUM(tamano), 0) FROM archivos GROUP BY 1"
```

Qué esperar (estimación a partir del tamaño medio de fila, 857 B):
- heap de ~0,75 GB, con ~95.000 páginas en vez de 1,46 M;
- índices de ~0,3–0,6 GB en total;
- `relallvisible` ≈ `relpages`;
- el `diff` vacío;
- la consulta por ruta en décimas de segundo en vez de 2,7 s.

Si el heap queda por encima de 2 GB, algo no cuadra: compara `reltuples` con la foto
de antes.

### 8. Devolver todo a su sitio (SIEMPRE, también tras abortar)

```bash
$PG -c "ALTER ROLE norm IN DATABASE normalizacion RESET lock_timeout"
$PG -tAc "SELECT r.rolname, s.setconfig FROM pg_db_role_setting s JOIN pg_roles r ON r.oid = s.setrole"   # sin lock_timeout
# Reanudar SOLO si no estaba pausado antes de la ventana (paso 1): si alguien lo había
# pausado por otro motivo, se queda como estaba.
if [ "$(cat /root/pausa_antes.txt)" = "false" ]; then
  $C exec -T api norm reanudar
else
  echo "estaba pausado antes de la ventana: se queda pausado"
fi
$PG -tAc "SELECT coalesce((SELECT valor FROM control WHERE clave = 'pausado'), 'false')"   # igual que /root/pausa_antes.txt
docker exec normalizacion-api-1 python -c "import urllib.request as u; print(u.urlopen('http://localhost:8000/salud').status)"   # 200
```

Abre el panel y comprueba que carga. Luego vuelve a lanzar, uno a uno, lo que paraste
en el paso 2 (`/root/escritores_parados.txt`). Ojo con una corrida parada a medias: su
fila en `corridas` sigue EN_CURSO, y `norm pipeline` se niega a arrancar otra («ya hay
una corrida en curso») hasta que la API reinicia y la marca FALLIDA. Relánzala después
del paso 9, que reinicia `api`; retoma donde quedó.

### 9. Después: la migración 0013 y el seguimiento

`alembic/versions/0013_autovacuum_archivos.py` ajusta los umbrales de autovacuum de
`archivos`. Se aplica al arrancar `api` con una imagen que la incluya, porque su
`command` ejecuta `alembic upgrade head` antes de `norm api`.

**No reconstruyas ni reinicies `api` mientras haya un VACUUM, ANALYZE o REINDEX sobre
`archivos`.** El ALTER de la 0013 los espera (no bloquea lecturas ni escrituras, pero sí
choca con cualquier otro mantenimiento de la tabla). Si no consigue el lock en 30 s,
falla con `canceling statement due to lock timeout`, la API no arranca y Docker la
reintenta en bucle. Mientras tanto `/buscar` está caído. Contra un autovacuum no espera:
lo cancela al segundo, y esa pasada se pierde entera. Por eso va aquí, con la tabla
recién compactada. Antes de lanzarla:

```bash
tail -5 /root/compactar_archivos.log   # el VACUUM (ANALYZE) del paso 5 ya terminó
$PG -tAc "SELECT a.pid, a.backend_type, l.mode, left(a.query, 60)
          FROM pg_locks l JOIN pg_stat_activity a USING (pid)
          WHERE l.relation = 'archivos'::regclass
            AND l.mode IN ('ShareUpdateExclusiveLock', 'ShareLock', 'ShareRowExclusiveLock',
                           'ExclusiveLock', 'AccessExclusiveLock')"   # vacío
$PG -tAc "SELECT pid, phase, heap_blks_scanned, heap_blks_total
          FROM pg_stat_progress_vacuum WHERE relid = 'archivos'::regclass"   # vacío
$PG -tAc "SELECT pid, phase FROM pg_stat_progress_create_index WHERE relid = 'archivos'::regclass"   # vacío
```

Si sale un autovacuum, sobre la tabla ya compacta debería durar poco: espera a que
acabe en vez de tirarlo. Luego:

```bash
$C build api && $C up -d --no-deps api
docker logs --since 5m normalizacion-api-1 2>&1 | tail -40   # sin traceback ni «lock timeout»
$PG -tAc "SELECT version_num FROM alembic_version"                    # 0013
$PG -tAc "SELECT reloptions FROM pg_class WHERE relname = 'archivos'" # los 4 autovacuum_*
```

El log de `api` no dice nada cuando la migración va bien: `alembic/env.py` no configura
el logging de alembic y solo salen los errores. La prueba son las dos consultas de
datos.

Con la 0013 ya aplicada, `alembic upgrade head` no toca `archivos` en los arranques
siguientes y la advertencia de arriba deja de aplicarse (hasta otra migración que la
toque).

En los días siguientes, el log de Postgres solo registra las pasadas de autovacuum de
más de 10 min (`log_autovacuum_min_duration`). Si la compactación funcionó, dejan de
aparecer líneas `automatic vacuum of table "normalizacion.public.archivos"`. Cuéntalas
así: `docker logs --since 24h normalizacion-postgres-1 2>&1 | grep -c 'automatic vacuum
of table "normalizacion.public.archivos"'` (0 es lo esperado; `grep -c` sale con código 1
cuando cuenta 0, y eso no es un error).

## Plan B: sin ventana, solo los índices (REINDEX CONCURRENTLY)

Reduce los dos índices grandes (7,2 GB) sin bloquear lecturas ni escrituras. Así cada
pasada de autovacuum bajaría de ~1 M a ~0,2 M páginas leídas (estimación). El heap
sigue en 11 GB y las lecturas completas (exportador, panel) siguen igual de caras.

Coste: cada índice recorre DOS veces el heap entero. Son 4 pasadas de 11 GB, hasta
44 GB leídos si el heap no está en caché. Hazlo también con el disco tranquilo y fuera
de la ventana del respaldo. Además, espera a que acabe toda transacción que ya
estuviera abierta.

**Mientras corre, no reconstruyas ni reinicies `api` si la 0013 no está aplicada**
(`SELECT version_num FROM alembic_version` da `0012`). Su ALTER esperaría al REINDEX, que
puede durar horas. A los 30 s fallaría y la API se quedaría reintentando en bucle, con
`/buscar` caído, hasta que acabara el REINDEX.

```bash
$PG -tAc "SELECT version_num FROM alembic_version"   # 0013, o no toques api hasta que acabe
$PG -tAc "SELECT pid, now() - xact_start FROM pg_stat_activity
          WHERE datname = current_database() AND xact_start < now() - interval '1 minute'"   # vacío
nohup sh -c "docker exec -i normalizacion-postgres-1 psql -U norm -d normalizacion -v ON_ERROR_STOP=1 \
  -c 'REINDEX INDEX CONCURRENTLY ix_archivos_claim' -c 'REINDEX INDEX CONCURRENTLY archivos_pkey'" \
  > /root/reindex_archivos.log 2>&1 &
$PG -tAc "SELECT phase, blocks_done, blocks_total, index_relid::regclass FROM pg_stat_progress_create_index"
```

No le pongas `lock_timeout`: sus esperas a transacciones viejas también son esperas de
lock, y cortarlas a medias deja un índice inválido.

**Abortar:** `pg_cancel_backend` sobre el pid que muestra `pg_stat_progress_create_index`.
Si se cancela a medias, queda un índice inválido con sufijo `_ccnew` que hay que quitar
a mano. El índice original sigue sirviendo mientras tanto:

```bash
$PG -tAc "SELECT indexrelid::regclass FROM pg_index WHERE indrelid = 'archivos'::regclass AND NOT indisvalid"
$PG -c "DROP INDEX CONCURRENTLY ix_archivos_claim_ccnew"   # el nombre que haya salido arriba
```

## Cuándo repetir

Después de cualquier DELETE masivo en `archivos` (quitar un disco grande), o cuando el
heap supere unas 3 veces lo que ocupan las filas vivas:

```bash
$PG -tAc "SELECT pg_size_pretty(pg_relation_size('archivos')),
                 pg_size_pretty((c.reltuples * 857)::bigint) FROM pg_class c WHERE relname = 'archivos'"
```

Los umbrales de autovacuum no evitan esto. Una tabla que creció hasta 28,8 M filas
conserva ese tamaño en disco aunque luego se vacíe.
