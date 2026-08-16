# PLAN_TOPOLOGIA.md — Azazel local e híbrido, con la misma base de código

**Estado:** plan definitivo · 2026-08-15
**Base:** lectura completa del backend (`src/normalizacion/**`, migraciones, `deploy/`, tests).
Cada afirmación de este documento cita archivo y línea.

**Una frase:** que Azazel corra **en una máquina** o **repartido entre la Mac y un VPS que también
ingiere**, cambiando una sola perilla, sin `if` de topología esparcidos y sin alterar lo que hoy
funciona.

**Decisiones tomadas con el usuario (2026-08-15):**

| # | Decisión | Valor |
|---|---|---|
| 1 | ¿El VPS ingiere? | **Sí** — fuentes que nacen en la red (dumps, padrones, descargas) |
| 2 | Copia permanente | Cada nodo guarda copia de lo que ingiere. **`mac-01` es el archivo maestro** |
| 3 | Replicación | **Programada desde el inicio** |
| 4 | `nodo_id` | **`mac-01`** y **`vps-01`** |

---

## 1. Cómo funciona el sistema hoy (lo que hay que respetar)

### 1.1 Las tres identidades deterministas

| Identidad | Fórmula | Dónde | Propiedad |
|---|---|---|---|
| `archivo_id` | `sha256(f"{disco_id}:{ruta_rel}\|{tamaño}\|{mtime_ns}")` | `walker.py:51`, `modelo.py:85-93` | Clave de **trabajo**: cola + `_id` del índice |
| `hash_contenido` | `sha256(bytes)` | `orquestador.py:81-87` | Clave del **almacén**: dedup + verificación |
| `entidad_id` | `sha256(f"{tipo}:{ancla_tipo}:{ancla_valor.upper()}")` | `entidades/modelo.py:59-64` | Clave de la **persona resuelta** |

Las tres son **independientes de la máquina**. Es la propiedad de la que cuelga todo el híbrido: dos
nodos que procesan lo mismo producen los mismos ids, y dos nodos que procesan cosas distintas
producen ids disjuntos —siempre que el `disco_id` lo sea (§2.1)—.

Las entradas internas de contenedores heredan el mismo esquema con ruta virtual:
`f"{fila.ruta}!{entrada.ruta_interna}"` (`precalificador.py:55-60`).

### 1.2 El flujo, con sus puntos de acoplamiento

```
catálogo ──▶ precalificación ──▶ worker ──▶ mover-frío ──▶ verificación ──▶ puerta
walker.py    precalificador.py  orquestador  verificador   verificador     verificador
   │              │                  │           │              │              │
   └─ upsert_disco│                  │           └─ montajes ───┘              │
      (punto de   │                  │              (el disco DEBE            │
       montaje)   │                  │               estar montado)           │
                  │                  └─ almacén + Sink → OpenSearch           │
                  └─ re-encola entradas internas (BFS por la cola)     por disco_id
```

Puntos que importan para el reparto:

- El worker lee `montajes` **una sola vez al arrancar** (`orquestador.py:166`): un disco registrado
  a media corrida no se ve hasta el siguiente proceso.
- `mover_frio` exige el punto de montaje vivo (`verificador.py:172-175`). Un disco desconectado
  **no puede cerrar su puerta**.
- El sink confirma antes de transicionar (`orquestador.py:284-287`): *"la cola nunca le miente al
  índice"*. Ese invariante no se toca.
- Solo una corrida a la vez **por base de datos** (`pipeline.py:220-222`). Cada nodo tiene la suya,
  así que en híbrido son dos corridas simultáneas — correcto y deseado.

### 1.3 Config en capas

`defaults` → `.env` → `NORM_*` (anidado con `__`) → `config_overrides` en Postgres, mergeado por
corrida (`config_overrides.py:111-125`). Solo el filtro (K1-K9) y los recursos (K15) son editables
en caliente; la lista de campos permitidos es explícita (`config_overrides.py:29-51`).

---

## 2. Los seis problemas reales

