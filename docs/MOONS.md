# MOONS — lunas de Azazel: nodos que normalizan sin quedarse el dato

**Estado:** en producción en `vps-storage-01` (205.209.102.101) desde 2026-09-07
**Rama:** `Moons` · **Matriz:** `main`

**Una frase:** una **luna** es un Azazel que normaliza documentos que **no le pertenecen**,
extrae el conocimiento y se lo manda al **planeta** (la matriz), conservando los originales
donde estaban.

---

## 1. La metáfora, y por qué no es solo decorativa

| | Planeta (matriz) | Luna |
|---|---|---|
| Nodo | `vps-01` — 162.35.188.181 | `vps-storage-01` — 205.209.102.101 |
| Perfil | `online` | `hibrido-ingesta` |
| Resuelve entidades | **Sí** | No |
| Sirve a otros sistemas | Sí | No |
| Guarda copia de los blobs | Sí (MinIO) | **No** |
| Los originales | Se pueden desechar | **Se conservan siempre** |

El planeta es el único que tiene el conocimiento completo y el único que resuelve
identidades. Las lunas orbitan: hacen el trabajo pesado de masticar corpus grandes y
empujan el resultado hacia dentro. Se pueden añadir y quitar lunas sin tocar el planeta.

---

## 2. La diferencia que lo cambia todo: el almacén

Azazel nació para **discos desechables**. El flujo canónico es:

```
disco físico → normalizar → copiar el blob al almacén → puerta verde → TIRAR el disco
```

La copia existe **para poder desechar el original**. El almacén es la copia permanente
y por eso `reclamacion.py` puede vaciar la carpeta de origen cuando la puerta da verde.

**En una luna, el original no se tira.** Es un corpus que vive ahí y que sigue vivo
después. Copiarlo al almacén sería duplicar el corpus entero en el mismo disco sin
comprar nada: en `vps-storage-01`, 13 TB de datos con 5,5 TB libres — **no cabe**.

De ahí el backend `almacen_backend = "ninguno"`.

---

## 3. Los cuatro cambios de esta rama

### 3.1 `AlmacenNulo` — el almacén que no guarda

`core/almacen/__init__.py` · fábrica en `crear_almacen`, y el frío en
`ingesta/workers/verificador.py:crear_almacen_frio`.

- `existe()` → siempre `False`, `guardar()` → no-op. El worker sigue leyendo una vez,
  hasheando al vuelo y extrayendo: **el documento que va al índice es idéntico**.
- `leer()` **lanza** `FileNotFoundError`. Con ello se pierden, en este nodo:
  `/archivo/{id}/contenido`, `reextraccion.py` y el conjunto de calidad. El archivo
  sigue en su carpeta; lo que no existe es una copia direccionable por hash.

**Medido:** tras procesar 4,6 GB, los buckets `almacen` y `frio` seguían en 4,0 K.

### 3.2 La verificación se salta lo que no puede cotejar

`verificador.py:verificar_indexados`.

La verificación relee el blob para cazar corrupción silenciosa (R1). Sin blob no hay
nada que cotejar — y dejar la fila en `INDEXADO` la hacía **reintentar en cada corrida
hasta morir en ERROR**: trabajo infinito y un panel que miente. Ahora, con almacén nulo,
la fila cierra a `HECHO` directamente.

La garantía no se relaja: **se traslada a la puerta**.

### 3.3 La puerta, fail-closed — el cerrojo que importa

`verificador.py:evaluar_puerta`.

```
si almacen_backend == "ninguno"  →  seguro_para_desechar = False
                                     motivo = "sin_copia_el_origen_es_la_unica"
```

Sin esto, una luna con todas las filas en `HECHO` daría **puerta verde**, y
`reclamacion.py` haría `rmtree` + `unlink` sobre el contenido de la carpeta de origen
— que en una luna es la **única copia que existe**.

Que todas las filas estén `HECHO` significa *"extraído e indexado"*, no *"a salvo"*:
el índice guarda texto y metadatos, no los bytes del original.

> **Verificado:** con `total=2, hechos=2, pendientes=0` la puerta devuelve
> `seguro_para_desechar: False`. Es el caso que antes habría autorizado el borrado.

