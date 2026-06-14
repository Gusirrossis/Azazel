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
echo "• Backend (dependencias, esquema, índice)…"
uv sync --extra workers --extra api >/dev/null 2>&1 || true
uv run alembic upgrade head >/dev/null 2>&1 || true
uv run norm aplicar-indice  >/dev/null 2>&1 || true

# 4) API (en segundo plano; sobrevive aunque cierres esta ventana).
if curl -fs http://localhost:8000/openapi.json >/dev/null 2>&1; then
  echo "• API ya estaba arriba → http://localhost:8000"
else
  nohup uv run norm api > "$LOGS/api.log" 2>&1 &
  echo "• API → http://localhost:8000   (log: $LOGS/api.log)"
fi

# 5) Front.
cd "$ROOT/normalizacion-front"
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
