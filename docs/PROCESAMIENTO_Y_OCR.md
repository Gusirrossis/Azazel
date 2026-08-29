# Cómo se vuelve consultable un archivo

La cadena completa, de bytes a ficha de persona:

```
bytes → precalificación → extractor → texto_indexable → índice → búsqueda
                                              ↓
                                      anclas (CURP/RFC) → entidades
```

Si un archivo no produce texto, **no existe** para nadie: ni para la búsqueda ni para
la resolución de entidades. Todo lo que sigue trata de esa frase.

---

## Las seis decisiones y por qué

### 1. Se mide antes de tocar

Sin una medida, cualquier cambio en el OCR es fe. "Subimos a 300 dpi" y "activamos el
deskew" suenan a mejora, pero cuestan tiempo por página y alguno puede empeorar el
resultado en este corpus concreto.

```bash
norm calidad muestrear --salida dorado/      # 1. la herramienta elige y exporta
#    2. una PERSONA transcribe dorado/verdad/*.txt y *.anclas
norm calidad evaluar --conjunto dorado/ --guardar linea_base.json
#    3. se cambia algo
norm calidad evaluar --conjunto dorado/ --contra linea_base.json
```

El paso 2 no se puede automatizar: si la verdad la produjera el propio OCR, se estaría
midiendo contra sí mismo.

**La métrica que manda es el recall de anclas, no el CER.** Un texto con 8% de
caracteres mal pero con todas las CURP legibles produce las entidades correctas; uno
con 2% de error que se comió un dígito verificador, no.

El muestreo es **estratificado**, no aleatorio: una muestra al azar de este corpus
daría casi puro PDF nativo, que es lo que ya funciona. El cupo se reparte hacia donde
está el riesgo (40% escaneos, 30% imágenes de documento). La semilla es fija: dos
personas obtienen la misma muestra y ampliar el conjunto no invalida lo ya anotado.

### 2. Cada contenido se lee una sola vez

El almacén ya deduplicaba los **bytes**; ahora la tabla `extracciones` deduplica el
**trabajo de entenderlos**, con `hash_contenido` como clave.

En el corpus actual esto no es un detalle: **39 312 archivos contra 19 656 hashes
únicos**, exactamente el doble. Cada archivo está duplicado, así que la mitad del OCR
se pagaba por información ya conocida.

```bash
norm reextraer estado     # cuántos reusos y cuánto tiempo se ahorró
```

### 3. Un timeout ya no tira el trabajo hecho

El plazo es **cooperativo**: viaja dentro del contexto (`ctx.vencido()`) y los plugins
con bucle lo consultan en cada página. Al agotarse devuelven lo acumulado con la
bandera `ocr_pdf_parcial`.

Antes el corte ocurría fuera y descartaba el resultado entero: un PDF de 20 páginas
que alcanzaba a leer 15 se indexaba sin una línea. En un corpus de escaneos ése no era
el caso raro, era el común.

El corte duro (25% por encima del plazo) sigue existiendo para el plugin que se cuelga
dentro de una librería nativa y nunca mira el reloj.

### 4. El OCR sabe cómo le fue

`image_to_data` da confianza por palabra; el viejo `image_to_string` solo texto. Ahora
cada documento lleva `ocr_confianza` (0-100, media ponderada por longitud de palabra).

Sin esa medida, `|||l1 0O ¬` y un acta bien leída entraban al índice con la misma cara.
Con ella se puede filtrar, priorizar el reproceso y ver en Grafana si una corrida va mal
**mientras corre**.

Por debajo de `ocr_confianza_descarte` el texto **no se indexa**. Un texto inventado es
peor que ninguno: ensucia la búsqueda y mete anclas falsas en la resolución de
entidades, creando personas que no existen. El texto no se pierde — queda en
`extracciones` y `norm reextraer` lo vuelve a intentar.

**Distinguir `NULL` de `0` importa:** un CSV o un PDF con texto nativo no tienen
confianza, tienen el texto exacto. Filtrar por `ocr_confianza < 50` no debe arrastrar
todo el corpus de texto nativo, que es la mayoría.

### 5. Solo se OCR-ea lo que parece un documento

Antes, activar el OCR mandaba **toda** imagen a HOT sin pasar por el scoring: un fondo
de pantalla costaba lo mismo que un acta.

Ahora un clasificador de umbrales mira aspecto, saturación y proporción de blanco sobre
una miniatura de 200 px, **antes de pagar nada**:

| Señal | Decisión |
|---|---|
| Lado < `imagen_ancho_min_documento` | frío — ícono o miniatura |
| Aspecto > 3:1 | frío — ningún papel es tan alargado |
| Casi todo blanco | frío — página en blanco |
| Saturación alta y poco blanco | frío — fotografía |
| Resto | **HOT** — parece escaneo |

No es un modelo: es auditable, determinista y prácticamente gratis. Ante la duda decide
HOT, y lo descartado va a **frío, que es reversible** — `rescore-frio` lo rescata si
mañana se decide otra cosa.

### 6. Lo mal leído se puede rehacer

`archivo_id` es determinista sobre (ruta, tamaño, mtime), así que un archivo en `HECHO`
con OCR malo no volvía a entrar nunca. Pero el almacén es direccionado por contenido:
los bytes se leen por `hash_contenido`, **sin necesitar el disco original**.

