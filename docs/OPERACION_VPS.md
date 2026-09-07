# Operar el VPS (`vps-01`) — chuleta

Todo desde `/srv/azazel/normalizacion-backend`. Los tres perfiles de compose:
`datos` (Postgres, OpenSearch, MinIO) · `app` (API, front, exportador, Caddy) ·
`obs` (Prometheus, Grafana).

**Atajo:** define esto una vez por sesión y el resto del documento lo usa.

```bash
ssh azazel
cd /srv/azazel/normalizacion-backend
C="docker compose -f deploy/docker-compose.prod.yml --env-file .env.prod --profile datos --profile app --profile obs"
```

> ⚠️ **Siempre los tres perfiles juntos.** Con `--profile app` a secas, las
> dependencias del perfil `datos` no existen y el compose falla con
> *"depends on undefined service postgres"* (o `opensearch`, según a cuál llegue
> antes). Medido el 2026-09-05 intentando construir solo `api`.

> ⚠️ El host es **`azazel` (162.35.188.181)**. `mawitherock` (163.172.149.0) era el
> VPS anterior y está **eliminado**; si algún documento o script sigue nombrándolo,
> está desactualizado.

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

El VPS tiene el repo clonado y sigue una rama. Se actualiza **por git**, no
empujando un tar:

```bash
# En el VPS
cd /srv/azazel/normalizacion-backend
git fetch origin <rama> && git merge --ff-only FETCH_HEAD
$C build api                     # reconstruye SIN tocar lo que está corriendo
$C up -d --wait api front exportador
$C exec -T api norm doctor
```

> ⚠️ **Un `git pull` en el host NO cambia lo que ejecuta el contenedor.** La imagen
> lleva el código copiado dentro; hasta que no se reconstruye, `docker compose run`
> y `exec` siguen usando el árbol viejo. Esto ya costó una prueba de migración que
> «pasó» sin ejecutar la migración: `alembic upgrade head` se quedó en la revisión
> anterior porque el fichero nuevo no estaba en la imagen.

> Si tocaste el **Dockerfile de OpenSearch** o la config de Postgres:
> `$C up -d --build --force-recreate --wait opensearch postgres`

### Migraciones de base de datos

Se prueban en una base desechable antes de producción — subir, bajar, volver a
subir— y solo después se aplican:

```bash
$C run --rm --no-deps api alembic upgrade head
```

Las que usan `CREATE INDEX CONCURRENTLY` van dentro de `autocommit_block()` (no
pueden correr en una transacción) y **crean el índice nuevo antes de borrar el
viejo**, para no dejar ninguna ventana sin índice sobre 28,8 M de filas.

---

## Cuando algo va mal

**El front da 502 en `/api/*`** — ya no debería pasar (nginx re-resuelve por
petición), pero si ocurre: `$C restart front`.

**Réplica atrasada** — en este VPS está atrasada *siempre*, porque no hay nada que
la dispare (ver «Lo que corre solo»). Se lanza a mano con
`$C exec -T api norm replicar` y se lee el motivo si falla. Detalle en
`deploy/RUNBOOKS.md#replicaatrasada`.

**Certificado caducado** — Caddy renueva solo. Necesita el **puerto 80 abierto**
para el desafío ACME; si tocaste `ufw`, compruébalo: `ufw status`.

**Se acabó el disco** — los respaldos tienen retención de 14 días, pero el almacén
y el índice crecen sin tope. `df -h /` y `docker system df`.

---

## Lo que corre solo (y lo que no)

```bash
crontab -l                      # el respaldo vive aquí
systemctl list-timers 'azazel-*'
```

| Tarea | Cuándo | Cómo | Estado |
|---|---|---|---|
| Respaldo de Postgres | Diario 09:30 UTC (03:30 en México) | `crontab` → `deploy/respaldo-cron.sh` | activo |
| Réplica del índice | — | — | **NO configurada** |

> ⚠️ **Este documento describía dos timers de systemd que en este servidor no
> existen.** Comprobado el 2026-09-05: `systemctl list-timers 'azazel-*'` devuelve
> *0 timers listed* y `list-unit-files` *0 unit files*. Es decir: **`norm replicar`
> no se ha ejecutado nunca aquí de forma automática**. Si la réplica importa para
> este nodo, hay que instalarla; mientras tanto, se lanza a mano.

