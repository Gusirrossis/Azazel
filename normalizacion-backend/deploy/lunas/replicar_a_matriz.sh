#!/usr/bin/env bash
# Réplica de una LUNA hacia la matriz: flush → snapshot → purga → export del bucket →
# rsync → inyectar + restaurar en la matriz (blue/green, ver replicacion.restaurar_ajenos).
#
# Antes vivía copiado a mano en cada luna (/opt/azazel-luna, /srv/azazel) y las copias
# divergían. Aquí, versionado y por variables. Sin credenciales escritas: las de la luna
# se leen de su .env.prod y las de la matriz, ALLÍ, de su .env.prod.
#
# Variables (las fija la línea de cron):
#   LUNA_DIR   carpeta del repo de la luna (la que contiene normalizacion-backend/)
#   EMISOR     nodo_id de la luna (prefijo de sus snapshots), p. ej. kubo-luna-01
#   NOMBRE     nombre corto para la matriz: _import_<NOMBRE>, snapshots-<NOMBRE>,
#              repositorio azazel-snapshots-<NOMBRE>
#   MATRIZ     IP de la matriz (162.35.188.181)
#   FLUJOS     conexiones en paralelo del rsync (4)
#   LOTE_MB    tamaño de cada lote de la cola del rsync (128)
#   LOTE_MAX_S tope de un intento antes de cortarlo y reintentarlo en otra conexión (150)
#   RONDAS     rondas de la cola antes de dar el ciclo por fallido (40)
#
# Ojo con la frecuencia: restaurar_ajenos restaura el índice ENTERO cuando cambia. Para
# una luna que normaliza sin parar, una vez al día y de noche; cada 20 min saturaría el
# disco de la matriz (medido el 01-10: un ciclo de 3 h atascado).
set -u
: "${LUNA_DIR:?falta LUNA_DIR}" "${EMISOR:?falta EMISOR}" "${NOMBRE:?falta NOMBRE}"
MATRIZ=${MATRIZ:-162.35.188.181}
FLUJOS=${FLUJOS:-4}
B="$LUNA_DIR/normalizacion-backend"
EXPORT="$LUNA_DIR/_export_snapshots"
IMPORT="/srv/azazel/_import_$NOMBRE"
LLAVE=$HOME/.ssh/azazel_replica
SSHP="ssh -i $LLAVE -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20"
log() { echo "$(date -Is) [$EMISOR] $*"; }
env_de() { grep "^$1=" "$B/.env.prod" | cut -d= -f2-; }
# ¿La carpeta es un repositorio entero? `index.latest` guarda la generación vigente (8
# bytes, big-endian) y `index-<N>` es la lista de snapshots de esa generación. Sin los
# dos, un `--delete`/`--remove` aguas abajo borraría lo que sí sirve.
repo_coherente() {
  local gen
  gen=$(od -An -t u8 --endian=big "$1/index.latest" 2>/dev/null | tr -d ' ')
  [ -n "$gen" ] && [ -f "$1/index-$gen" ]
}

log "0/4 flush del indice"
docker exec normalizacion-api-1 python -c "
from normalizacion.core.config import cargar_config
from normalizacion.core.indexador.opensearch import crear_cliente
c=crear_cliente(cargar_config()); c.indices.refresh(index='archivos-*')
c.indices.flush(index='archivos-*', wait_if_ongoing=True); print('   flush ok')
" 2>&1 | grep -v '^{'

log "1/4 snapshot"
# Con el código del REPO (montado), no con el horneado en la imagen de la API: un arreglo
# de `replicacion` llega sin reconstruir ni reiniciar la API, y reiniciarla marca FALLIDA
# la corrida en curso. Misma configuración que la API, en un archivo que solo lee el
# dueño y se borra al salir.
ENVR=$(mktemp)
chmod 600 "$ENVR"
trap 'rm -f "$ENVR"' EXIT
docker inspect normalizacion-api-1 --format '{{range .Config.Env}}{{println .}}{{end}}' \
  | grep -E '^NORM_' > "$ENVR"
SNAP=$(docker run --rm --network normalizacion_interna --env-file "$ENVR" \
  -v "$B/src:/app/src:ro" normalizacion-api norm replicar 2>&1)