Segunda capa de defensa, independiente: **los montajes de datos van `:ro`**.

### 3.4 `disco_id` derivado de la ruta, no del nombre

`core/despliegue.py:disco_id_desde_raiz` · usado en `ingesta/pipeline.py:iniciar_corrida`.

Fuera de `local`, el `disco_id` era obligatorio (P1 de `PLAN_TOPOLOGIA.md`): derivarlo
del **basename** hace que dos carpetas homónimas colisionen y sus `archivo_id` con ellas.

Pero la **ruta relativa a la raíz fija** (`api_carpeta_raiz`, aquí `/datos`) sí es única
por construcción del filesystem, y entre nodos la desambigua el prefijo `nodo_id:`.
Así el operador **solo elige carpeta** y no se inventa un identificador que, escrito
distinto en dos corridas, duplicaría el disco entero.

Fuera de la raíz sigue siendo obligatorio: allí no hay unicidad garantizada.

---

## 4. El transporte hacia el planeta

`norm replicar` existe y funciona (`core/replicacion.py`), pero el canal hay que
montarlo. En `vps-storage-01` vive en `/srv/azazel/replicar_a_matriz.sh`, en cron
cada 15 min, con log en `/srv/azazel/replica.log`:

```
0. flush del índice      ← sin esto el snapshot deja fuera lo más reciente
1. norm replicar         ← snapshot de archivos-vps-storage-01-*
1b. purga                ← conserva los 3 últimos snapshots
2. export del bucket     ← mc mirror a /srv/azazel/_export_snapshots
3. rsync                 → matriz:/srv/azazel/_import_snapshots
4. inyectar + restaurar  ← mc efímero en la red interna del planeta, sin exponer puertos
   → restore + backfill de entidades
```

### La trampa que costó 22 documentos

**`restaurar_ajenos` restauraba el snapshot MÁS ANTIGUO.** Recorría los snapshots
ordenados **ascendente** y daba el índice por restaurado en el primero que lo contenía.
Con un emisor que fotografía su índice cada ciclo, eso significa restaurar siempre el
más viejo: **los datos nuevos no llegan jamás, y sin error** — que es lo peor.

> Síntoma real: la luna tenía 24 documentos y el planeta recibía 2.

**Arreglado en el código** (`replicacion.py`): de cada índice ajeno se elige el snapshot
más reciente, y los snapshots no-`SUCCESS` se descartan (uno fallido y reciente
secuestraría el restore). Cubierto por `tests/unit/test_moons.py`.

Además, un índice ya presente se salta salvo `refrescar=True` (`norm replicar
--refrescar`): un restore sobre un índice abierto falla y retirarlo es destructivo, así
que la replicación periódica lo pide explícitamente y nunca ocurre por sorpresa.

> **Sobre el `flush` del paso 0:** el script hace `flush` antes del snapshot porque
> OpenSearch fotografía los segmentos en disco. Es **defensivo**: durante el diagnóstico
> se sospechó que el translog era la causa de los documentos perdidos y **no lo era** —
> añadirlo no cambió nada; lo que faltaba era lo de arriba. Se conserva por barato, no
> porque esté demostrado que haga falta.

### Por qué el planeta NO cambia de perfil

Para *recibir*, `replicar()` exige `es_archivo_maestro=False`, o sea `hibrido-servicio`.
Pero eso le quitaría al planeta su condición de archivo maestro y con ella la
reclamación de espacio de la que depende.

En lugar de eso, el script llama directamente a `replicacion.restaurar_ajenos(config)`.
El planeta sigue en `online`, intacto, y aun así recibe. **Ninguna luna obliga a tocar
el planeta.**

---

## 5. Desplegar una luna nueva

1. **Perfil y almacén** en `.env.prod`:
   ```
   NORM_DESPLIEGUE__PERFIL=hibrido-ingesta
   NORM_DESPLIEGUE__NODO_ID=<único: vps-storage-01, vps-luna-02…>
   ```
   y en el compose override del servicio `api`:
   ```yaml
   environment:
     NORM_ALMACEN_BACKEND: "ninguno"
   ```

