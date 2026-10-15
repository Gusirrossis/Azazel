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
# En PARALELO. Una sola conexión TCP desde kubo cae a ~0,5 MB/s cuando hay pérdida de
# paquetes: medido el 13-10, 0,9 % de segmentos retransmitidos en el host y la conexión
# del rsync con cwnd 8 y 37 MB reenviados de 3,1 GB. El ciclo de la 01:00 del 14-10 pasó
# 13 h en este paso y se comió los de las 07:00 y las 13:00. Con 4 conexiones: 2,1-3,0
# MB/s en 3 de 3 pruebas, frente a 0,5 MB/s de una sola en 3 de 5. Los blobs de un
# snapshot nunca cambian de contenido, así que repartirlos no tiene orden que respetar.
LISTAS=$(mktemp -d)
trap 'rm -f "$ENVR"; rm -rf "$LISTAS"' EXIT
rsync -a --dry-run --out-format='%l %n' -e "$SSHP" "$EXPORT/" "root@$MATRIZ:$IMPORT/" > "$LISTAS/todo" \
  || { log "ERROR rsync (listado)"; exit 1; }
# Reparto por tamaño, de mayor a menor, al flujo con menos bytes (las carpetas, que
# acaban en «/», las crea rsync solo).
grep -v '/$' "$LISTAS/todo" | sort -rn | awk -v d="$LISTAS" -v n="$FLUJOS" '
  { m = 0; for (i = 1; i < n; i++) if (t[i] < t[m]) m = i
    t[m] += $1; c[m]++; sub(/^[0-9]+ /, ""); print > (d "/flujo" m) }
  END { for (i = 0; i < n; i++) if (c[i]) printf "   flujo %d: %d ficheros, %.1f MB\n", i, c[i], t[i] / 1e6 }'
pids=()
for f in "$LISTAS"/flujo*; do
  [ -e "$f" ] || continue
  rsync -az --files-from="$f" -e "$SSHP" "$EXPORT/" "root@$MATRIZ:$IMPORT/" & pids+=($!)
done
fallos=0
for p in ${pids[@]+"${pids[@]}"}; do wait "$p" || fallos=$((fallos + 1)); done
[ "$fallos" -gt 0 ] && log "   $fallos flujo(s) fallaron: el repaso los reenvía"
# Repaso en UN flujo: reenvía lo que un flujo no terminara (y es quien dice si el paso
# fue bien) y `--delete` quita de `_import` lo que la luna ya purgó.
log "3b/4 repaso y limpieza de _import"
rsync -az --delete -e "$SSHP" "$EXPORT/" "root@$MATRIZ:$IMPORT/" || { log "ERROR rsync"; exit 1; }

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
