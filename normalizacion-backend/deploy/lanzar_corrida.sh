#!/usr/bin/env bash
# Corrida de normalización en un contenedor DEDICADO y con TOPE DE DISCO.
#
#   deploy/lanzar_corrida.sh <ruta-dentro-del-contenedor> <disco-id> [workers]
#   p. ej.  deploy/lanzar_corrida.sh /datos/bases LILITH-BASES 4
#
# Por qué así (medido el 29-09):
#   · En la luna de Lilith el disco es de plato y lo comparte con Lilith (lectura
#     aleatoria de 4 KiB a 7,9 MB/s). Una corrida que lo sature deja a Lilith sin disco.
#   · `ionice -c3` y `io.weight` NO sirven aquí: solo actúan con el planificador BFQ, y
#     la luna tiene `none` (la matriz, `mq-deadline`). Lo que sí funciona con cualquier
#     planificador es el TOPE (`io.max` del cgroup), que Docker pone con --device-*-bps
#     y --device-*-iops. Es un techo fijo: frena aunque el disco esté libre.
#   · Lanzar la corrida desde el panel la ejecuta DENTRO del contenedor de la API, y
#     ponerle tope a ese contenedor frenaría también a la API que atiende a Lilith. Por
#     eso un contenedor aparte, como `norm-matrix` en la matriz.
#   · El tope cubre lo que lee y escribe la corrida (los archivos de origen, sus
#     temporales). Lo que escriben por ella MinIO, Postgres y OpenSearch va en sus
#     propios contenedores; queda acotado porque la corrida no les entrega más rápido.
#
# Mientras corre, `/salud` responde `"ocupado": true` (hay una corrida EN_CURSO).
#
# Variables (con su valor por omisión):
#   FRENO_LEER_MBS=40  FRENO_ESCRIBIR_MBS=30  FRENO_LEER_IOPS=400  FRENO_ESCRIBIR_IOPS=300
#   CPUS=4  MEMORIA=8g  IMAGEN=normalizacion-api  NOMBRE=norm-corrida  API=normalizacion-api-1
#   CACHE_T3=<carpeta del host para la caché de extracción; sin ella, /tmp del contenedor>
#   CODIGO=<carpeta src/ del repo: se monta sobre /app/src y corre el código actual sin
#          reconstruir la imagen>
#   SIN_CATALOGO=1  retoma un disco ya catalogado (norm pipeline --sin-catalogo)
#   T3_TOPE_BYTES=<tope de la caché de extracción; sin él, la mitad del disco libre, que
#          en un disco compartido puede ser demasiado (kubo: ~2,7 TB → disco al 91 %)>
#   PRECALIFICA=<procesos del filtro en paralelo; sin él, 1 (un solo hilo, como siempre)>
#
# FRENO_ESCRIBIR_*=0 QUITA el tope de escritura (se recomienda en disco compartido).
# Medido en la luna kubo el 02-10: la precalificación vuelca a /tmp cada entrada de más
# de 8 MB y escribía al tope (50 MB/s). Topar escrituras CON caché no frena al proceso:
# frena el volcado de sus páginas sucias, y ext4 (data=ordered) no cierra un commit del
# journal sin volcarlas. Resultado: un `fsync` ajeno a 276 ms, un kworker 21 min en D y
# dockerd sin poder crear contenedores (la réplica colgada). Matar la corrida lo
# deshizo en el acto; sin el tope, el fsync ajeno bajó a 3 ms.
# OJO: sin tope, la escritura NO queda acotada por la lectura cuando se descomprime
# (medido: un .gz de 3,9 GB volcado a un temporal de ~30 GB a 150-250 MB/s). Es el
# precio de no bloquear al resto; en disco compartido, vigilar el espacio libre (el
# centinela frena al 93 %).
set -euo pipefail

RUTA=${1:?falta la ruta dentro del contenedor (p. ej. /datos/bases)}
DISCO_ID=${2:?falta el disco-id}
WORKERS=${3:-4}
API=${API:-normalizacion-api-1}
IMAGEN=${IMAGEN:-normalizacion-api}
NOMBRE=${NOMBRE:-norm-corrida}
# Usuario y base cambian de nodo a nodo (norm en la matriz, normalizacion en kubo): se
# leen del propio contenedor de Postgres en vez de suponerlos.
PGU=$(docker exec normalizacion-postgres-1 printenv POSTGRES_USER)
PGD=$(docker exec normalizacion-postgres-1 printenv POSTGRES_DB)
PG=(docker exec normalizacion-postgres-1 psql -U "$PGU" -d "$PGD" -tAc)

