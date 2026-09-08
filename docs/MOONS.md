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

### Dos trampas que el canal tiene que sortear

**a) El snapshot no ve el translog.** OpenSearch fotografía los segmentos **en disco**.
Lo recién indexado vive en el translog y **no entra en el snapshot**. Sin un `flush`
previo, cada ciclo deja fuera lo más nuevo, en silencio.

**b) `restaurar_ajenos` restaura el snapshot MÁS ANTIGUO.**
`replicacion.py:218` recorre los snapshots ordenados **ascendente** y da el índice por
restaurado en el primero que lo contiene. Con snapshots repetidos del mismo nodo, eso
significa restaurar siempre el más viejo: **los datos nuevos no llegan jamás**.

> Síntoma real: la luna tenía 24 documentos y el planeta recibía 2.

El script lo sortea eligiendo explícitamente el snapshot más reciente de cada índice y
borrando la copia vieja antes de restaurar (un restore sobre un índice abierto falla).
**El bug sigue en el código**: quien llame a `norm replicar` en un nodo receptor lo
arrastra. Arreglarlo en `replicacion.py` está pendiente (§6).

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
| **Arreglar `restaurar_ajenos` en el código** (§4b) | Hoy el arreglo vive en un script. `norm replicar` sigue roto para cualquier receptor |
| **Tests** de `AlmacenNulo`, puerta fail-closed y `disco_id_desde_raiz` | Los tres cambios se validaron a mano contra el nodo real, no en CI |
| **Medir con volumen grande** | `PT2` son 115.358 entradas sobre HDD SATA (~150 IOPS). El cuello será el **I/O**, y los techos de CPU no protegen de eso |
| **TLS en la luna** | El panel va por HTTP plano con la cookie sin `Secure`. Caddy está parado (peleaba por el :80 con nginx) |
| **Rotar secretos** | Los de `vps-storage-01` (MinIO, Postgres, OpenSearch) son provisionales |
| **`indexado_en` en el mapping** | Haría el backfill incremental por tiempo en vez de rescan completo por hash (§3.5 de `PLAN_TOPOLOGIA.md`) |

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