El híbrido no necesita arquitectura nueva. Necesita resolver seis cosas concretas que el código no
contempla porque nunca tuvo que hacerlo. **Dos de ellas ya son problemas hoy, en una sola máquina.**

### P1 · El `disco_id` se deriva de un nombre de carpeta

```python
id_disco = disco_id or raiz.name     # walker.py:67
id_disco = disco_id or ruta.name     # pipeline.py:218
```

Es opcional en el CLI (`cli.py:89-91`, `cli.py:383`) y en la API (`SolicitudPipeline.disco_id`).

**Ya hoy, en una sola máquina:** dos discos desechables llamados `RESPALDO` comparten `disco_id`.
`upsert_disco` **sobrescribe el punto de montaje del primero** (`cola/__init__.py:70-77`),
`actualizar_total_disco` cuenta los dos juntos (`cola:109-117`), y `evaluar_puerta("RESPALDO")`
emite **un veredicto sobre un disco que no existe como unidad física**. En un sistema cuya puerta es
sagrada y sin override, eso importa aunque nunca montes el híbrido.

**En híbrido:** `mac-01` cataloga `/Volumes/DATOS` y `vps-01` cataloga `/datos/DATOS` → mismo
`disco_id` → los `archivo_id` de los dos nodos colisionan.

### P2 · No existe "el almacén" que replicar

```python
def config_con_destino(config, destino):          # pipeline.py:73-89
    return config.model_copy(update={
        "almacen_backend": "local",
        "almacen_local_raiz": str(base / "almacen"),
        "almacen_frio_local_raiz": str(base / "frio"),
    })
```

Cuando el operador elige carpeta destino en el front —el flujo **normal**, documentado en
`INSTALACION.md` y `ARRANQUE.md`— el almacén de esa corrida es una carpeta suelta. Puede haber **N
almacenes**, uno por corrida; por eso existe `destinos_por_disco()` (`pipeline.py:48-70`) y por eso
`crear_almacen_frio` replica la misma lógica (`verificador.py:34-47`).

Cualquier plan que diga "replicamos el bucket de MinIO" es falso contra este código.

### P3 · El índice de escritura es una constante

```python
def indice_escritura(config) -> str:              # opensearch.py:24-26
    return f"{config.indice_alias}-000001"
```

**Para el híbrido:** los dos nodos escribirían en `archivos-000001`. Restaurar el snapshot del otro
**sobrescribe el índice propio**. Es el fallo más caro y el más silencioso: no revienta, sólo faltan
documentos.

**Ya hoy, aparte del híbrido:** la política ISM define `rollover` a 30 GB (`ism/politica_archivos.json`,
y `test_contratos_deploy.py:47-51` verifica que exista), pero el índice se crea con
`{"aliases": {alias: {}}}` **sin `is_write_index`** (`opensearch.py:155-157`) y la plantilla no fija
`index.plugins.index_state_management.rollover_alias` (`deploy/mappings/archivos.json`). Sin esas dos
piezas el rollover no puede promover índice nuevo — y aunque lo hiciera, el sink seguiría escribiendo
en `-000001` porque está fijo en el código. Probablemente no lo has visto porque en la Mac OpenSearch
corre **sin plugin ISM**, caso contemplado como normal en `opensearch.py:146-150`.

Lo bueno: **todas las lecturas ya van por el alias** (`busqueda.py:133,138,165,187,197`;
`backfill.py:244,252`). Un alias multi-índice funciona sin tocar la búsqueda, y el PIT se abre sobre
el alias (`busqueda.py:116-125`).

### P4 · La puerta pertenece a quien tiene el disco

`evaluar_puerta` cuenta `WHERE disco_id = %s` y persiste `discos.seguro_para_desechar`
(`verificador.py:237-268`). `mover_frio` necesita el montaje vivo. El veredicto es **local por
diseño**: ningún nodo puede opinar sobre un disco que no vio.

### P5 · El demonio de envío arranca en toda instancia de la API

