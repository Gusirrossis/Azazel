#!/usr/bin/env bash
# La MATRIZ se trae la réplica de una LUNA: lee su marca, copia su carpeta de export con
# una llave que solo puede LEER esa carpeta, comprueba lo copiado, lo inyecta en su MinIO
# y restaura (blue/green, `replicacion.restaurar_ajenos` con `emisor`).
#
# Por qué al revés que antes. Hasta el 22-10 la luna empujaba: entraba como root en la
# matriz para inyectar y restaurar, así que quien tomara una luna tenía la matriz. Ahora
# las lunas no tienen llave ni camino de red hacia aquí (ufw solo deja SALIR a su :22). En
# la luna, la llave de la matriz va en ~/.ssh/authorized_keys como
#   restrict,from="162.35.188.181",command="/usr/bin/rrsync -ro <LUNA_DIR>/_export_snapshots" ssh-ed25519 …
# y no sirve para nada más. Lo único que cruza es esa carpeta, y lo que trae una luna
# comprometida no se cree: antes de usarlo se comprueba
#   · el tamaño, ANTES de copiar (TOPE_GB, MAX_FICHEROS), para que no llene el disco;
#   · que solo haya ficheros y carpetas: sin -l ni -D, rsync se salta enlaces simbólicos,
#     dispositivos y especiales, y un enlace no puede hacer que la matriz suba algo suyo;
#   · que sea un repositorio entero y de la generación que anunció la marca;
#   · que SOLO traiga snapshots e índices con el nombre de su emisor, aquí y otra vez al
#     restaurar: una luna no puede pisar lo que sirve la matriz de otra.
#
# Variables (las fija la línea de cron de root):
#   NOMBRE        kubo | storage → _import_<NOMBRE>, bucket snapshots-<NOMBRE>,
#                 repositorio azazel-snapshots-<NOMBRE>
#   EMISOR        nodo_id de la luna (kubo-luna-01, vps-storage-01)
#   ORIGEN        usuario@ip de la luna
#   LLAVE         llave privada (por defecto /root/.ssh/replica_<NOMBRE>)
#   CONOCIDOS     known_hosts con la llave de host de cada luna, fijada a mano
#   TOPE_GB       tamaño máximo que se acepta de la carpeta de la luna
#   MAX_FICHEROS  número máximo de ficheros
#   CODIGO        carpeta normalizacion-backend cuyo src/ se monta para restaurar: un
#                 arreglo llega sin reconstruir la API (que además perdió el 26-10 los
#                 parches en caliente al recrearse)
#   FLUJOS, LOTE_MB, LOTE_MAX_S, RONDAS  la cola de la copia (ver abajo)
set -u -o pipefail
: "${NOMBRE:?falta NOMBRE}" "${EMISOR:?falta EMISOR}" "${ORIGEN:?falta ORIGEN}"
[[ "$NOMBRE" =~ ^[a-z0-9-]{1,30}$ ]] || { echo "NOMBRE no valido"; exit 2; }
[[ "$EMISOR" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || { echo "EMISOR no valido"; exit 2; }
[[ "$ORIGEN" =~ ^[a-z_][a-z0-9_-]{0,31}@[0-9.]{7,15}$ ]] || { echo "ORIGEN no valido"; exit 2; }
LLAVE=${LLAVE:-/root/.ssh/replica_$NOMBRE}
CONOCIDOS=${CONOCIDOS:-/root/.ssh/known_hosts_lunas}
TOPE_GB=${TOPE_GB:-1500}
MAX_FICHEROS=${MAX_FICHEROS:-200000}
MAX_FICHERO=${MAX_FICHERO:-8G}
CODIGO=${CODIGO:-/srv/azazel-replica/normalizacion-backend}
ALIAS=${ALIAS:-archivos}
FLUJOS=${FLUJOS:-4}
LOTE_MB=${LOTE_MB:-128}
LOTE_MAX_S=${LOTE_MAX_S:-150}
RONDAS=${RONDAS:-40}
ESPERA_FALLO_MIN=${ESPERA_FALLO_MIN:-120}
IMPORT=/srv/azazel/_import_$NOMBRE
ESTADOS=/srv/azazel/_replica_estado
ESTADO=$ESTADOS/$NOMBRE
E=/srv/azazel/normalizacion-backend/.env.prod
MARCA=.azazel-listo
TOPE=$((TOPE_GB * 1000000000))
SSHP="ssh -i $LLAVE -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$CONOCIDOS -o ConnectTimeout=20 -o ServerAliveInterval=15 -o ServerAliveCountMax=4"
log() { echo "$(date -Is) [$NOMBRE] $*"; }
generacion() { od -An -t u8 --endian=big "$1/index.latest" 2>/dev/null | tr -d ' '; }
repo_coherente() {
  local gen
  gen=$(generacion "$1")
  [ -n "$gen" ] && [ -f "$1/index-$gen" ]
}
campo() { sed -nE "s/^$1=([A-Za-z0-9._:-]{1,80})\$/\1/p" "$2" | head -1; }
TMP=$(mktemp -d)
chmod 700 "$TMP"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$ESTADOS" "$IMPORT"
touch "$ESTADOS/$NOMBRE.visto"  # el cron corre, aunque no haya novedades
GEN=
# Un fallo que no se arregla reintentando (una comprobación, un restore) espera
# ESPERA_FALLO_MIN antes de volver con la MISMA generación. Uno de red, no: se reintenta
# en la siguiente vuelta del cron.
fin_fallo() { [ -n "$GEN" ] && echo "$GEN fallo $(date +%s)" > "$ESTADO"; exit 1; }

# La marca, con tope de tamaño: es un fichero de la luna como cualquier otro.
leer_marca() {
  rm -f "$1"
  timeout 120 rsync -t --max-size=4k -e "$SSHP" "$ORIGEN:$MARCA" "$1" 2>"$TMP/err_marca" && [ -s "$1" ]
}
if ! leer_marca "$TMP/marca"; then
  # Sin marca: la luna está exportando (la borra mientras) o aún no publicó. Normal.
  grep -q "No such file" "$TMP/err_marca" && exit 0
  log "AVISO: no se pudo leer la marca de la luna: $(tail -1 "$TMP/err_marca" | cut -c1-160)"
  exit 1
fi
GEN=$(campo generacion "$TMP/marca")
BYTES=$(campo bytes "$TMP/marca")
SNAP=$(campo snapshot "$TMP/marca")
if ! [[ "$GEN" =~ ^[0-9]{1,12}$ && "$BYTES" =~ ^[0-9]{1,16}$ ]]; then
  GEN=
  log "ERROR: marca mal formada; no se copia nada"
  exit 1
fi
if [ -f "$ESTADO" ]; then
  read -r EGEN ERES ETS < "$ESTADO"
  [ "${EGEN:-}" = "$GEN" ] && [ "${ERES:-}" = ok ] && exit 0  # ya está, sin novedades
  if [ "${EGEN:-}" = "$GEN" ] && [ "${ERES:-}" = fallo ] \
      && [ $(( $(date +%s) - ${ETS:-0} )) -lt $((ESPERA_FALLO_MIN * 60)) ]; then
    exit 0
  fi
fi
log "generacion $GEN de $EMISOR (snapshot ${SNAP:-?}, $((BYTES / 1000000)) MB anunciados)"
if [ "$BYTES" -gt "$TOPE" ]; then
  log "ERROR: la luna anuncia $((BYTES / 1000000000)) GB, mas que el tope de $TOPE_GB GB; no se copia"
  fin_fallo
fi

log "1/4 copia desde la luna ($FLUJOS flujos)"
# En PARALELO, por LOTES y por RONDAS. La salida de kubo pierde paquetes por RACHAS
# (14-10: de 16 conexiones nuevas seguidas, 1-6 y 14-16 no pasaron de 320 KB/s y 7-13
# fueron a 6-8 MB/s, con 0 % de pérdida en ping). Una conexión TCP que cae en racha mala
# se hunde y no se recupera: un rsync de una sola conexión tardó 13 h. Por eso lotes de
# LOTE_MB que toma cada conexión al quedar libre, cada intento en una conexión NUEVA y con
# LOTE_MAX_S de tope; tras cada ronda se vuelve a listar lo que falta y `--partial-dir`
# conserva lo ya recibido de un fichero cortado. Si en RONDAS rondas no termina, la vuelta
# falla sin tocar nada servido y la siguiente sigue donde esta lo dejó.
LISTAS=$TMP/listas
mkdir -p "$LISTAS"
traer_lote() {
  timeout "$LOTE_MAX_S" rsync -rtz --exclude="$MARCA" --exclude="$MARCA.tmp" \
    --max-size="$MAX_FICHERO" --partial-dir=.rsync-parcial --files-from="$1" \
    -e "$SSHP" "$ORIGEN:./" "$IMPORT/" 2>/dev/null
}
export -f traer_lote
export SSHP ORIGEN IMPORT LOTE_MAX_S MARCA MAX_FICHERO
ronda=0
medido=0
while :; do
  ronda=$((ronda + 1))
  if [ "$ronda" -gt "$RONDAS" ]; then
    log "ERROR copia: quedan ficheros tras $RONDAS rondas (lo copiado se conserva)"
    exit 1
  fi
  rm -f "$LISTAS"/lote*
  if ! timeout 300 rsync -rt --exclude="$MARCA" --exclude="$MARCA.tmp" --max-size="$MAX_FICHERO" \
      --dry-run --out-format='%l %n' -e "$SSHP" "$ORIGEN:./" "$IMPORT/" \
      > "$LISTAS/todo" 2> "$TMP/err_lista"; then
    log "   ronda $ronda: el listado no respondio ($(tail -1 "$TMP/err_lista" | cut -c1-120))"
    sleep 30
    continue
  fi
  if [ "$medido" = 0 ]; then
    # El tope, con lo que de verdad hay que copiar y no con lo que dice la marca. En el
    # primer listado que responda, no en la ronda 1: si esa falla, la 2 no se lo salta.
    medido=1
    FALTA=$(grep -v '/$' "$LISTAS/todo" | awk '{s += $1} END {printf "%d", s}')
    NFALTA=$(grep -vc '/$' "$LISTAS/todo")
    YA=$(du -sb "$IMPORT" | cut -f1)
    NYA=$(find "$IMPORT" -type f | wc -l)
    if [ $((FALTA + YA)) -gt "$TOPE" ] || [ $((NFALTA + NYA)) -gt "$MAX_FICHEROS" ]; then
      log "ERROR: copiarlo dejaria $(((FALTA + YA) / 1000000000)) GB y $((NFALTA + NYA)) ficheros (topes: $TOPE_GB GB, $MAX_FICHEROS); no se copia"
      fin_fallo
    fi
  fi
  # De mayor a menor, en lotes de hasta LOTE_MB (un fichero mayor va solo). Las carpetas,
  # que acaban en «/», las crea rsync.
  log "   ronda $ronda: quedan $(grep -v '/$' "$LISTAS/todo" | sort -rn | awk -v d="$LISTAS" -v tope=$((LOTE_MB * 1048576)) '
    { if (!n || (t + $1 > tope && c)) { if (f) close(f); n++; t = 0; c = 0; f = sprintf("%s/lote%05d", d, n) }
      t += $1; c++; s += $1; sub(/^[0-9]+ /, ""); print > f }
    END { printf "%d ficheros, %.1f MB, en %d lotes", NR, s / 1e6, n }')"
  [ -n "$(find "$LISTAS" -name 'lote*' -print -quit)" ] || break
  find "$LISTAS" -name 'lote*' | sort | xargs -r -P "$FLUJOS" -I{} bash -c 'traer_lote "$1"' _ {}
done
# Todo copiado. `--delete` quita lo que la luna ya purgó y las `.rsync-parcial` que
# quedaran: nada a medias llega a la inyección. También con tope y reintento.
for intento in 1 2 3 4 5; do
  timeout 600 rsync -rt --delete --exclude="$MARCA" --exclude="$MARCA.tmp" --max-size="$MAX_FICHERO" \
    -e "$SSHP" "$ORIGEN:./" "$IMPORT/" 2> "$TMP/err_limpia" && break
  if [ "$intento" = 5 ]; then
    log "ERROR copia (limpieza de _import): $(tail -1 "$TMP/err_limpia" | cut -c1-160)"
    exit 1
  fi
  sleep 30
done
# Si la luna volvió a exportar mientras se copiaba, lo copiado puede mezclar dos
# generaciones. Se descarta la vuelta; lo recibido sirve para la siguiente.
if ! leer_marca "$TMP/marca2" || ! cmp -s "$TMP/marca" "$TMP/marca2"; then
  log "la luna volvio a exportar durante la copia: esta vuelta se descarta"
  exit 1
fi

log "2/4 comprobaciones"
if [ "$(stat -c %s "$IMPORT/index.latest" 2>/dev/null)" != 8 ] || ! repo_coherente "$IMPORT"; then
  log "ERROR: la copia no es un repositorio entero"
  fin_fallo
fi
if [ "$(generacion "$IMPORT")" != "$GEN" ]; then
  log "ERROR: la copia es la generacion $(generacion "$IMPORT") y la marca dice $GEN"
  fin_fallo
fi
if [ -n "$(find "$IMPORT" ! -type f ! -type d -print -quit)" ]; then
  log "ERROR: en la copia hay algo que no es fichero ni carpeta"
  fin_fallo
fi
# La lista de snapshots e índices del repositorio (JSON): solo nombres, nunca datos.
if ! python3 -I - "$IMPORT/index-$GEN" "$EMISOR" "$ALIAS" <<'PY'; then
import json, os, re, sys
ruta, emisor, alias = sys.argv[1:4]
if os.path.getsize(ruta) > 100_000_000:
    print("   RECHAZADO: la lista del repositorio pesa demasiado")
    sys.exit(3)
with open(ruta, "rb") as f:
    datos = json.load(f)
pi = re.compile(re.escape(alias) + "-" + re.escape(emisor) + r"-\d{6}")
ps = re.compile(re.escape(emisor) + r"-\d{8}-\d{6}")
indices = list(datos.get("indices", {}))
snaps = [str(s.get("name", "")) for s in datos.get("snapshots", [])]
malos = [i for i in indices if not pi.fullmatch(i)] + [s for s in snaps if not ps.fullmatch(s)]
print(f"   repositorio: {len(snaps)} snapshots, {len(indices)} indices de {emisor}")
if malos or not snaps:
    print(f"   RECHAZADO: {len(malos)} nombres que no son de {emisor}" if malos else "   RECHAZADO: sin snapshots")
    for x in malos[:10]:
        print("     -", x[:80])
    sys.exit(3)
PY
  fin_fallo
fi

# Una restauración a la vez en toda la matriz: su disco es uno solo y mecánico.
exec 9> /tmp/azazel-restaurar.lock
if ! flock -w 21600 9; then
  log "ERROR: otra restauracion lleva 6 h ocupando la matriz; se reintenta luego"
  exit 1
fi

log "3/4 inyeccion en el MinIO de la matriz"
U=$(grep '^NORM_MINIO_ROOT_USER=' "$E" | cut -d= -f2-)
P=$(grep '^NORM_MINIO_ROOT_PASSWORD=' "$E" | cut -d= -f2-)
RED=$(docker inspect normalizacion-minio-1 -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' | awk '{print $1}')
# Solo los que faltan (sin --overwrite: en un repositorio de snapshots nada cambia de
# contenido) y el puntero `index.latest` AL FINAL, para que nunca apunte a un snapshot a
# medio subir (03-10: el `snapshot_missing`). Si no termina bien, no se restaura.
LOGI=$TMP/inyectar.log
for intento in 1 2 3; do
  if docker run --rm --network "$RED" -v "$IMPORT:/entrada:ro" \
      -e MU="$U" -e MP="$P" -e NOMBRE="$NOMBRE" --entrypoint sh minio/mc -c '
        mc alias set prod http://minio:9000 "$MU" "$MP" >/dev/null || exit 3
        mc mb -p "prod/snapshots-$NOMBRE" >/dev/null
        mc mirror --retry --quiet --exclude index.latest --exclude ".rsync-parcial/*" /entrada "prod/snapshots-$NOMBRE" || exit 4
        mc cp --quiet /entrada/index.latest "prod/snapshots-$NOMBRE/index.latest"' > "$LOGI" 2>&1; then
    log "   inyeccion ok (intento $intento)"
    break
  fi
  log "   inyeccion FALLO (intento $intento): $(tail -1 "$LOGI" | cut -c1-200)"
  if [ "$intento" = 3 ]; then
    log "ERROR: no se restaura con el bucket incompleto"
    exit 1
  fi
  sleep 60
done

log "4/4 restaurar (solo indices de $EMISOR)"
ENVR=$TMP/env
docker inspect normalizacion-api-1 --format '{{range .Config.Env}}{{println .}}{{end}}' \
  | grep -E '^NORM_' > "$ENVR"
docker run --rm --network "$RED" --env-file "$ENVR" -e NOMBRE="$NOMBRE" -e EMISOR="$EMISOR" \
  -v "$CODIGO/src:/app/src:ro" normalizacion-api python -c "
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
    c.transport.perform_request('PUT','/_snapshot/'+REPO,body={'type':'s3','settings':{'bucket':'snapshots-'+n,'client':'default','readonly':True}})
r = replicacion.restaurar_ajenos(config, c, refrescar=True, repositorio=REPO, emisor=os.environ['EMISOR'])
print('   restaurado:', r.indices, '| sin_cambios:', r.sin_cambios, '| ok:', r.ok, '| motivo:', r.motivo)
if r.rechazados:
    print('   RECHAZADOS (no son de', os.environ['EMISOR'] + '):', [x[:80] for x in r.rechazados[:10]])
# El recuento es INFORMATIVO: no puede tumbar un ciclo que ya restauró bien.
try:
    c.indices.refresh(index=config.indice_alias+'-*', request_timeout=300)
    print('   docs en el alias:', c.count(index=config.indice_alias, request_timeout=300)['count'])
except Exception as exc:
    print('   docs en el alias: (no se pudo contar:', type(exc).__name__ + ')')
raise SystemExit(0 if r.ok else 2)
" > "$TMP/restaurar.log" 2>&1
rc=$?
grep -v '^{' "$TMP/restaurar.log" | tail -8
if [ "$rc" != 0 ]; then
  log "ERROR al restaurar (codigo $rc): ciclo NO completado"
  fin_fallo
fi
# Fuera del bucket lo que la luna ya purgó. SOLO tras un restore bueno: hasta subir
# `index.latest` el repositorio vigente es el viejo y sus blobs hacen falta. Si falla, el
# ciclo no: lo servido ya está bien.
if docker run --rm --network "$RED" -v "$IMPORT:/entrada:ro" \
    -e MU="$U" -e MP="$P" -e NOMBRE="$NOMBRE" --entrypoint sh minio/mc -c '
      mc alias set prod http://minio:9000 "$MU" "$MP" >/dev/null || exit 3
      mc mirror --retry --quiet --remove --exclude ".rsync-parcial/*" /entrada "prod/snapshots-$NOMBRE"' > "$LOGI" 2>&1; then
  log "   limpieza del bucket ok"
else
  log "   limpieza del bucket FALLO (lo restaurado no se toca): $(tail -1 "$LOGI" | cut -c1-200)"
fi
echo "$GEN ok $(date +%s)" > "$ESTADO"
log "ciclo terminado"
