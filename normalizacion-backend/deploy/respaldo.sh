#!/usr/bin/env bash
# Respaldo lógico de Postgres → bucket de MinIO, con retención.
#
# Qué se respalda y qué NO, y por qué:
#
#   · Postgres SÍ. Es el plano de control: la cola, las corridas, las entidades
#     resueltas, los overrides y los cursores de réplica y backfill. Nada de eso se
#     puede reconstruir: la cola sabe qué se procesó y qué no, y perderla significa
#     reprocesar discos que quizá ya se desecharon.
#   · El ÍNDICE no, aquí. Se replica por snapshots (`norm replicar`) y además es
#     reconstruible desde los blobs. Duplicarlo en el respaldo sería pagar dos veces.
#   · Los BLOBS no. Son el dato masivo; su seguridad es el archivo maestro y la
#     replicación entre nodos, no un dump nocturno.
#
# Limitación honesta: esto es un dump lógico, NO point-in-time recovery. Se
# recupera hasta el último respaldo, no hasta el segundo antes del incidente. PITR
# real exige archivar WAL de forma continua a object storage; es más caro de operar
# y no está montado. Con la cola siendo idempotente y el catálogo incremental,
# perder unas horas significa re-catalogar, no perder datos.
set -euo pipefail

cd "$(dirname "$0")/.."
set -a; . ./.env.prod; set +a

COMPOSE="docker compose -f deploy/docker-compose.prod.yml --env-file .env.prod --profile datos"
BUCKET="${NORM_BUCKET_RESPALDOS:-respaldos}"
RETENCION_DIAS="${NORM_RETENCION_RESPALDOS_DIAS:-14}"
SELLO="$(date -u +%Y%m%d-%H%M%S)"
NOMBRE="pg-${NORM_DESPLIEGUE__NODO_ID:-nodo}-${SELLO}.sql.gz"
TMP="/tmp/${NOMBRE}"

echo "[respaldo] volcando ${NORM_PG_DB:-normalizacion}…"
$COMPOSE exec -T postgres pg_dump -U "$NORM_PG_USER" -d "${NORM_PG_DB:-normalizacion}" \
  | gzip -9 > "$TMP"

TAM=$(stat -c %s "$TMP")
# Un dump vacío o truncado se sube igual de bien que uno bueno, y sólo se descubre
# el día que hace falta restaurar. Mejor fallar aquí y que salte la alerta.
if [ "$TAM" -lt 1024 ]; then
  echo "[respaldo] ERROR: el volcado pesa ${TAM} bytes — sospechoso, no se sube" >&2
  rm -f "$TMP"
  exit 1
fi

echo "[respaldo] subiendo ${NOMBRE} (${TAM} bytes) a ${BUCKET}…"
$COMPOSE exec -T minio sh -c "
  mc alias set l http://localhost:9000 \$MINIO_ROOT_USER \$MINIO_ROOT_PASSWORD >/dev/null &&
  mc mb -p l/${BUCKET} >/dev/null 2>&1 || true"

# `mc pipe` evita tener que copiar el archivo dentro del contenedor.
#
# `mc` pinta la barra de progreso aunque la salida no sea un terminal. Medido en la
# primera corrida desatendida: 180 actualizaciones de «1.55 GiB / ? 92.83 MiB/s»
# separadas por retornos de carro, todas en UNA línea de 5.109 bytes — el 93% de un
# log de 5.477, tapando justo lo único que se va a leer, el resultado.
#
# Medido, no supuesto, con una subida de 3 GB:
#
#   sin flags ............ 8.270 bytes
#   con --quiet .......... 7.467 bytes   ← NO la calla; sólo un 10% menos
#   por stderr ................. 0 bytes ← redirigir stderr no sirve de nada
#   stdout tras el `sed` ...... 68 bytes ← 116 veces menos
#
# Así que la barra sale por stdout, mezclada con la línea de resultado, y `--quiet`
# no la desactiva. El `sed` se queda con lo que hay tras el último retorno de carro,
# que es exactamente lo que un terminal acabaría mostrando.
#
# El `sed` va DENTRO de la tubería y `pipefail` sigue activo, así que un fallo de
# `mc` sigue abortando el respaldo: filtrar el ruido no puede convertir un error en
# un éxito silencioso.
$COMPOSE exec -T minio sh -c "
  mc alias set l http://localhost:9000 \$MINIO_ROOT_USER \$MINIO_ROOT_PASSWORD >/dev/null &&
  mc --no-color pipe l/${BUCKET}/${NOMBRE}" < "$TMP" | sed 's/.*\r//'

rm -f "$TMP"

echo "[respaldo] retención: borrando lo anterior a ${RETENCION_DIAS} días…"
$COMPOSE exec -T minio sh -c "
  mc alias set l http://localhost:9000 \$MINIO_ROOT_USER \$MINIO_ROOT_PASSWORD >/dev/null &&
  mc rm --recursive --force --older-than ${RETENCION_DIAS}d l/${BUCKET}" || true

echo "[respaldo] OK: ${NOMBRE}"