echo "$SNAP" | grep -E "snapshot:|OK|FALL"
# Un snapshot fallido NO puede acabar en «ciclo terminado»: el 02-10 el cliente cortó a
# los 30 s, el ciclo exportó el snapshot ANTERIOR y la matriz dijo «sin cambios» con
# 166 000 documentos sin llegar.
if ! echo "$SNAP" | grep -q "snapshot:" || echo "$SNAP" | grep -q "FALL"; then
  log "ERROR: el snapshot no se tomó; no se replica uno viejo"
  exit 1
fi

log "1b/4 purga (deja los 3 ultimos)"
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

log "2/4 export del bucket"
mkdir -p "$EXPORT"
# mc en un contenedor de la red interna: la luna no publica MinIO al host. Credenciales
# por MC_HOST_ (variable), no en la línea de comandos.
#
# `--remove`: el export es el bucket tal cual, no su historia. Sin él, cada purga dejaba
# aquí los blobs de los snapshots borrados (kubo, 14-10: 321 GB y ~60 GB/día) y el rsync
# los llevaba también a `_import` y al bucket de la matriz.
docker run --rm --network normalizacion_interna -v "$EXPORT:/salida" \
  -e "MC_HOST_local=http://$(env_de NORM_MINIO_ROOT_USER):$(env_de NORM_MINIO_ROOT_PASSWORD)@minio:9000" \
  minio/mc mirror --overwrite --remove --quiet local/snapshots /salida >/dev/null 2>&1 \
  || { log "ERROR export del bucket"; exit 1; }
repo_coherente "$EXPORT" || { log "ERROR: el export no es un repositorio entero; no se replica"; exit 1; }

log "3/4 rsync a la matriz ($FLUJOS flujos)"
# En PARALELO, por LOTES y por RONDAS. La salida de kubo hacia la matriz pierde paquetes
# por RACHAS: el 14-10, de 16 conexiones nuevas de 8 MB seguidas, 1-6 y 14-16 no pasaron
# de 320 KB/s y 7-13 fueron a 5,8-7,8 MB/s; con 0 % de pérdida en ping y a la vez que
# otra ruta (kubo -> fuera) iba bien en 7 de 8. Una conexión TCP que cae en una racha
# mala se hunde (cwnd 1) y no se recupera aunque la racha pase: el rsync de una sola
# conexión del ciclo de la 01:00 del 14-10 tardó 13 h y se comió los dos siguientes, y de
# 4 conexiones fijas dos se quedaron a ~70 KB/s.
#
# Por eso: lotes de LOTE_MB que cada conexión toma al quedar libre, cada intento en una
# conexión NUEVA y con LOTE_MAX_S de tope (con 10 min, un lote que arrancaba en racha
# mala se quedaba hundido 10 min). Tras cada ronda se vuelve a listar lo que falta;
# `--partial-dir` conserva lo ya enviado de un fichero cortado (llegan a 1 GiB). Si en
# RONDAS rondas no termina, el ciclo falla sin tocar la matriz y el siguiente sigue donde
# este lo dejó, en vez de colgarse horas. Los blobs de un snapshot nunca cambian de
# contenido: el orden da igual.
LOTE_MB=${LOTE_MB:-128}
LOTE_MAX_S=${LOTE_MAX_S:-150}
RONDAS=${RONDAS:-40}
LISTAS=$(mktemp -d)
trap 'rm -f "$ENVR"; rm -rf "$LISTAS"' EXIT
subir_lote() {
  timeout "$LOTE_MAX_S" rsync -az --partial-dir=.rsync-parcial --files-from="$1" \
    -e "$SSHP" "$EXPORT/" "root@$MATRIZ:$IMPORT/" 2>/dev/null
}
export -f subir_lote
export SSHP EXPORT MATRIZ IMPORT LOTE_MAX_S
ronda=0
while :; do
  ronda=$((ronda + 1))
  [ "$ronda" -gt "$RONDAS" ] && { log "ERROR rsync: quedan ficheros tras $RONDAS rondas (lo enviado se conserva)"; exit 1; }
  rm -f "$LISTAS"/lote*
  if ! timeout 300 rsync -a --dry-run --out-format='%l %n' -e "$SSHP" "$EXPORT/" \
      "root@$MATRIZ:$IMPORT/" > "$LISTAS/todo" 2>/dev/null; then
    log "   ronda $ronda: el listado no respondió"; sleep 30; continue
  fi
  # De mayor a menor, en lotes de hasta LOTE_MB (un fichero mayor va solo). Las carpetas,
  # que acaban en «/», las crea rsync.
  log "   ronda $ronda: quedan $(grep -v '/$' "$LISTAS/todo" | sort -rn | awk -v d="$LISTAS" -v tope=$((LOTE_MB * 1048576)) '
    { if (!n || (t + $1 > tope && c)) { if (f) close(f); n++; t = 0; c = 0; f = sprintf("%s/lote%05d", d, n) }
      t += $1; c++; s += $1; sub(/^[0-9]+ /, ""); print > f }
    END { printf "%d ficheros, %.1f MB, en %d lotes", NR, s / 1e6, n }')"
  [ -n "$(find "$LISTAS" -name 'lote*' -print -quit)" ] || break
  find "$LISTAS" -name 'lote*' | sort | xargs -r -P "$FLUJOS" -I{} bash -c 'subir_lote "$1"' _ {}