El respaldo **sí** corre solo, pero se montó el 2026-09-05 y antes tampoco existía:
el único respaldo que había era el que alguien recordaba lanzar. `respaldo-cron.sh`
añade lo que un script necesita para correr desatendido —PATH explícito, cerrojo
`flock`, log con fecha— y deja el resultado en dos sitios:

```bash
cat /var/lib/azazel/respaldo-estado    # ok|FALLO + sello de tiempo: ¿estamos respaldados?
tail -20 /var/log/azazel-respaldo.log  # qué pasó en las últimas corridas
```

> **No alerta.** Un fallo queda escrito con fecha, pero nadie recibe aviso: hay que
> ir a mirar. Llevarlo a Grafana exige exponer ese marcador como métrica, y no está
> hecho.

---

## Datos que hay que tener a mano

| | |
|---|---|
| Host | `ssh azazel` → 162.35.188.181 |
| URL | https://162-35-188-181.sslip.io |
| Perfil | `hibrido-servicio` · nodo `vps-01` |
| Secretos | `/srv/azazel/normalizacion-backend/.env.prod` (600) |
| Índice de escritura | `archivos-vps-01-000001`; el alias `archivos` apunta **también** a `archivos-mac-01-000001` (índices disjuntos por nodo) |
| Base de pruebas | **no existe ninguna en este servidor** — ver la sección de tests |

> El campo `NORM_API_KEYS` del `.env.prod` no es «la API key» a secas: quien la
> presenta entra como **`admin`**, no como consumidor. Ver la tabla de credenciales.

**Rotar secretos:** `deploy/RUNBOOKS.md#rotar-secretos`. No todos cuestan igual — el
de Postgres y el admin de OpenSearch no se rotan cambiando la variable.

---

## Cuando llegue el dominio propio

```bash
# 1) Registro A del dominio → 162.35.188.181
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

**No lo hagas.** Y hoy no puedes aunque quieras, lo cual es una suerte: hay tres
barreras y las tres se comprobaron el 2026-09-05.

1. **La imagen de producción no trae `pytest`** (se construye con `--no-dev`):
   `ModuleNotFoundError: No module named 'pytest'`.
2. **Los tests no están en la imagen**: `/app/tests` no existe.
3. **La guarda del `conftest` abortaría la sesión.** `tests/integracion/conftest.py`
   cuenta las filas de `archivos` y se detiene si superan 1.000; producción tiene
   **28.829.247**, o sea 28.829 veces el tope.

Lo que la fixture de integración haría si llegara a correr:

```sql
TRUNCATE archivos, discos, control, corridas, config_overrides,
         entidades, mapeos_aprobados, recetas, usuarios, sesiones, extracciones CASCADE
```

Son **once** tablas, no ocho como decía este documento. Y ahora duele más que
antes: `entidades` tiene 89.652 filas de un backfill de 23 minutos, y `usuarios`
contiene la cuenta con la que se entra al panel.

La base `normalizacion_test` que este documento mandaba usar **no existe** en el
servidor (`pg_database` solo tiene `normalizacion`). Los tests se corren en local o
en CI, no aquí. Si algún día hiciera falta, se crea una base desechable, se migra y
se borra al terminar — es lo que se hace para probar migraciones.

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
ssh azazel
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

| Credencial | Rol | Tipo | Para qué |
|---|---|---|---|
| Usuario + contraseña | el suyo | persona | Personas, en el panel |
| Clave CON NOMBRE (pestaña Acceso) | `lector` | `clave-consumidor` | Consumidores externos: **buscar, no descargar** |
| `NORM_API_KEYS` del `.env.prod` | `admin` | clave estática | Emergencia, cuando nadie puede entrar al panel |

> Una clave con nombre **no puede descargar ni explorar el sistema de ficheros**,
> aunque su rol sea `lector`. El rol dice *cuánto* alcanza; el tipo dice *si es una
> persona*. Verificado el 2026-09-05 creando una clave de prueba y borrándola:
>
> | Petición | Código |
> |---|---|
> | `POST /buscar` | 200 |
> | `GET /archivo/{id}/contenido` | **403** |
> | `GET /sistema/carpetas` | **403** |
> | `GET /sistema/destinos-disco` | **403** |
> | `GET /seguridad/claves-busqueda` | **403** |
>
> El guard es `_solo_personas` en `api/main.py`. Este documento afirmaba lo
> contrario («buscar y descargar»), y era falso.

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