if docker ps -a --format '{{.Names}}' | grep -qx "$NOMBRE"; then
  echo "ABORTO: ya existe un contenedor '$NOMBRE' (docker rm si terminó)"; exit 2
fi
EN_CURSO=$("${PG[@]}" "SELECT string_agg(id::text, ',') FROM corridas WHERE estado = 'EN_CURSO'")
if [ -n "$EN_CURSO" ]; then
  echo "ABORTO: ya hay una corrida EN_CURSO ($EN_CURSO); la nueva moriría al empezar"; exit 2
fi

# El disco físico que hay debajo de Docker: el tope se pone sobre el dispositivo entero.
PART=$(df --output=source /var/lib/docker | tail -1)
DISCO="/dev/$(lsblk -no PKNAME "$PART")"
[ -b "$DISCO" ] || { echo "ABORTO: no encuentro el disco de $PART"; exit 2; }

# La misma configuración que la API (Postgres, OpenSearch, MinIO, nodo). Lleva
# secretos: archivo solo legible por root y borrado en cuanto el contenedor lo lee.
ENVF=$(mktemp)
chmod 600 "$ENVF"
trap 'rm -f "$ENVF"' EXIT
docker inspect "$API" --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -E '^NORM_' > "$ENVF"

CACHE=()
if [ -n "${CACHE_T3:-}" ]; then
  mkdir -p "$CACHE_T3"
  CACHE=(-v "$CACHE_T3:/cache_t3" -e NORM_T3_CACHE_DIR=/cache_t3)
fi
[ -n "${T3_TOPE_BYTES:-}" ] && CACHE+=(-e "NORM_T3_CACHE_DISCO_BYTES=$T3_TOPE_BYTES")
[ -n "${PRECALIFICA:-}" ] && CACHE+=(-e "NORM_WORKER__PROCESOS_PRECALIFICA=$PRECALIFICA")
CODIGO_V=()
if [ -n "${CODIGO:-}" ]; then
  [ -d "$CODIGO/normalizacion" ] || { echo "ABORTO: $CODIGO no es la carpeta src/ del repo"; exit 2; }
  CODIGO_V=(-v "$CODIGO:/app/src:ro")
fi
FRENOS=(--device-read-bps "$DISCO:${FRENO_LEER_MBS:-40}mb" --device-read-iops "$DISCO:${FRENO_LEER_IOPS:-400}")
EM=${FRENO_ESCRIBIR_MBS:-30}
EI=${FRENO_ESCRIBIR_IOPS:-300}
[ "$EM" != 0 ] && FRENOS+=(--device-write-bps "$DISCO:${EM}mb")
[ "$EI" != 0 ] && FRENOS+=(--device-write-iops "$DISCO:${EI}")
EXTRA=()
[ "${SIN_CATALOGO:-0}" = 1 ] && EXTRA=(--sin-catalogo)

docker run -d --name "$NOMBRE" --network normalizacion_interna --volumes-from "$API" \
  --env-file "$ENVF" -e NORM_WORKER__LOTE_CLAIM=25 "${CACHE[@]}" "${CODIGO_V[@]}" "${FRENOS[@]}" \
  --cpus "${CPUS:-4}" --memory "${MEMORIA:-8g}" --memory-swap "${MEMORIA:-8g}" --restart no \
  "$IMAGEN" norm pipeline "$RUTA" --disco-id "$DISCO_ID" --workers "$WORKERS" "${EXTRA[@]}"

echo "lanzada '$NOMBRE' sobre $RUTA ($DISCO_ID)${EXTRA:+ sin catalogo}${CODIGO:+ con el codigo de $CODIGO}, tope en $DISCO:" \
  "lectura ${FRENO_LEER_MBS:-40} MB/s y ${FRENO_LEER_IOPS:-400} IOPS," \
  "escritura $([ "$EM" = 0 ] && echo 'sin tope MB/s' || echo "$EM MB/s") y $([ "$EI" = 0 ] && echo 'sin tope IOPS' || echo "$EI IOPS")"
echo "seguir:  docker logs -f $NOMBRE | grep -E 'fase_|corrida_'   ·   /salud → ocupado"
echo "tope en vivo:  cat /sys/fs/cgroup/system.slice/docker-\$(docker inspect -f '{{.Id}}' $NOMBRE).scope/io.max"
