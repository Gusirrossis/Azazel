#!/usr/bin/env bash
# Publica la réplica de una LUNA para que la MATRIZ se la lleve:
#   flush → snapshot → purga → export del bucket a una carpeta → marca `.azazel-listo`.
#
# La luna NO se conecta a la matriz: no tiene llave ni camino de red hacia ella. Es la
# matriz la que entra a LEER esta carpeta (y solo esta) con una llave limitada en
# ~/.ssh/authorized_keys a `restrict,from="<ip de la matriz>",command="rrsync -ro <export>"`;
# ver deploy/matriz/traer_replica.sh. Hasta el 22-10 era al revés: la luna entraba como
# root en la matriz para inyectar y restaurar, y quien tomara una luna tenía la matriz.
#
# La marca `.azazel-listo` dice qué generación del repositorio está ENTERA en la carpeta.
# Se borra antes de tocar el export y se escribe después de comprobarlo: mientras exista,
# la carpeta está completa. La matriz la lee antes y después de copiar; si cambió por el
# camino, descarta esa vuelta y lo intenta en la siguiente.
#
# Variables (las fija la línea de cron):
#   LUNA_DIR   carpeta del repo de la luna (la que contiene normalizacion-backend/)
#   EMISOR     nodo_id de la luna (prefijo de sus snapshots), p. ej. kubo-luna-01
#
# Frecuencia: la matriz restaura el índice ENTERO cuando cambia. Para una luna que
# normaliza sin parar, cada 6 h; cada 20 min saturaría el disco de la matriz (01-10).
set -u
: "${LUNA_DIR:?falta LUNA_DIR}" "${EMISOR:?falta EMISOR}"
B="$LUNA_DIR/normalizacion-backend"
EXPORT="$LUNA_DIR/_export_snapshots"
MARCA=.azazel-listo
log() { echo "$(date -Is) [$EMISOR] $*"; }
env_de() { grep "^$1=" "$B/.env.prod" | cut -d= -f2-; }
# ¿La carpeta es un repositorio entero? `index.latest` guarda la generación vigente (8
# bytes, big-endian) y `index-<N>` es la lista de snapshots de esa generación.
generacion() { od -An -t u8 --endian=big "$1/index.latest" 2>/dev/null | tr -d ' '; }
repo_coherente() {
  local gen
  gen=$(generacion "$1")
  [ -n "$gen" ] && [ -f "$1/index-$gen" ]
}

log "0/3 flush del indice"
# El snapshot se lleva el último COMMIT de cada shard, no lo último indexado: sin este
# flush viaja lo de antes. 16-10: con el disco de kubo saturado el flush se cortó a los 30 s
# del cliente, el ciclo siguió, y el snapshot salió idéntico al anterior —«sin cambios»—
# con 504 000 documentos del índice activo sin llegar a la matriz.
FLUSH=$(docker exec normalizacion-api-1 python -c "
from normalizacion.core.config import cargar_config
from normalizacion.core.indexador.opensearch import crear_cliente
c=crear_cliente(cargar_config()); c.indices.refresh(index='archivos-*', request_timeout=900)
c.indices.flush(index='archivos-*', wait_if_ongoing=True, request_timeout=900); print('   flush ok')
" 2>&1 | grep -v '^{')
echo "$FLUSH" | tail -3
if ! echo "$FLUSH" | grep -q "flush ok"; then
  log "ERROR: el flush no terminó; un snapshot ahora llevaría lo de antes"
  exit 1
fi

log "1/3 snapshot"
# Con el código del REPO (montado), no con el horneado en la imagen de la API: un arreglo
# de `replicacion` llega sin reconstruir ni reiniciar la API (reiniciarla marca FALLIDA la
# corrida en curso). Misma configuración que la API, en un archivo que solo lee el dueño.
ENVR=$(mktemp)
chmod 600 "$ENVR"
trap 'rm -f "$ENVR"' EXIT
docker inspect normalizacion-api-1 --format '{{range .Config.Env}}{{println .}}{{end}}' \
  | grep -E '^NORM_' > "$ENVR"
SNAP=$(docker run --rm --network normalizacion_interna --env-file "$ENVR" \
  -v "$B/src:/app/src:ro" normalizacion-api norm replicar 2>&1)
echo "$SNAP" | grep -E "snapshot:|OK|FALL"
# Un snapshot fallido NO puede publicarse: el 02-10 el cliente cortó a los 30 s, el ciclo
# exportó el snapshot ANTERIOR y la matriz dijo «sin cambios» con 166 000 docs sin llegar.
if ! echo "$SNAP" | grep -q "snapshot:" || echo "$SNAP" | grep -q "FALL"; then
  log "ERROR: el snapshot no se tomó; no se publica uno viejo"
  exit 1
fi
ULTIMO=$(echo "$SNAP" | grep -oE "$EMISOR-[0-9]{8}-[0-9]{6}" | tail -1)

log "1b/3 purga (deja los 3 ultimos)"
docker exec -e EMISOR="$EMISOR" normalizacion-api-1 python -c "
import os
from normalizacion.core.config import cargar_config
from normalizacion.core import replicacion
from normalizacion.core.indexador.opensearch import crear_cliente
c=crear_cliente(cargar_config()); R=replicacion.REPOSITORIO; pref=os.environ['EMISOR']+'-'
mios=sorted(s['snapshot'] for s in c.transport.perform_request('GET','/_snapshot/'+R+'/_all',timeout=300).get('snapshots',[]) if s['snapshot'].startswith(pref))
for v in mios[:-3]:
    c.transport.perform_request('DELETE','/_snapshot/'+R+'/'+v,timeout=900); print('   purgado',v)
print('   vivos:', len(mios[-3:]))
" 2>&1 | grep -v '^{'

log "2/3 export del bucket"
mkdir -p "$EXPORT"
# Desde aquí la carpeta está «en obras»: sin marca, la matriz no copia.
rm -f "$EXPORT/$MARCA" "$EXPORT/$MARCA.tmp"
# mc en un contenedor de la red interna: la luna no publica MinIO al host. Credenciales
# por MC_HOST_ (variable), no en la línea de comandos. `--remove`: el export es el bucket
# tal cual, no su historia (kubo, 14-10: 321 GB de blobs de snapshots ya purgados).
docker run --rm --network normalizacion_interna -v "$EXPORT:/salida" \
  -e "MC_HOST_local=http://$(env_de NORM_MINIO_ROOT_USER):$(env_de NORM_MINIO_ROOT_PASSWORD)@minio:9000" \
  minio/mc mirror --overwrite --remove --quiet local/snapshots /salida >/dev/null 2>&1 \
  || { log "ERROR export del bucket"; exit 1; }
repo_coherente "$EXPORT" || { log "ERROR: el export no es un repositorio entero; no se publica"; exit 1; }

log "3/3 marca de listo"
GEN=$(generacion "$EXPORT")
BYTES=$(du -sb "$EXPORT" | cut -f1)
# Escrita aparte y renombrada: la matriz nunca lee una marca a medias.
printf 'generacion=%s\nsnapshot=%s\nbytes=%s\nfecha=%s\n' \
  "$GEN" "${ULTIMO:-desconocido}" "$BYTES" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$EXPORT/$MARCA.tmp" \
  && mv "$EXPORT/$MARCA.tmp" "$EXPORT/$MARCA" \
  || { log "ERROR: no se pudo escribir la marca"; exit 1; }
log "publicado: generacion $GEN, snapshot ${ULTIMO:-?}, $((BYTES / 1000000)) MB"