2. **Montar los datos en `:ro`** bajo `/datos/<nombre>`. Read-only no es cosmético:
   es la segunda capa que impide un borrado accidental. Los mountpoints deben existir
   antes (`/datos` va montado `:ro` y Docker no puede crearlos dentro).

3. **Buckets de MinIO**: `almacen`, `frio`, `snapshots` deben existir o el repositorio
   de snapshots falla con `path is not accessible on cluster-manager node`.

4. **Llave SSH propia** de la luna autorizada en el planeta (nunca al revés: el planeta
   no necesita entrar en la luna).

5. **Cron** con el script de replicación.

### ⚠ El compose vive tras `profiles:` y exige `--env-file`

Dos trampas que juntas convierten un despliegue de dos minutos en media hora:

- Los servicios están **detrás de perfiles**: `postgres`, `opensearch` y `minio` en
  `datos`; `api`, `exportador` y `front` en `app`. Sin `--profile` compose no ve
  **ninguno** y responde `no such service: minio` — un mensaje que hace pensar en un
  fichero mal escrito cuando el fichero está perfecto. Peor: `config --services`
  devuelve **lista vacía con código de salida 0**, así que parece que todo va bien.
- Hace falta `--env-file ../.env.prod`. Sin él, la interpolación muere con
  `required variable NORM_MINIO_ROOT_USER is missing a value`.

La invocación que funciona, desde `deploy/`:

```bash
docker-compose -f docker-compose.prod.yml -f ../docker-compose.override.yml \
  --env-file ../.env.prod --profile app --profile datos -p normalizacion \
  build api    # y luego: up -d --no-deps --force-recreate api
```

> **Los dos ficheros, siempre.** El override es donde vive
> `NORM_ALMACEN_BACKEND: "ninguno"`. Recrear `api` solo con `docker-compose.prod.yml`
> deja la luna con almacén real: empezaría a copiar blobs y la puerta daría verde
> sobre la ÚNICA copia que existe. Tras recrear, compruébalo:
> `docker exec normalizacion-api-1 env | grep ALMACEN`.

### ⚠ Docker se salta el firewall

En `vps-storage-01`, `ufw` permite sólo 22, 80, 443 y 68 — y sin embargo **3000 y 8000
respondían desde internet**. No es un fallo de ufw: Docker escribe sus propias reglas en
la cadena `DOCKER` de iptables, que se evalúa **antes** que las de ufw. Todo puerto
publicado con `ports:` queda expuesto aunque el firewall diga lo contrario.

Consecuencias para una luna, que ingiere material sensible:

- Publicar sólo lo imprescindible. Los servicios de datos van a `127.0.0.1:` en el
  compose (Postgres, OpenSearch, MinIO), y eso sí los mantiene fuera de internet.
- El puerto **8000 del API sigue expuesto en claro**; el acceso bueno es el 8443 con TLS.
  Cerrarlo requiere quitar su `ports:` o filtrarlo en la cadena `DOCKER-USER` — ufw no basta.
- Un puerto que nginx abre (como el 8443) **sí** obedece a ufw y hay que autorizarlo
  explícitamente: si no, `nginx -t` valida, el proceso escucha y desde fuera no responde nada.

### Techos de recursos

Una luna suele compartir máquina con otras cosas. Sin `cpus:` los workers se llevan
todos los núcleos. En `vps-storage-01` (10 cores, 39 GB, compartido con tres sitios PHP):

| Contenedor | CPU | RAM |
|---|---|---|
| api (workers) | 3.5 | 6 GB |
| opensearch | 2.5 | 6 GB (heap 3g) |
| postgres | 1.5 | 4 GB |
| minio | 1.0 | 2 GB |
| exportador / front | 0.5 / 0.5 | 512 M / 256 M |

Gobernador en `adaptativo` + `balanceado` con **`workers_max` explícito**: en adaptativo
el gobernador ignora `NORM_WORKER__PROCESOS` y dimensiona por RAM libre — en esta máquina
subió solo a 8 workers para 3,5 cores.

> Si toca `mem_limit`, toca también `memswap_limit`. El compose base los fija iguales a
> propósito (sin swap: el cgroup mata al proceso que se pasa y el box sigue vivo).

---

## 6. Lo que falta

