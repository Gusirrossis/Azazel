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

## Acceso al panel: usuarios y sesiones

El panel se entra con **usuario y contraseña**. La sesión viaja en una cookie
`HttpOnly` + `Secure` + `SameSite=Strict`, y vive como fila en la tabla `sesiones`:
por eso se puede revocar de verdad y al instante, cosa que un JWT no permite sin
montar una lista negra.

Las `NORM_API_KEYS` del `.env.prod` **no desaparecen**: son para consumidores
máquina (reddoor, el AEB) y como acceso de emergencia. Ver la tabla de más abajo.

### Alta del primer administrador

El panel exige una cuenta para entrar, así que la primera se crea desde la
terminal del servidor. Mientras no exista ningún usuario **ni** ninguna llave, la
API acepta cualquier petición: es el hueco justo para este arranque, y se cierra
solo en cuanto existe la primera cuenta.

```bash
ssh mawitherock
cd /srv/azazel/normalizacion-backend
docker compose -f deploy/docker-compose.prod.yml exec api \
  norm usuarios crear tu-usuario --rol admin
```

Pide la contraseña por teclado, sin eco: no pasarla como argumento es deliberado,
porque un argumento queda en el historial del shell y en la lista de procesos.

Mínimo **12 caracteres**. La política prefiere longitud a composición: no exige
símbolos porque eso empuja a `Password1!`, que es lo primero que prueba cualquier
diccionario.

### Los tres roles

| Rol | Puede |
|---|---|
| `lector` | Buscar, ver tableros y entidades, descargar originales |
| `operador` | Lo anterior + lanzar corridas, reprocesar, mover frío, editar el filtro |
| `admin` | Todo + usuarios, claves de API, recetas y recursos |

Son acumulativos. El backend los impone endpoint por endpoint; el front además
esconde lo que tu rol no alcanza, para no ofrecer botones que solo darían 403.

### Cómo entra cada tipo de credencial

| Credencial | Rol | Para qué |
|---|---|---|
| Usuario + contraseña | el suyo | Personas, en el panel |
| Clave CON NOMBRE (pestaña Acceso, `bus_…`) | `lector` | Consumidores externos: buscar y descargar |
| `NORM_API_KEYS` del `.env.prod` | `admin` | Emergencia, cuando nadie puede entrar al panel |

### Operaciones habituales

```bash
# Dentro del contenedor api (mismo prefijo docker compose … exec api que arriba)
norm usuarios listar
norm usuarios crear ana --rol operador
norm usuarios rol ana admin
norm usuarios contrasena ana        # reseteo: cierra todas sus sesiones
norm usuarios desactivar ana        # no borra: conserva la traza de lo que hizo
norm usuarios activar ana
```

No se puede degradar ni desactivar al **último admin activo**: el CLI y la API lo
rechazan. Salir de esa situación obligaría a entrar a Postgres a mano.

### Si te quedas fuera

1. **Olvidaste la contraseña** → `norm usuarios contrasena <usuario>` por SSH.
2. **No queda ningún admin** → crea otro: `norm usuarios crear rescate --rol admin`.
3. **La API no responde y hace falta consultar ya** → usa la llave de
   `NORM_API_KEYS` con la cabecera `X-API-Key`; entra como `admin`.

Un reseteo de contraseña cierra todas las sesiones de esa cuenta. Es deliberado:
si se cambia porque se sospecha que alguien entró, dejar viva su sesión no arregla
nada.

### Cosas que rompen el login (y no lo parecen)

- **`NORM_SESION_COOKIE_SECURE=true` sin HTTPS.** El navegador descarta la cookie
  sin avisar: el login responde 200 y aun así "no pasa nada". En producción va
  siempre en `true` (Caddy pone el TLS); solo en dev nativo sobre `http://localhost`
  hay que ponerlo en `false`.
- **Quitar `X-Forwarded-For` del nginx del front.** La API vería a todo el mundo
  con la misma IP, y el freno del login por IP bloquearía a todos los usuarios a la
  vez en cuanto alguien fallara cinco veces.
- **Un `NORM_API_CORS_ORIGENES` que no incluya el origen real del front.** Con
  `allow_credentials` el navegador exige orígenes explícitos; con `*` no manda la
  cookie.
