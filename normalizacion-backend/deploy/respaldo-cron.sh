#!/usr/bin/env bash
# Envoltorio de cron para `respaldo.sh`. El script de respaldo hace su trabajo bien;
# lo que le falta para correr desatendido a las 3 de la mañana es todo lo de aquí:
#
#   · PATH. Cron arranca con un PATH mínimo que NO incluye /usr/bin en todas las
#     distribuciones, y `respaldo.sh` invoca `docker`. Sin esto falla con «command
#     not found» y el respaldo no existe hasta que alguien lo note.
#   · Un cerrojo. Si un volcado tarda más de un día, o alguien lanza uno a mano
#     mientras corre el de cron, dos `pg_dump` simultáneos duplican la carga sobre
#     Postgres y llenan /tmp con dos copias de 1,7 GB.
#   · Rastro con fecha. Cron manda la salida al correo de root, que en un VPS no lee
#     nadie nunca. Un fallo que solo existe en el correo de root es un fallo invisible.
#   · Un marcador de estado. El log dice qué pasó; el marcador dice, en una línea y
#     sin parsear nada, si el último respaldo salió bien y cuándo. Es lo que hay que
#     mirar para responder «¿estamos respaldados?».
#
# Limitación honesta: esto NO alerta. Deja el fallo escrito y con fecha, pero nadie
# recibe un aviso. Para que un respaldo fallido salte en Grafana haría falta exponer
# el marcador como métrica; mientras no exista, revisar el estado es trabajo manual.
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

AQUI="$(cd "$(dirname "$0")" && pwd)"
LOG="${NORM_RESPALDO_LOG:-/var/log/azazel-respaldo.log}"
ESTADO="${NORM_RESPALDO_ESTADO:-/var/lib/azazel/respaldo-estado}"
CERROJO=/var/lock/azazel-respaldo.lock

mkdir -p "$(dirname "$ESTADO")" "$(dirname "$LOG")"

exec 9>"$CERROJO"
if ! flock -n 9; then
  echo "$(date -u +%FT%TZ) [cron] ya hay un respaldo en curso; no arranco otro" >>"$LOG"
  exit 0
fi

{
  echo "===== $(date -u +%FT%TZ) inicio ====="
  codigo=0
  bash "$AQUI/respaldo.sh" || codigo=$?
  if [ "$codigo" -eq 0 ]; then
    echo "$(date -u +%FT%TZ) OK"
    printf 'ok %s %s\n' "$(date -u +%s)" "$(date -u +%FT%TZ)" >"$ESTADO"
  else
    echo "$(date -u +%FT%TZ) FALLO codigo=$codigo"
    printf 'FALLO %s %s codigo=%s\n' "$(date -u +%s)" "$(date -u +%FT%TZ)" "$codigo" >"$ESTADO"
  fi

  # El log crece unas líneas por día; recortarlo evita que dentro de dos años haya
  # que abrir un fichero de megas para ver si el respaldo de anoche salió bien.
  #
  # Se vacía y se reescribe el MISMO fichero en vez de sustituirlo con `mv`: este
  # bloque tiene el log abierto en modo append, y un `mv` dejaría ese descriptor
  # apuntando al inodo viejo ya desenlazado — todo lo que se escribiera después se
  # perdería sin dar error. Hoy no hay nada detrás, pero no quiero dejar puesta esa
  # trampa para quien añada una línea al final.
  if [ "$(wc -l <"$LOG" 2>/dev/null || echo 0)" -gt 5000 ]; then
    if tail -n 2000 "$LOG" >"$LOG.tmp"; then
      cat "$LOG.tmp" >"$LOG" && rm -f "$LOG.tmp"
    fi
  fi
  exit "$codigo"
} >>"$LOG" 2>&1
