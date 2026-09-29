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
set -euo pipefail

RUTA=${1:?falta la ruta dentro del contenedor (p. ej. /datos/bases)}
DISCO_ID=${2:?falta el disco-id}
WORKERS=${3:-4}
API=${API:-normalizacion-api-1}
IMAGEN=${IMAGEN:-normalizacion-api}
NOMBRE=${NOMBRE:-norm-corrida}
PG=(docker exec normalizacion-postgres-1 psql -U norm -d normalizacion -tAc)

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

docker run -d --name "$NOMBRE" --network normalizacion_interna --volumes-from "$API" \
  --env-file "$ENVF" -e NORM_WORKER__LOTE_CLAIM=25 "${CACHE[@]}" \
  --device-read-bps "$DISCO:${FRENO_LEER_MBS:-40}mb" \
  --device-write-bps "$DISCO:${FRENO_ESCRIBIR_MBS:-30}mb" \
  --device-read-iops "$DISCO:${FRENO_LEER_IOPS:-400}" \
  --device-write-iops "$DISCO:${FRENO_ESCRIBIR_IOPS:-300}" \
  --cpus "${CPUS:-4}" --memory "${MEMORIA:-8g}" --memory-swap "${MEMORIA:-8g}" --restart no \
  "$IMAGEN" norm pipeline "$RUTA" --disco-id "$DISCO_ID" --workers "$WORKERS"

echo "lanzada '$NOMBRE' sobre $RUTA ($DISCO_ID), tope en $DISCO:" \
  "lectura ${FRENO_LEER_MBS:-40} MB/s y ${FRENO_LEER_IOPS:-400} IOPS," \
  "escritura ${FRENO_ESCRIBIR_MBS:-30} MB/s y ${FRENO_ESCRIBIR_IOPS:-300} IOPS"
echo "seguir:  docker logs -f $NOMBRE | grep -E 'fase_|corrida_'   ·   /salud → ocupado"
echo "tope en vivo:  cat /sys/fs/cgroup/system.slice/docker-\$(docker inspect -f '{{.Id}}' $NOMBRE).scope/io.max"