```bash
norm reextraer --listar                            # ¿cuánto trabajo hay?
norm reextraer --confianza-menor 50                # lo peor leído, primero
norm reextraer --bandera ocr_pdf_parcial           # los que se quedaron a medias
norm reextraer --version-vieja                     # todo lo de una versión anterior
```

Reindexa por `archivo_id`, que es idempotente: correrlo dos veces no duplica nada.
Informa de cuántos **mejoraron y cuántos empeoraron**, para decidir con datos si la
pasada valió la pena.

---

## El lazo hacia entidades

`entidades/anclas.py` centraliza la detección de CURP/RFC que antes vivía dentro del
backfill. Ahora la usan los dos caminos —el worker en vivo y el backfill del histórico—
y eso importa: si decidieran distinto, el mismo documento produciría entidades
diferentes según por dónde entró.

Además guarda la **ventana de contexto**: ±200 caracteres alrededor de cada ancla, en
`contexto_anclas`. Hoy la resolución solo fija el ancla y lo que de ella se deriva; el
nombre y el domicilio que están en la línea de al lado se pierden porque asociarlos
requiere NER (E8). Guardar la ventana ahora cuesta unos bytes y es exactamente la
entrada que ese NER necesitará — sin tener que volver a OCR-ear 39 000 documentos.

El contexto se corta del texto **original**, no de su versión en mayúsculas: un NER se
apoya en la capitalización para reconocer nombres propios.

---

## Búsqueda en español

`texto_indexable` pasa a usar un analizador con `asciifolding` + stemmer ligero.

El analizador `standard` pasa a minúsculas pero **no pliega acentos**: buscar `Ramírez`
no encontraba `RAMIREZ`, y el OCR destroza acentos constantemente. Sobre texto
reconocido eso se comía una fracción grande de los aciertos.

El subcampo `texto_indexable.exacto` conserva el análisis estándar para búsquedas
literales.

**Requiere un ÍNDICE NUEVO, no basta reindexar documentos.** El mapping de un índice
existente es casi inmutable: `norm aplicar-indice` solo afecta a los índices que se
creen después, y reescribir documentos uno a uno (lo que hace `norm reextraer`) no
cambia el analizador con el que se tokenizan. Sin este paso la mejora no ocurre.

```bash
norm reindexar                              # crea el índice nuevo y lanza la copia
# vigilar: GET _tasks/<id-que-imprimió>
norm reindexar --finalizar archivos-000002  # mueve el alias, atómicamente
```

Usa el `_reindex` de OpenSearch: copia servidor-a-servidor, sin volver a extraer ni a
pasar OCR. Es **reversible** — el índice viejo sale del alias pero sigue existiendo
hasta que se borre con `--borrar-viejo`. El alias no se mueve si el índice nuevo tiene
menos documentos que el viejo.

## Datos personales en el índice

`contexto_anclas.contexto` está **excluido de `_source`** en el mapping. Sigue indexado
y buscable, pero no se devuelve en los resultados: sin esa exclusión, cualquier
`lector` —incluida una clave de máquina externa como reddoor— podía enumerar el corpus
de datos personales navegando hits, que no es una capacidad que nadie decidiera darle
al índice.

---

## Orden de despliegue

```bash
alembic upgrade head                       # crea `extracciones`
norm aplicar-indice                        # plantilla nueva (solo afecta a índices nuevos)
norm reindexar                             # índice nuevo + copia; luego --finalizar
norm calidad muestrear --salida dorado/    # y transcribir
norm calidad evaluar --conjunto dorado/ --guardar linea_base.json
# activar OCR en .env.prod, reiniciar
norm reextraer --version-vieja --limite 500   # por lotes, midiendo
```

Primero se quita el 50% duplicado (Fase 1), **después** se gasta mejor lo que queda
(300 dpi es más caro por página). El orden inverso paga la mejora dos veces.

---

## Cosas que rompen esto y no lo parecen

- **Subir `VERSION_EXTRACTOR` sin querer** invalida la caché entera y dispara una
  pasada de OCR sobre todo el corpus. Súbela solo cuando un cambio deba rehacer el
  trabajo ya hecho.
- **`ocr_confianza_descarte` demasiado alto** deja documentos legibles sin texto. Se
  ajusta contra el conjunto dorado, no a ojo.
- **Apagar `ocr_deskew` o `ocr_binarizar` "para ir más rápido"** puede costar más
  recall del que ahorra en tiempo. Se mide antes.
- **Reindexar sin aplicar el mapping nuevo** deja los documentos con el analizador
  viejo y el problema de los acentos intacto.

---

## Lo que queda pendiente

**Aislamiento por proceso.** El corte de un extractor sigue siendo por hilo: un plugin
que respeta el plazo termina solo y no deja nada colgando, pero uno atascado dentro de
código C deja el hilo huérfano hasta el fin del proceso. Pasarlo a `ProcessPoolExecutor`
exige que la fuente cruce la frontera del proceso, y hoy es un `SpooledTemporaryFile`
no serializable. Es un refactor con riesgo propio y va con el hardening de Fase 7.

Mitigación mientras tanto: el pool tiene dos hilos, así que un plugin abandonado no
congela el archivo siguiente.
