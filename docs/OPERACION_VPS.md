# Operar el VPS (`vps-01`) — chuleta

Todo desde `/srv/azazel/normalizacion-backend`. Los tres perfiles de compose:
`datos` (Postgres, OpenSearch, MinIO) · `app` (API, front, exportador, Caddy) ·
`obs` (Prometheus, Grafana).

**Atajo:** define esto una vez por sesión y el resto del documento lo usa.

```bash
ssh mawitherock
cd /srv/azazel/normalizacion-backend
C="docker compose -f deploy/docker-compose.prod.yml --env-file .env.prod --profile datos --profile app --profile obs"
```

> ⚠️ **Siempre los tres perfiles juntos.** Con `--profile app` a secas, las
> dependencias del perfil `datos` no existen y el compose falla con
> *"depends on undefined service opensearch"*.

---

## Lo que se hace el 95 % de las veces

| Qué | Comando |
|---|---|
| Ver estado | `$C ps` |
| Diagnóstico del nodo | `$C exec -T api norm doctor` |
| Logs de un servicio | `$C logs -f api` |
| Reiniciar todo | `$C up -d --wait` |
| Replicar ahora | `$C exec -T api norm replicar` |
| Respaldar ahora | `deploy/respaldo.sh` |

---

## Arrancar desde cero (tras un reinicio del servidor)

Docker arranca solo (`restart: unless-stopped`), así que normalmente **no hay que
hacer nada**. Si hiciera falta:

```bash
$C up -d --wait
$C exec -T api norm doctor
```

---

## Actualizar el código

```bash
# 1) Desde tu equipo, empuja los cambios al VPS (sin pasar por GitHub)
cd ~/Documents/GitHub/Azazel
tar czf - --exclude='.git' --exclude='node_modules' --exclude='.venv' \
    --exclude='__pycache__' --exclude='.mypy_cache' --exclude='.pytest_cache' \
    --exclude='.ruff_cache' . | ssh mawitherock 'tar xzf - -C /srv/azazel'

# 2) En el VPS
$C up -d --build --wait api front exportador
$C exec -T api norm doctor
```

> Si tocaste el **Dockerfile de OpenSearch** o la config de Postgres:
> `$C up -d --build --force-recreate --wait opensearch postgres`

---

## Cuando algo va mal

**El front da 502 en `/api/*`** — ya no debería pasar (nginx re-resuelve por
petición), pero si ocurre: `$C restart front`.

**Réplica atrasada o nunca ejecutada** — `systemctl status azazel-replicar.timer`,
luego `$C exec -T api norm replicar` a mano y lee el motivo. Detalle en
`deploy/RUNBOOKS.md#replicaatrasada`.

**Certificado caducado** — Caddy renueva solo. Necesita el **puerto 80 abierto**
para el desafío ACME; si tocaste `ufw`, compruébalo: `ufw status`.

**Se acabó el disco** — los respaldos tienen retención de 14 días, pero el almacén
y el índice crecen sin tope. `df -h /` y `docker system df`.

---

## Timers activos

```bash
systemctl list-timers 'azazel-*'
```

| Timer | Cada | Qué hace |
|---|---|---|
| `azazel-replicar` | 30 min | Snapshot o restore del índice, según el papel del nodo |
| `azazel-respaldo` | Diario 03:30 | Vuelca Postgres a `minio://respaldos/` (retiene 14 días) |

---

## Datos que hay que tener a mano

| | |
|---|---|
| URL | https://163-172-149-0.sslip.io |
| Perfil | `hibrido-servicio` · nodo `vps-01` |
| Secretos | `/srv/azazel/normalizacion-backend/.env.prod` (600) |
| API key | dentro de ese archivo, en `NORM_API_KEYS` |
| Índice de escritura | `archivos-vps-01-000001` (alias `archivos`) |
| Base de pruebas | `normalizacion_test` — **nunca** correr tests contra `normalizacion`: la fixture hace `TRUNCATE` de 8 tablas |

**Rotar secretos:** `deploy/RUNBOOKS.md#rotar-secretos`. No todos cuestan igual — el
de Postgres y el admin de OpenSearch no se rotan cambiando la variable.

---

## Cuando llegue el dominio propio

```bash
# 1) Registro A del dominio → 163.172.149.0
# 2) En el VPS:
sed -i 's|^NORM_DOMINIO=.*|NORM_DOMINIO=tu-dominio.com|' .env.prod
sed -i 's|^NORM_API_CORS_ORIGENES=.*|NORM_API_CORS_ORIGENES=["https://tu-dominio.com"]|' .env.prod
$C up -d --force-recreate caddy api
```

Caddy pide el certificado solo. **El Caddyfile no se toca**: ya está en su forma
final. Hoy usa `sslip.io` (DNS comodín público) porque Let's Encrypt no emite para
IPs — es de terceros, y con dominio propio esa dependencia desaparece.

---

## Correr los tests contra este VPS

Nunca contra la base de producción (la fixture la trunca). Contra `normalizacion_test`:

```bash
set -a; . ./.env.prod; set +a
docker run --rm --network normalizacion_interna \
  -v /srv/azazel/normalizacion-backend:/work -w /work \
  -e NORM_POSTGRES_DSN="postgresql://${NORM_PG_USER}:${NORM_PG_PASSWORD}@postgres:5432/normalizacion_test" \
  -e NORM_OPENSEARCH_URL="https://opensearch:9200" \
  -e NORM_OPENSEARCH_USUARIO=admin -e NORM_OPENSEARCH_PASSWORD="${NORM_OS_ADMIN_PASSWORD}" \
  -e UV_CACHE_DIR=/tmp/uvcache \
  normalizacion-api:latest \
  sh -c "uv sync --frozen --extra workers --extra api --group dev >/dev/null 2>&1 && uv run pytest -q"
```

Para la suite unitaria en los **tres perfiles** (lo que hace la CI), repite
cambiando `-e NORM_DESPLIEGUE__PERFIL=local|hibrido-ingesta|hibrido-servicio`.
