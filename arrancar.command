#!/bin/bash
# ============================================================================
# Azazel — ARRANQUE RÁPIDO en la Mac (nativo, sin Docker).
#
# Para qué: tras un corte de luz o una actualización del sistema, levanta TODO
# de un doble clic. Cuando termine, abre el navegador y pulsa «Re-indexar» para
# reanudar la indexación donde quedó (el trabajo NO se pierde: la cola es durable
# y el catálogo es incremental).
#
# Cómo: doble clic en este archivo desde el Finder. (La primera vez, si el Mac
# pregunta, autoriza ejecutarlo; o en Terminal: chmod +x arrancar.command)
# ============================================================================
set -uo pipefail
cd "$(dirname "$0")"
ROOT="$(pwd)"
LOGS="$HOME/azazel-logs"; mkdir -p "$LOGS"

echo "▶ Azazel — arrancando…"
echo

# 0) Acotar el HEAP de OpenSearch (JVM) ANTES de arrancarlo. Sin tope, OpenSearch
#    reclama hasta la mitad de la RAM y compite con Python → la Mac se satura y se
#    cae el panel. Dejamos un heap modesto (≈1/8 de la RAM, entre 1 y 4 GB) vía un
#    drop-in en jvm.options.d (lo respeta en cada reinicio del servicio).
OS_CONF="$(brew --prefix 2>/dev/null)/etc/opensearch"
if [ -d "$OS_CONF" ]; then
  RAM_MB=$(( $(sysctl -n hw.memsize 2>/dev/null || echo 8589934592) / 1048576 ))
  HEAP_MB=$(( RAM_MB / 8 ));  [ "$HEAP_MB" -lt 1024 ] && HEAP_MB=1024;  [ "$HEAP_MB" -gt 4096 ] && HEAP_MB=4096
  mkdir -p "$OS_CONF/jvm.options.d"
  printf -- "-Xms%sm\n-Xmx%sm\n" "$HEAP_MB" "$HEAP_MB" > "$OS_CONF/jvm.options.d/azazel-heap.options"
  echo "• OpenSearch heap acotado a ${HEAP_MB} MB (de ${RAM_MB} MB de RAM)"
fi

# 1) Bases como servicios de Homebrew (idempotente: si ya viven, no hace nada).
echo "• Bases de datos (Postgres + OpenSearch)…"
brew services start postgresql@16 >/dev/null 2>&1 || true
brew services start opensearch    >/dev/null 2>&1 || true

# 2) Esperar a que respondan antes de seguir.
printf "  Postgres "
until pg_isready -q 2>/dev/null; do sleep 1; printf "."; done; echo " ✓"
printf "  OpenSearch "
until curl -fs http://localhost:9200 >/dev/null 2>&1; do sleep 2; printf "."; done; echo " ✓"

# 3) Backend al día — cubre el caso "actualizamos la versión" (todo idempotente).
cd "$ROOT/normalizacion-backend"

# ⚙K16 — qué nodo es esta máquina. Sale del .env (NORM_DESPLIEGUE__PERFIL); si no
# está, es `local`, que es el comportamiento de siempre. Se muestra porque un nodo
# con el perfil equivocado NO falla: arranca perfectamente y hace lo que no le toca
# (p. ej. resolver entidades que ya resuelve el otro nodo, pisándose en el AEB).
PERFIL="$( (grep -E '^NORM_DESPLIEGUE__PERFIL=' .env 2>/dev/null || echo '=local') | tail -1 | cut -d= -f2 )"
NODO="$( (grep -E '^NORM_DESPLIEGUE__NODO_ID=' .env 2>/dev/null || echo '=local') | tail -1 | cut -d= -f2 )"
echo "• Perfil de despliegue: ${PERFIL:-local}  ·  nodo: ${NODO:-local}"

echo "• Backend (dependencias, esquema, índice)…"
uv sync --extra workers --extra api >/dev/null 2>&1 || true
uv run alembic upgrade head >/dev/null 2>&1 || true
uv run norm aplicar-indice  >/dev/null 2>&1 || true

# Diagnóstico del nodo: stores alcanzables, seguridad, réplica. No aborta el
# arranque —puede haber avisos legítimos— pero deja el estado a la vista.
uv run norm doctor 2>&1 | sed 's/^/    /' || true

# 4) API (en segundo plano; sobrevive aunque cierres esta ventana).
if curl -fs http://localhost:8000/openapi.json >/dev/null 2>&1; then
  echo "• API ya estaba arriba → http://localhost:8000"
else
  nohup uv run norm api > "$LOGS/api.log" 2>&1 &
  echo "• API → http://localhost:8000   (log: $LOGS/api.log)"
fi

# 5) Front. Acotamos el heap de Node (Vite dev) — no necesita más y así no compite
#    por RAM con la ingesta y OpenSearch en la misma Mac.
cd "$ROOT/normalizacion-front"
export NODE_OPTIONS="--max-old-space-size=512"
[ -d node_modules ] || npm install >/dev/null 2>&1
if curl -fs http://localhost:5173 >/dev/null 2>&1; then
  echo "• Front ya estaba arriba → http://localhost:5173"
else
  nohup npm run dev > "$LOGS/front.log" 2>&1 &
  echo "• Front → http://localhost:5173  (log: $LOGS/front.log)"
fi

# 6) Esperar a que el front responda y abrir el navegador.
printf "• Abriendo el navegador "
for _ in $(seq 1 25); do
  curl -fs http://localhost:5173 >/dev/null 2>&1 && break
  sleep 1; printf "."
done
echo " ✓"
open http://localhost:5173

echo
echo "═══════════════════════════════════════════════════════════════"
echo "✓ Listo. En el navegador, pulsa «Re-indexar» para reanudar."
echo "  (Para apagar la API y el front: doble clic en apagar.command)"
echo "═══════════════════════════════════════════════════════════════"