```python
from normalizacion.entidades.envio import iniciar_bucle    # main.py:91-93
iniciar_bucle(config)
```

**Incondicional, en `crear_app`.** En híbrido eso significa **dos procesos empujando entidades al
AEB**. Y el cable manda `"modo_merge": "reemplazar"` (`envio.py:230`), es decir last-write-wins: cada
nodo sobrescribiría al otro con su versión parcial de la misma persona.

No corrompe datos —`entidad_id` es determinista y el AEB es idempotente— pero los dos nodos tendrían
conjuntos **incompletos y distintos** (cada uno resolvió sobre su parte del índice) y se pisarían en
bucle. Es la razón concreta, verificable, de por qué las entidades se resuelven en **un solo nodo**.

### P6 · El cursor del backfill es incompatible con la replicación continua

```python
cuerpo = {"size": lote, "sort": [{"archivo_id": "asc"}], ...}   # backfill.py:171-180
if cursor: cuerpo["search_after"] = [cursor]
```

El backfill barre el alias ordenado por `archivo_id` con `search_after`, guardando el cursor en
`control` (`backfill.py:158-168`). Su propio docstring lo admite: *"un escaneo por `archivo_id`
(hash) completa UNA pasada sobre lo ya indexado"* (`backfill.py:20-22`).

**Con replicación esto deja de ser una limitación menor y pasa a ser una pérdida sistemática.**
`archivo_id` es un sha256: se distribuye uniforme. Los documentos que llegan por snapshot restaurado
caen repartidos por todo el espacio de ids, así que **en promedio la mitad de cada lote replicado
aterriza por detrás del cursor y no se escanea nunca**. `search_after` solo avanza.

El remedio existente es `reiniciar=True` (`cli.py:228`, `backfill.py:240-241`): rescan completo,
idempotente pero caro. Hay que decidirlo explícitamente, no descubrirlo.

---

## 3. El diseño

### 3.0 La regla que evita la bifurcación

**El código nunca pregunta el perfil. Pregunta por capacidades.**

```python
# ❌   if config.despliegue.perfil == "hibrido-servicio":
# ✅   if not topologia.corre_entidades:
```

Añadir mañana una tercera topología (Fase 7 de ARQUITECTURA §8: workers en varias máquinas) será
**añadir un perfil**, no tocar los sitios de uso.

```python
class PerillasDespliegue(BaseModel):
    """⚙ K16 — QUÉ es este nodo. Se fija al arrancar; NO editable en caliente."""
    perfil: Literal["local", "hibrido-ingesta", "hibrido-servicio"] = "local"
    nodo_id: str = "local"

@dataclass(frozen=True)
class Topologia:
    corre_ingesta: bool        # catálogo, filtro, workers, frío, verificación
    corre_entidades: bool      # backfill + demonio de envío al AEB   (P5)
    sirve_publico: bool        # API expuesta a internet
    es_archivo_maestro: bool   # aquí converge la copia permanente de todo
    destino_eligible: bool     # ¿el front puede elegir carpeta destino? (P2)
```

| Perfil | ingesta | entidades | público | maestro | destino eligible |
|---|:--:|:--:|:--:|:--:|:--:|
| `local` | ✅ | ✅ | ❌ | ✅ | ✅ |
| `hibrido-ingesta` (`mac-01`) | ✅ | ❌ | ❌ | ✅ | ✅ |
| `hibrido-servicio` (`vps-01`) | ✅ | ✅ | ✅ | ❌ | ❌ |

**`local` enciende todo y no replica nada: es exactamente el sistema de hoy.**

> `destino_eligible` **no se inventa**: ya existe `_destino_eligible(cfg)` en `main.py:284,331`, y el
> front lo consume vía `EstadoPipeline.destino_eligible`. La capacidad se integra ahí.

### 3.1 `disco_id` — la regla y su trampa de migración

> El `nodo_id` se aplica al **registrar un disco nuevo**. **Jamás** se recalcula el `disco_id` de un
> disco ya catalogado.