done
# Todo enviado. `--delete` quita de `_import` lo que la luna ya purgó y las
# `.rsync-parcial` que quedaran: nada a medias llega a la inyección. No transfiere
# datos, pero también va con tope y reintento: una racha mala lo colgaría igual.
log "3b/4 limpieza de _import"
for intento in 1 2 3 4 5; do
  timeout 600 rsync -az --delete -e "$SSHP" "$EXPORT/" "root@$MATRIZ:$IMPORT/" && break
  [ "$intento" = 5 ] && { log "ERROR rsync (limpieza de _import)"; exit 1; }
  sleep 30
done

log "4/4 inyectar + restaurar + entidades"
if ! $SSHP "root@$MATRIZ" NOMBRE="$NOMBRE" bash -s <<'REMOTO'
set -u -o pipefail
E=/srv/azazel/normalizacion-backend/.env.prod
IMP=/srv/azazel/_import_$NOMBRE
repo_coherente() {
  local gen
  gen=$(od -An -t u8 --endian=big "$1/index.latest" 2>/dev/null | tr -d ' ')
  [ -n "$gen" ] && [ -f "$1/index-$gen" ]
}
repo_coherente "$IMP" || { echo "   ERROR: $IMP no es un repositorio entero"; exit 1; }
U=$(grep '^NORM_MINIO_ROOT_USER=' "$E" | cut -d= -f2-)
P=$(grep '^NORM_MINIO_ROOT_PASSWORD=' "$E" | cut -d= -f2-)
RED=$(docker inspect normalizacion-minio-1 -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}')
# Credenciales por variables y `mc alias set`, no dentro de una URL: la contraseña de la
# matriz podría llevar @, : o / y romperla.
#
# La inyección TIENE que terminar bien antes de restaurar. Antes su salida iba a
# /dev/null y nadie miraba el código: el 02-10 un `mc mirror` se quedó a medias con el
# disco de la matriz saturado y la restauración arrancó igual → 404 NoSuchKey de un blob
# de 103 MB, el índice nuevo en ROJO y el clúster entero en rojo. Ahora se reintenta y,
# si no lo logra, el ciclo se para aquí: lo que ya sirve sigue intacto.
#
# En un repositorio de snapshots nada cambia de contenido: cada archivo nace con nombre
# nuevo, salvo `index.latest`, el puntero al último. Por eso se suben SOLO los que faltan
# (sin --overwrite), con reintento por objeto, y el puntero AL FINAL: nunca apunta a un
# snapshot a medio subir. 03-10: con --overwrite y en cualquier orden, un corte a mitad de
# un .part1 de 158 MB (IncompleteBody) tumbó la copia, cada reintento re-subió 23,5 GiB, y
# un puntero subido antes que sus datos es justo el `snapshot_missing` de Lilith.
LOGI=/tmp/inyectar_$NOMBRE.log
for intento in 1 2 3; do
  if docker run --rm --network "$RED" -v "/srv/azazel/_import_$NOMBRE:/entrada:ro" \
      -e MU="$U" -e MP="$P" -e NOMBRE="$NOMBRE" --entrypoint sh minio/mc -c '
        mc alias set prod http://minio:9000 "$MU" "$MP" >/dev/null || exit 3
        mc mb -p "prod/snapshots-$NOMBRE" >/dev/null
        mc mirror --retry --quiet --exclude index.latest /entrada "prod/snapshots-$NOMBRE" || exit 4
        mc cp --quiet /entrada/index.latest "prod/snapshots-$NOMBRE/index.latest"' > "$LOGI" 2>&1; then
    echo "   inyeccion ok (intento $intento)"; break
  fi
  echo "   inyeccion FALLO (intento $intento): $(tail -1 "$LOGI" | cut -c1-200)"
  [ "$intento" = 3 ] && { echo "   ERROR: no se restaura con el bucket incompleto"; exit 1; }
  sleep 60
