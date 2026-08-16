#!/usr/bin/env bash
# Siembra las credenciales de S3/MinIO en el keystore antes de arrancar OpenSearch.
#
# `repository-s3` NO lee las credenciales de opensearch.yml ni del entorno: sólo del
# keystore. Sin esto, el repositorio de snapshots se registra pero cada snapshot
# falla con un 403 de MinIO.
set -euo pipefail

BIN=/usr/share/opensearch/bin
KS=/usr/share/opensearch/config/opensearch.keystore

[ -f "$KS" ] || "$BIN/opensearch-keystore" create

if [ -n "${NORM_S3_ACCESS_KEY:-}" ]; then
  printf '%s' "$NORM_S3_ACCESS_KEY" | "$BIN/opensearch-keystore" add --stdin --force s3.client.default.access_key
  printf '%s' "$NORM_S3_SECRET_KEY" | "$BIN/opensearch-keystore" add --stdin --force s3.client.default.secret_key
  echo "[azazel] credenciales S3 sembradas en el keystore"
else
  echo "[azazel] sin NORM_S3_ACCESS_KEY: los snapshots a MinIO no funcionarán"
fi

exec /usr/share/opensearch/opensearch-docker-entrypoint.sh "$@"
