#!/usr/bin/env bash
# Centinela de una LUNA (cron cada 2 min). Vigila lo propio y protege al anfitrión:
# si la luna ahoga a la máquina que la hospeda, el problema es la luna.
#
#   - Alerta de memoria, contenedores caídos o enfermos y carga.
#   - FRENO: disco raíz ≥ 93 % → pausa la ingesta directo en Postgres (no a través de la
#     API, que es lo primero que se cuelga). Se reanuda a mano con `norm reanudar`.
#   - RELANZA la corrida si su contenedor murió sin terminar, como mucho una vez por
#     hora: un fallo que se repite al instante no puede convertirse en un bucle.
#
# Variables (las fija la línea de cron): LUNA_DIR, EMISOR, RUTA (dentro del
# contenedor, p. ej. /datos/kubomamalon), DISCO_ID, WORKERS (3), y los FRENO_* de
# deploy/lanzar_corrida.sh.
set -u
: "${LUNA_DIR:?falta LUNA_DIR}" "${EMISOR:?falta EMISOR}" "${RUTA:?falta RUTA}" "${DISCO_ID:?falta DISCO_ID}"
B="$LUNA_DIR/normalizacion-backend"
LOGS="$LUNA_DIR/logs"; mkdir -p "$LOGS"
ALERTAS="$LOGS/centinela-alertas.log"
TS=$(date -Is)
alerta() { echo "$TS [$EMISOR] $*" >> "$ALERTAS"; }
# Usuario y base, del propio contenedor (cambian de nodo a nodo).
PGU=$(docker exec normalizacion-postgres-1 printenv POSTGRES_USER 2>/dev/null)
PGD=$(docker exec normalizacion-postgres-1 printenv POSTGRES_DB 2>/dev/null)
sql() { docker exec normalizacion-postgres-1 psql -U "$PGU" -d "$PGD" -tAc "$1" 2>/dev/null; }

docker stats --no-stream --format '{{.Name}}|{{.MemPerc}}|{{.MemUsage}}' 2>/dev/null \
| while IFS='|' read -r n p u; do
    v=${p%\%}; v=${v%.*}; [ -z "$v" ] && continue
    [ "$v" -ge 85 ] && alerta "MEMORIA $n al $p ($u)"
  done
docker ps -a --format '{{.Names}}|{{.Status}}' 2>/dev/null | grep -E '^normalizacion-' \
| while IFS='|' read -r n s; do
    case "$s" in
      Up*unhealthy*) alerta "SALUD $n: $s" ;;
      Up*) : ;;
      *) alerta "CAIDO $n: $s" ;;
    esac
  done

USO=$(df / | awk 'NR==2{print $5}' | tr -d '%')
PAUSADO=$(sql "SELECT coalesce((SELECT valor FROM control WHERE clave = 'pausado'), 'false')")
if [ "$USO" -ge 93 ] && [ "$PAUSADO" != "true" ]; then
  if sql "INSERT INTO control (clave, valor) VALUES ('pausado', 'true') ON CONFLICT (clave) DO UPDATE SET valor = 'true', actualizado_en = now()" >/dev/null; then
    alerta "FRENO: disco al ${USO}% — ingesta PAUSADA (reanudar con: docker exec normalizacion-api-1 norm reanudar)"
    PAUSADO=true
  else
    alerta "FRENO FALLO con el disco al ${USO}%: la ingesta SIGUE"
  fi
fi

# Relanzar la corrida si murió sin terminar. Nunca con el freno puesto (terminaría al
# instante y se relanzaría cada 2 min) ni si su contenedor sigue vivo.
if [ "$PAUSADO" != "true" ] && ! docker ps -q -f name='^norm-corrida$' | grep -q .; then
  ULTIMA=$(sql "SELECT estado FROM corridas ORDER BY id DESC LIMIT 1")
  PEND=$(sql "SELECT count(*) FROM archivos WHERE estado IN ('PENDIENTE', 'PRECALIFICADO', 'EN_PROCESO')")
  if [ "$ULTIMA" = "EN_CURSO" ] || { [ "$ULTIMA" = "FALLIDA" ] && [ "${PEND:-0}" -gt 0 ]; }; then
    SELLO="$LOGS/.ultimo_relanzamiento"
    if [ ! -f "$SELLO" ] || [ $(( $(date +%s) - $(stat -c %Y "$SELLO") )) -ge 3600 ]; then
      [ "$ULTIMA" = "EN_CURSO" ] && sql "UPDATE corridas SET estado = 'FALLIDA', terminada_en = now(), error = 'contenedor muerto: lo relanza el centinela' WHERE estado = 'EN_CURSO'" >/dev/null
      docker rm norm-corrida >/dev/null 2>&1
      touch "$SELLO"
      if (cd "$B" && bash deploy/lanzar_corrida.sh "$RUTA" "$DISCO_ID" "${WORKERS:-3}" >> "$LOGS/lanzar.log" 2>&1); then
        alerta "RELANZADA la corrida (la anterior: $ULTIMA, pendientes: ${PEND:-?})"
      else
        alerta "RELANZAR FALLO (ver $LOGS/lanzar.log)"
      fi
    fi
  fi
fi

docker exec normalizacion-api-1 python -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/salud',timeout=8)" >/dev/null 2>&1 || alerta "API de la luna no responde"
echo "$TS load=$(cut -d' ' -f1-3 /proc/loadavg) mem=$(free -g | awk 'NR==2{print $3"/"$2"GB"}') disco=${USO}% pausado=${PAUSADO:-?}" >> "$LOGS/centinela.log"
tail -n 5000 "$LOGS/centinela.log" > "$LOGS/centinela.log.tmp" && mv "$LOGS/centinela.log.tmp" "$LOGS/centinela.log"