Por qué: cambiar el `disco_id` cambia **todos** los `archivo_id` de ese disco. Como
`insertar_pendientes` hace `ON CONFLICT (archivo_id) DO NOTHING` (`cola:90`), las filas viejas **no
se borran** y las nuevas **sí se insertan**: el disco quedaría **duplicado entero** en la cola y en
el índice, con la puerta contando el doble.

- `local` mantiene `nodo_id = "local"` y **no prefija**. Los ids de hoy siguen válidos para siempre.
- En híbrido los discos nuevos nacen `mac-01:RESPALDO`, `vps-01:dump-padron-2026-08`.
- Cuando `nodo_id != "local"`, el `disco_id` **deja de ser opcional**: derivarlo de un basename es
  exactamente lo que causa P1.
- `norm doctor` lista los discos sin prefijo y explica que re-catalogarlos con id nuevo los
  duplicaría. Decisión del operador, informada.

### 3.2 Índice por nodo, y arreglar el rollover en la misma pasada

```python
def indice_escritura(config) -> str:
    n = config.despliegue.nodo_id
    return f"{config.indice_alias}-000001" if n == "local" else f"{config.indice_alias}-{n}-000001"
```

- `local` produce **exactamente el nombre de hoy** → cero migración.
- Los nodos híbridos escriben en índices disjuntos; el alias los une para leer, sin tocar
  `busqueda.py`.
- El restore del snapshot ajeno **añade** índices, nunca sobrescribe.

**En la misma fase se arregla P3.2.** Tocar `aplicar_indice` a medias dejaría el rollover igual de
roto pero con más índices: hay que añadir `is_write_index: true` al alias, `rollover_alias` a la
plantilla, y que `indice_escritura` **resuelva el índice de escritura desde el alias** en vez de
fijarlo. Si no, cada índice se queda en `-000001` para siempre.

### 3.3 El almacén y quién puede elegir destino

| Perfil | Almacén | Por qué |
|---|---|---|
| `local` | destino elegible por corrida (como hoy) | Nada cambia |
| `mac-01` | destino elegible por corrida | Es el **maestro**: no empuja blobs, N carpetas no estorban |
| `vps-01` | **MinIO fijo, sin selector** | Empuja blobs al maestro: necesita un almacén único y direccionable (P2) |

**Direcciones de replicación** — asimétricas a propósito:

| Qué | Dirección | Por qué |
|---|---|---|
| Snapshots del índice | `mac-01 → vps-01` | El VPS sirve búsquedas sobre todo el corpus |
| Blobs HOT | `vps-01 → mac-01` | La copia permanente converge donde hay espacio |
| Almacén **frío** | **no se replica** | Lo más pesado y lo menos consultado; vive en la Mac |

**Consecuencia aceptada:** desde el VPS se puede **buscar** todo, pero **descargar el original** de
un archivo que ingirió la Mac requiere que la Mac esté accesible. El puerto `Almacen`
(`almacen/__init__.py:18-31`) permitiría un backend con fallback remoto; **no está en este plan**.

### 3.4 La puerta, por propiedad del disco

Cada nodo emite veredicto **solo sobre los discos que él registró**; por uno ajeno → 409, nunca un
veredicto inventado. En `local` es dueño de todo y responde siempre: **un solo camino de código**.

Y una condición extra para el nodo que **no** es maestro:

> En `vps-01`, un origen es seguro para desechar cuando el 100 % de sus filas está HECHO o
> COLD-movido **y además sus blobs llegaron al archivo maestro**.

Sin eso borrarías un dump cuya única copia vive en un VPS de 1 TB. Se añade al `SELECT` de
`evaluar_puerta`, el único sitio donde vive la regla.

### 3.5 Entidades: un solo resolvedor, y el rescan explícito

`iniciar_bucle` deja de ser incondicional y queda tras `topologia.corre_entidades` (P5). El backfill,
igual.

Para P6, el nodo de servicio necesita que el backfill **vea lo replicado**. Dos opciones, y hay que
elegir una a sabiendas:

| Opción | Cómo | Coste |
|---|---|---|
| **Rescan periódico** (recomendado para empezar) | `norm backfill-entidades --reiniciar` tras cada restore | Un barrido completo del índice por ciclo. Idempotente y simple |
| Cursor por lote replicado | Backfill acotado al rango de índices recién restaurados | Menos trabajo, pero acopla el backfill a la replicación |

La solución de fondo —un campo `indexado_en` monótono en el documento para barrer por tiempo en vez
de por hash— es la que el propio docstring del backfill señala (`backfill.py:20-22`) y **no está en
este plan**: cambia el mapping y obliga a reindexar.

---

## 4. Plan por fases

Ninguna fase cambia el comportamiento de `local`.

| Fase | Entregable | Aceptación |
|---|---|---|
| **H0 — Cimientos** | `PerillasDespliegue` (K16) + `Topologia` + `derivar()`. Default `local`. Sin sitios de uso | Golden file: la `Config` por defecto es **idéntica** a la de hoy. Suite verde sin tocar tests |
| **H1 — Identidad del disco** | `disco_id` obligatorio si `nodo_id != "local"`; prefijo en discos **nuevos**; aviso en `doctor` | Property test: los `archivo_id` catalogados **no cambian**. Dos nodos → ids disjuntos. Extiende `test_walker.py:47` |
| **H2 — Índice por nodo + rollover** | `indice_escritura` por nodo · `is_write_index` · `rollover_alias` · resolver el índice **desde el alias** | `local` da el mismo nombre que hoy. Restaurar el snapshot ajeno **no borra** el propio. El rollover promueve y el sink lo sigue |
| **H3 — Capacidades en los bordes** | CLI y API consultan `Topologia`. **`iniciar_bucle` tras `corre_entidades`** (P5). `_destino_eligible` incorpora la capacidad (P2). Rutas de entidades → 409 donde no aplican | Tabla perfil × ruta/comando como **dato**, no `if` repetido. Un nodo `hibrido-ingesta` **no** arranca el demonio de envío |
| **H4 — Puerta por propiedad** | Veredicto solo sobre discos propios + condición "replicado al maestro" | Ningún nodo emite veredicto ajeno. Un origen sin replicar **no** es seguro. Los tests de puerta no se relajan |
| **H5 — Compose de producción** | `docker-compose.prod.yml` por perfil: sin puertos publicados salvo Caddy, secretos por entorno, OpenSearch **con** seguridad | `docker compose config` no expone 5432/9200/9000. Extiende `test_contratos_deploy.py` |
| **H6 — Replicación + entidades** | Buckets, política MinIO, `norm replicar`, timers, gauges de réplica en el exportador, alerta + runbook. Rescan del backfill tras restore (§3.5) | E2E de dos instancias: el VPS ve los docs del Mac, el Mac recibe los blobs del VPS, reindexar **no duplica**, y las entidades cubren lo replicado |
| **H7 — Ergonomía** | `norm doctor`, tres `.env.ejemplo`, `arrancar.command`/`.bat` leen el perfil, insignia en la UI | `doctor` detecta: discos sin prefijo, store inalcanzable, API sin llaves, réplica atrasada, perfil incoherente |
| **H8 — Endurecimiento** | WireGuard, Caddy+TLS, rotación de secretos, backup/PITR, runbooks del híbrido | Despliegue reproducible desde cero siguiendo solo la documentación |

**Camino crítico:** H0 → H1 → H2 son el cimiento de datos y **no se reordenan**. H5 va **antes** de
exponer nada a internet. H3 debe preceder a cualquier arranque simultáneo de las dos APIs (si no,
P5 duplica el empuje al AEB desde el primer minuto).

**H2 tiene valor aunque canceles el híbrido:** arregla un rollover que hoy no funciona.

---

## 5. Pruebas

- **CI en los tres perfiles.** `local` es la referencia: si un perfil nuevo cambia un resultado de
  `local`, el build falla. Es lo que hace sostenible tener dos modos.
