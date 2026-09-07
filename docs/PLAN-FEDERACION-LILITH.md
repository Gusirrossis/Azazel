# Plan — que Lilith pueda buscar en Azazel

**Este plan es para quien desarrolla Azazel.** El plan hermano, del lado de
Lilith, está en `Lilith/docs/PLAN-AZAZEL.md`.

Lo primero, y es importante para decidir prioridades: **nada de esto bloquea a
Lilith.** La integración funciona hoy, contra Azazel tal y como está. Lo que
sigue son mejoras medidas, no requisitos — con una sola excepción, la clave con
nombre de la fase 1, que es de higiene y cuesta un minuto.

> ## Estado: EJECUTADO — 2026-09-05
>
> Las seis fases están hechas y verificadas contra el servidor real. Este documento
> se conserva porque explica **por qué** cada decisión, que es lo que no se puede
> reconstruir del código.
>
> | Fase | Qué era | Estado |
> |---|---|---|
> | 1 | Clave con nombre para Lilith | ✅ clave `lilith`, alta 2026-09-04 20:53 UTC |
> | 2 | Dejar pedir menos campos (`campos`) | ✅ medido: 564.150 B → 11.337 B (98 % menos) |
> | 3 | Que la conexión se reutilice | ⚠️ ver abajo — HTTP/1.1 y `Alt-Svc: h3` anunciado |
> | 4 | Sonda de disponibilidad barata | ✅ `GET /salud` responde 200 |
> | 5 | Decir de dónde viene cada resultado | ✅ campo `origen` en la respuesta |
> | 6 | Decidir si un consumidor descarga | ✅ **decidido que NO** — 403 verificado |
>
> **La fase 3 es la que queda a medias.** Comprobado el 2026-09-05:
> `curl -sI` devuelve `HTTP/1.1` y la cabecera `Alt-Svc: h3=":443"; ma=2592000`. O
> sea: el anuncio de HTTP/3 que este mismo documento advertía que costó una tarde en
> Lilith **está puesto aquí**, con 30 días de vigencia. No afecta a la federación
> (servidor a servidor) pero sí a quien abra el panel en un navegador.
>
> Añadido después de escribir este plan: el `/buscar` federado devuelve también las
> **entidades** que casan con la búsqueda —por CURP/RFC exacta, por nombre, o por las
> anclas de los documentos encontrados— vía `incluir_entidades`. Ver `ESTADO.md`.

## 0. Qué es Lilith y qué va a pedir

Lilith es una consola web sobre bases SQLite: 110 bases, 61,9 GB, y un buscador
global que las recorre en ~68 s. Va a llamar a `POST /buscar` **una vez por
búsqueda de usuario**, con un texto libre y un tope de resultados, y a enseñar lo
que devuelvas junto a lo suyo.

Volumen esperado: **una petición por búsqueda**, no una por página ni por
pulsación. No hay autocompletado ni sondeo. Con el uso actual de Lilith eso es
del orden de decenas de peticiones al día, no miles.

## 1. Lo que se midió (2026-08-29, contra tu VPS real)

| | |
|---|---|
| Índice | **500.258 documentos · 13,08 TB** |
| `POST /buscar` «garcia» dentro del VPS | **9.007 resultados en 0,13 s** |
| Lo mismo desde el VPS de Lilith | 0,88 s – 6,29 s |
| De eso, **abrir la conexión (TLS)** | **0,60 s – 5,39 s** |
| Página de 20 resultados | **66.559 bytes** |
| De esos, `texto_indexable` | **~39 KB (59 %)** |

**Tu API es rápida. El problema está en el transporte**, y de ahí salen las dos
peticiones que de verdad importan (fases 2 y 3).

## 2. Fase 1 — una clave con nombre para Lilith *(lo único urgente)*

Ya tienes el mecanismo hecho y bien hecho: `claves_busqueda.py`, claves con
nombre, solo el sha256 guardado, gestionables desde el panel, verificación en
tiempo constante.

**Lo que hay que hacer: generar una clave llamada `lilith` desde el panel y
pasársela al equipo de Lilith.** Nada de código.

Y lo que hay que evitar: reutilizar la clave estática de `NORM_API_KEYS`. Hoy es
una sola para todo, en el `.env.prod`, y compartirla significa que revocar el
acceso de Lilith obliga a rotarla para todos los consumidores a la vez. Con una
clave con nombre, revocar a Lilith es un clic y no afecta a nadie más.

## 3. Fase 2 — dejar pedir menos campos *(el mayor ahorro)*

**El 59 % de cada respuesta es `texto_indexable`, y Lilith no lo usa.** Ya le
mandas `_resaltado` con los fragmentos donde aparece lo buscado, que es
exactamente lo que se pinta. El texto completo del archivo viaja entero, cruza
Europa y se descarta al llegar.

Reparto por campo, de un documento real:

```
texto_indexable   1962 bytes   <-- no se usa
senales            283
_resaltado          198   <-- esto es lo que se pinta
clave_almacen        72
procedencias         71
archivo_id           66
hash_contenido       66
...
nombre               10
```

Lo mínimo que resuelve esto es un campo más en `SolicitudBusqueda`:

```python
campos: list[str] | None = Field(
    default=None, max_length=40,
    description="Campos a devolver. Por omisión, todos (comportamiento actual).",
)
```

…que se traduzca a `_source` en el cuerpo de OpenSearch. Con una allowlist, no
con lo que llegue: es la misma disciplina que ya tiene `construir_consulta`, donde
el texto del usuario solo viaja como valor.

**Por omisión no cambia nada**, así que ningún consumidor existente se entera.

Alternativa aún más barata si prefieres no tocar el esquema: un
`excluir_texto: bool = False`. Menos flexible, cero riesgo.

## 4. Fase 3 — que la conexión se pueda reutilizar

Esta es la medición incómoda: **0,60 s a 5,39 s solo en abrir la conexión**,
contra 0,13 s de la consulta. Lilith va a mantener una conexión persistente, pero
eso solo sirve si el servidor la deja viva.

Qué comprobar, por orden:

1. **`keep_alive` en Caddy hacia el backend** y que no se cierre la conexión de
   cliente antes de tiempo. Un `Connection: close` en la respuesta tira por
   tierra la reutilización.
2. **HTTP/2**, que multiplexa y reaprovecha el saludo. Caddy lo sirve por defecto;
   solo hay que confirmar que llega hasta el cliente.
3. **Un aviso desde la experiencia de Lilith, que costó una tarde entera:** tu
   Caddy publica `443/udp` y con él anuncia `Alt-Svc: h3`. Un navegador guarda ese
   anuncio **por perfil y durante semanas**, y si la red del usuario no deja pasar
   el UDP, las peticiones mueren **antes de salir** — sin código de estado, sin
   cabeceras y sin una sola línea en los registros del servidor. En Lilith obligó
   a apagar HTTP/3 y a mandar `Alt-Svc: clear`. Aquí no afecta a la federación
   —Lilith llama de servidor a servidor— pero **sí a quien use tu panel desde el
   navegador**. Merece una comprobación.

## 5. Fase 4 — una sonda de disponibilidad barata

Lilith necesita saber si Azazel contesta **antes** de lanzar la búsqueda, para
degradar en silencio si no. Hoy lo más parecido es `/estadisticas`, que ejecuta
tres agregaciones sobre 500.000 documentos para responder «sí, estoy vivo».

Un `GET /salud` que no toque OpenSearch —o que solo haga un `ping`— y conteste en
milisegundos. Sin autenticación o con ella, da igual; lo que importa es que sea
barato y que no mienta: si OpenSearch está caído, tiene que decirlo, porque
`/buscar` va a fallar de todas formas.

## 6. Fase 5 — decir de dónde viene cada resultado

Lilith va a mezclar resultados suyos con los tuyos, y **tiene que poder
etiquetarlos sin inventarse el nombre**. Hoy tendría que escribir «Azazel» a mano
en su propio código, y el día que haya un segundo nodo dejaría de ser cierto.

Un identificador estable en la respuesta:

```python
class RespuestaBusqueda(BaseModel):
    total: int
    documentos: list[dict[str, Any]]
    origen: str = "azazel"          # o el nombre real del nodo
    ...
```

Es una línea, y evita que el nombre de tu sistema quede cableado en el código de
otro.

## 7. Fase 6 — abrir un resultado

Cuando alguien encuentre algo en Lilith que vive en Azazel, va a querer verlo. Ya
tienes `GET /archivo/{archivo_id}` y `GET /archivo/{archivo_id}/contenido`.

Lo que hace falta decidir —y es tuyo, no de Lilith—: **si una clave de búsqueda
puede además descargar contenido.** Son dos permisos distintos. Buscar es saber
que un documento existe; descargarlo es tenerlo. Si la respuesta es «no», Lilith
enseñará la ruta y ahí se acaba, que también es útil.

## 8. Lo que NO hace falta

- **CORS.** Lilith llama de servidor a servidor, desde su contenedor `api`. No
  abras CORS para esto: sería exponer tu API al navegador de cualquiera.
- **Replicar el índice.** Lilith consulta en vivo. Copiar 13 TB para buscarlos
  dos veces no tiene sentido y quedaría desfasado el primer día.
- **Un endpoint nuevo de federación.** `POST /buscar` ya es exactamente lo que
  hace falta.
- **Cambiar el modelo de permisos por ahora.** Del lado de Lilith, los resultados
  de Azazel se limitan a administradores mientras no exista algo mejor. Si
  algún día quieres permisos por consumidor —que una clave vea solo ciertos
  discos—, ahí sí hay conversación; pero es otro proyecto.

## 9. Resumen para priorizar

| Fase | Coste | Valor | ¿Bloquea a Lilith? |
|---|---|---|---|
| 1 · clave con nombre | un minuto, sin código | poder revocar sin afectar a nadie | no, pero hazlo ya |
| 2 · pedir menos campos | pequeño | **−59 % de tráfico** | no |
| 3 · reutilizar conexión | comprobación | **el mayor efecto en la latencia** | no |
| 4 · sonda barata | pequeño | degradar sin castigar a OpenSearch | no |
| 5 · identificador de origen | una línea | que nadie cablee tu nombre | no |
| 6 · descargar contenido | decisión de producto | ver lo encontrado | no |