| Qué | Por qué importa |
|---|---|
| **Medir con volumen grande** | `PT2` son 115.358 entradas sobre HDD SATA (~150 IOPS). El cuello será el **I/O**, y los techos de CPU no protegen de eso: Postgres y MySQL pelean por el mismo disco |
| **`indexado_en` en el mapping** | Haría el backfill incremental por tiempo en vez de rescan completo por hash (§3.5 de `PLAN_TOPOLOGIA.md`). Hoy cada réplica dispara un barrido entero del índice |
| **El front no sabe que es una luna** | `/archivo/{id}/contenido` falla con `FileNotFoundError` y la UI lo pinta como error genérico, cuando la respuesta correcta es "este nodo no guarda copia" |
| **Migrar el script de transporte al repo** | `replicar_a_matriz.sh` vive sólo en `/srv/azazel` del nodo. Debería versionarse en `deploy/` |
| **`centinela.sh` no está versionado** | Vive sólo en el nodo (`/srv/azazel/` o `/opt/azazel-luna/`) y las tres copias divergen. Ya lleva lógica que costó un incidente —el freno— y no hay forma de revisarla, ni de saber si los tres nodos tienen la misma. Debería estar en `deploy/` con lo específico de cada nodo en variables |
| **La caché de extracción es por PROCESO** | Con 4 workers, un 7z de 130 GB puede llegar a ocupar ~520 GB en `/tmp` a la vez. Cabe en este nodo (5,4 TB libres) y no en uno más chico. Una caché compartida entre workers, o un tope que cuente el total y no lo de cada proceso, lo acotaría |

### El 7z que paró la luna 16 horas (BCJ2)

**Síntoma:** `vps-storage-01` con una corrida `EN_CURSO` que no procesaba **ni un solo
archivo**. Un núcleo al 100 %, los cuatro workers con 24 s de CPU cada uno, 104 GB
escritos y `/tmp` oscilando entre 1,2 y 2 GB porque el directorio se recreaba cada 80 s.
La corrida anterior estuvo así 13 horas y llenó el disco al 99 %.

**Causa:** `PROGRAMA + DB PREPARATORIA.7z` usa el filtro **BCJ2**, que `py7zr` no
implementa (`method_names=['LZMA2', 'LZMA', 'BCJ2*']`). El bucle era:

```
extraer 2 GB → reventar en la entrada BCJ2 → rmtree de lo extraído
   → el fallo viaja como OSError → el precalificador lo trata como TRANSITORIO
   → reintentar → extraer 2 GB otra vez → ...
```

Con 37.623 entradas dentro de ese archivo y `intentos_max=3`, eran ~39 días de trabajo
para no indexar nada. Y **sin un solo error visible**: `errores: 0` en el panel, porque
un transitorio en reintento no cuenta como error.

**Arreglo, en dos mitades — las dos hacen falta:**

1. `_dir_7z_extraido` cae a **`unar`** cuando py7zr no sabe el codec. `unar` ya era la
   vía para los RAR y sí soporta BCJ2: extrajo 15.103 ficheros del mismo archivo.
   Va con `-no-directory`, porque por defecto unar añade una carpeta contenedora
   cuando hay varias entradas en la raíz y entonces `dir_ex / entrada` no resuelve
   **ninguna** — el mismo síntoma que se venía de curar.
2. Cuando **ningún** descompresor lo abre, se lanza `ContenedorIlegible`, que **no
   es un `OSError`**. Esa es la línea que rompe el bucle: el precalificador manda los
   `OSError` a reintentos, y aquí reintentar es re-descomprimir el archivo entero para
   fallar exactamente igual.

> **La lección general:** un codec no soportado es PERMANENTE. Clasificarlo como
> transitorio no cuesta un reintento barato — cuesta re-descomprimir gigabytes, y el
> panel dice que todo va bien mientras el nodo no avanza.

Cubierto por `tests/unit/test_contenedores.py::TestSieteZCodecNoSoportado` (4 casos,
verificados quitando el arreglo: los 4 fallan sin él).

### El freno que frenaba a través de lo que se rompe

El centinela pausa la ingesta al 93 % de disco. Lo hacía así:

```bash
if docker exec normalizacion-api-1 norm pausar >/dev/null 2>&1; then
  alerta "FRENO ACTIVADO: ..."
fi          # ← sin `else`: si el exec falla, silencio absoluto
```

Dos fallos en tres líneas. El freno **atravesaba el contenedor del API**, que es
justo el que se cuelga cuando hay problemas; y el `if` sin `else` se tragaba el
fallo. Resultado medido en `vps-storage-01`: el disco llegó al **99 %** con la
ingesta corriendo y en `centinela-alertas.log` **no hay una sola línea `FRENO`**.
El freno no existía y nadie podía saberlo.

Ahora la pausa se escribe **directa en Postgres** (`control.pausado = 'true'`, que es
lo que `norm pausar` hace por dentro), y un freno fallido **genera su propia alerta**.

> Verificado con las tres ramas, en el nodo y sin tocar la bandera real (clave de
> juguete): con Postgres accesible activa y el `true` llega a la tabla; con Postgres
> inalcanzable avisa `FRENO FALLO … LA INGESTA SIGUE CORRIENDO`; y el código viejo,
> ante el mismo fallo, no escribe **nada**.

Aplicado en los tres nodos. **Un freno que falla en silencio es peor que no tener
freno**, porque das por hecho que está puesto.

### Ya resuelto

- ~~El freno del centinela no saltaba y no avisaba de que no saltaba~~ → pausa
  directa en Postgres + alerta cuando el freno falla (arriba).
- ~~Un 7z con BCJ2 dejaba la luna en bucle infinito~~ → fallback a `unar` +
  `ContenedorIlegible` como fallo permanente (arriba).
- ~~`restaurar_ajenos` restauraba el snapshot más antiguo~~ → arreglado en `replicacion.py`,
  con `--refrescar` para la replicación periódica.
- ~~Sin tests~~ → `tests/unit/test_moons.py`, 19 casos. Verificados **quitando cada arreglo**:
  sin el cerrojo de la puerta y sin la elección del snapshot reciente fallan exactamente los
  4 tests que deben fallar, y los otros 15 siguen pasando.
- ~~TLS~~ → nginx termina TLS en **:8443** con certificado propio (SAN por IP y por
  sslip.io) y hace proxy al front. La cookie de sesión **vuelve a llevar `Secure`**, que
  era el arreglo de verdad: antes viajaba en claro. Comprobado que por HTTP plano el
  cliente ya no guarda la sesión.
- ~~Secretos~~ → rotados el usuario del panel, la API key, Postgres y MinIO, con prueba
  diferencial (lo viejo da 401, lo nuevo 200) y el ciclo de réplica funcionando después.

**La contraseña de admin de OpenSearch NO se rotó, a propósito.**
`OPENSEARCH_INITIAL_ADMIN_PASSWORD` sólo actúa en la **primera** inicialización del
clúster: cambiarla en el entorno no cambia la del nodo, deja al API autenticando con una
que ya no existe y tumba el índice entero. Rotarla de verdad exige regenerar el hash en
`internal_users.yml` y pasar `securityadmin.sh`. Escucha sólo en `127.0.0.1`, así que el
riesgo de dejarla no compensa el de romper el nodo por sorpresa.

---

## 7. Modelo de ramas

| Rama | Qué es |
|---|---|
| **`main`** | **El planeta.** Azazel matriz: resuelve entidades, sirve a los demás sistemas, es el archivo maestro. Toda rama vuelve aquí o muere |
| **`Moons`** | **Las lunas.** Nodos de ingesta que normalizan corpus ajenos sin quedarse los blobs y empujan el conocimiento al planeta. Esta rama |
| `feat/topologia-hibrida` | ⚙K16: perfiles, capacidades y `disco_id` por nodo. El cimiento sobre el que `Moons` es posible |
| `feat/perfil-online-vigilante` | Perfil `online` + vigilante de carpeta + reclamación de espacio. Base de la que sale `Moons` |

**La regla:** una rama se nombra por **lo que el nodo ES**, no por el ticket que la abrió.
Si mañana hay un nodo que solo sirve búsquedas y no ingiere nada, será otro cuerpo del
sistema con su propia rama — no un `feat/` suelto.