done
docker exec -e NOMBRE="$NOMBRE" normalizacion-api-1 python -c "
import os
from normalizacion.core.config import cargar_config
from normalizacion.core import replicacion
from normalizacion.core.indexador.opensearch import crear_cliente
config=cargar_config(); c=crear_cliente(config); n=os.environ['NOMBRE']
REPO='azazel-snapshots-'+n
# Registrar falla con 500 'repository is currently used' si hay un restore en curso:
# solo se registra si falta.
try:
    c.transport.perform_request('GET','/_snapshot/'+REPO); ya=True
except Exception:
    ya=False
if not ya:
    c.transport.perform_request('PUT','/_snapshot/'+REPO,body={'type':'s3','settings':{'bucket':'snapshots-'+n,'client':'default'}})
r = replicacion.restaurar_ajenos(config, c, refrescar=True, repositorio=REPO)
print('   restaurado:', r.indices, '| sin_cambios:', r.sin_cambios, '| ok:', r.ok, '| motivo:', r.motivo)
# El recuento es INFORMATIVO: no puede tumbar un ciclo que ya restauró bien. El 06-10 la
# matriz, ocupada restaurando el índice de kubo, no respondió al refresh en 30 s y el
# ciclo de Lilith salió «NO completado» con ok=True y todo sin cambios.
try:
    c.indices.refresh(index=config.indice_alias+'-*', request_timeout=300)
    print('   docs en el alias:', c.count(index=config.indice_alias, request_timeout=300)['count'])
except Exception as exc:
    print('   docs en el alias: (no se pudo contar:', type(exc).__name__ + ')')
raise SystemExit(0 if r.ok else 2)  # un restore fallido tiene que verse en el codigo de salida
" 2>&1 | grep -v '^{'
rc=$?
# Limpieza del bucket: fuera lo que la luna ya purgó. SOLO tras un restore bueno y nunca
# antes: hasta subir `index.latest` el repositorio vigente es el viejo y sus blobs hacen
# falta. Ya restaurado, un índice no depende del repositorio (el restore copia los
# ficheros al nodo) y el «sin cambios» compara con la huella guardada, no con el snapshot
# del que salió. Si falla, el ciclo no: lo servido ya está bien.
if [ "$rc" = 0 ]; then
  if docker run --rm --network "$RED" -v "$IMP:/entrada:ro" \
      -e MU="$U" -e MP="$P" -e NOMBRE="$NOMBRE" --entrypoint sh minio/mc -c '
        mc alias set prod http://minio:9000 "$MU" "$MP" >/dev/null || exit 3
        mc mirror --retry --quiet --remove /entrada "prod/snapshots-$NOMBRE"' > "$LOGI" 2>&1; then
    echo "   limpieza del bucket ok"
  else
    echo "   limpieza del bucket FALLO (lo restaurado no se toca): $(tail -1 "$LOGI" | cut -c1-200)"
  fi
fi
exit "$rc"
REMOTO
then
  log "ERROR en la matriz (ver arriba): ciclo NO completado"; exit 1
fi
log "ciclo terminado"
