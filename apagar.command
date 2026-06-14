#!/bin/bash
# ============================================================================
# Azazel — APAGAR la API y el front en la Mac (las bases siguen vivas).
# Doble clic en el Finder. Las bases (Postgres/OpenSearch) consumen casi nada
# en reposo y conviene dejarlas; así el arranque siguiente es más rápido.
# ============================================================================
echo "▶ Deteniendo Azazel…"

if pkill -f "norm api" 2>/dev/null; then echo "• API detenida"; else echo "• API no estaba corriendo"; fi
if pkill -f "vite"     2>/dev/null; then echo "• Front detenido"; else echo "• Front no estaba corriendo"; fi

echo
echo "Las bases (Postgres/OpenSearch) siguen como servicios. Para pararlas también:"
echo "  brew services stop opensearch && brew services stop postgresql@16"