- **Golden file de la `Config`** (H0): congela los defaults; cualquier deriva salta en el diff.
- **Inmutabilidad de `archivo_id`** (H1): el corpus indexado sigue siendo direccionable.
- **Property test de fusión** (H2): dos conjuntos en nodos distintos → el alias devuelve exactamente
  `|A ∪ B|`. Es la propiedad de la que depende todo.
- **Contrato por capacidad** (H3): tabla perfil × ruta como dato. Incluye "el demonio de envío
  arranca ⟺ `corre_entidades`".
- **La puerta sigue siendo sagrada** (H4): los tests existentes no se relajan; la condición nueva
  solo **añade** restricción.
- **Cobertura del backfill sobre índice replicado** (H6): tras un restore, ninguna entidad resoluble
  queda sin resolver.

---

## 6. Riesgos

| Riesgo | Mitigación |
|---|---|
| `if perfil ==` se cuela y se multiplica | Capacidades, no perfiles (§3.0). Regla de revisión: `perfil` solo se lee en `derivar()` |
| **Re-catalogar un disco con id nuevo lo duplica entero** | Nunca recalcular el id de un disco existente (§3.1) |
| **El restore del snapshot borra el índice del otro nodo** | Índice por nodo (§3.2). El más caro y el más silencioso |
| **Dos APIs empujando al AEB y pisándose** | `iniciar_bucle` tras capacidad (H3). Hoy es incondicional |
| **El backfill salta la mitad de lo replicado** | Rescan explícito tras restore (§3.5), decidido y documentado |
| Replicar "el bucket" cuando los blobs están en carpetas sueltas | El selector de destino es una capacidad (§3.3) |
| El VPS desecha un origen sin copia en el maestro | La puerta exige replicación al maestro (§3.4) |
| Arreglar el índice a medias y dejar el rollover roto | H2 incluye `is_write_index` + `rollover_alias` |
| La réplica se detiene en silencio | Gauges + alerta + `doctor`. Hoy el exportador solo mira la cola (`metricas.py:58-93`) |
| El rate-limit no protege con dos instancias | Es en memoria por proceso (`seguridad.py:9-25`); el límite real va en Caddy (H8) |
| El compose `.dev` acaba en el VPS | H5 antes de cualquier despliegue público |
| El modo local deja de estar probado | CI en los tres perfiles (§5) |

---

## 7. Lo que este plan NO hace

- **No parte los workers en varias máquinas** (Fase 7 de ARQUITECTURA §8). Deja el hueco abierto.
- **No mueve las entidades a un servicio aparte** (PLAN_BACKEND_CENTRAL). `vps-01` resuelve y empuja
  al AEB con el `envio.py`/`destino.py` que ya existen.
- **No añade `indexado_en` al mapping** (§3.5). Cambiaría el contrato del índice y obligaría a
  reindexar.
- **No implementa descarga remota de blobs** desde el VPS (§3.3).
- **No cambia el esquema de Postgres** salvo lo mínimo de H4. Ambos nodos corren el mismo
  `alembic upgrade head` y usan subconjuntos distintos de tablas. Simple gana a listo.
- **No implementa replicación en Python.** Azazel declara el contrato, orquesta el snapshot y observa
  el lag; MinIO y OpenSearch hacen el trabajo.

---

## 8. Decisiones abiertas

1. **Discos ya catalogados sin prefijo de nodo.** ¿Se quedan así (seguro, sin coste, conviven sin
   problema) o migración explícita que los renombre? La migración implica reindexarlos completos.
2. **Frecuencia del rescan de entidades** (§3.5). Depende de cuántos documentos nuevos llegan por
   ciclo de replicación — dato que sale del piloto.

---

> **Sobre el dimensionado:** el piloto de `PLAN_PILOTO_M4` no se ha corrido y no existe
> `BENCHMARKS.md`. El crecimiento del índice por millón de archivos, el **% HOT real** y el factor de
> expansión de comprimidos siguen sin medirse. Este plan es correcto con cualquier valor; el
> **tamaño del disco del VPS** no lo será hasta medirlos.
