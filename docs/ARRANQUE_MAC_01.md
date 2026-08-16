# Arranque de `mac-01` — el nodo de ingesta

**Para qué:** el VPS (`vps-01`) ya está desplegado y sirviendo. Falta el otro extremo:
la Mac, que es la que lee los discos físicos y es el **archivo maestro** (donde vive la
copia permanente de todo).

**Cómo usar este documento:** copia el bloque de §3 y pégaselo a Claude Code en la Mac.
Es una sola instrucción; el resto del documento es para que puedas verificar lo que hizo.

---

## 1. Lo que ya existe (no hay que tocarlo)

| | |
|---|---|
| VPS | `163.172.149.0` · perfil `hibrido-servicio` · nodo `vps-01` |
| Web | https://163-172-149-0.sslip.io (certificado real de Let's Encrypt) |
| Acceso SSH | usuario `root`, llave `mawitherock.pem` |
| Secretos | `/srv/azazel/normalizacion-backend/.env.prod` (permisos 600, **nunca salieron del servidor**) |
| Config WireGuard de la Mac | `/srv/azazel/mac-01-wg0.conf` (ya generada, con su llave privada) |
| Réplica | timer de systemd cada 30 min, ya activo |

---

## 2. Antes de empezar, en la Mac

1. **Docker Desktop** instalado y abierto. En *Settings → Resources*: **16 GB de RAM** y
   **4 CPU** como mínimo.
2. **La llave SSH.** Cópiala desde este Windows (`~/Downloads/mawitherock.pem`) a la Mac,
   por ejemplo con AirDrop o un USB, y déjala en `~/.ssh/mawitherock.pem`.
   En macOS **sí** hace falta `chmod 400` (en Windows mandaban las ACL, en Mac manda el
   modo del archivo).
3. **El repo.** `git pull` y sitúate en la rama `feat/topologia-hibrida`.

> ⚠️ Los discos de origen se montan **solo lectura** y el destino va en **otro disco
> físico distinto**. Si origen y destino comparten bus, las lecturas pelean contra las
> escrituras y el rendimiento se hunde — es el cuello declarado del sistema.

---

## 3. LA INSTRUCCIÓN — pégale esto a Claude Code en la Mac

```
Configura esta Mac como el nodo `mac-01` de Azazel (perfil hibrido-ingesta,
archivo maestro), siguiendo docs/PLAN_TOPOLOGIA.md y docs/ARRANQUE_MAC_01.md.

Contexto: el VPS 163.172.149.0 ya está desplegado como `vps-01` (perfil
hibrido-servicio). Tengo la llave SSH en ~/.ssh/mawitherock.pem. Falta este
extremo.

Haz esto, verificando cada paso antes de seguir al siguiente:

1. Deja el acceso SSH al VPS con `IdentitiesOnly yes` y alias `mawitherock`
   (sin esto ssh ofrece todas mis llaves, el servidor corta a los 5 intentos
   y fail2ban banea la IP). Comprueba que entras.

2. Monta WireGuard: baja /srv/azazel/mac-01-wg0.conf del VPS por scp, instala
   wireguard-tools con brew, levanta el túnel y comprueba que haces ping a
   10.77.0.1. Sin túnel no hay replicación.

3. Prepara el backend: `uv sync --extra workers --extra api`, y crea un .env
   con perfil hibrido-ingesta y nodo_id mac-01. Las bases (Postgres,
   OpenSearch, MinIO) van en Docker; la API y los workers van NATIVOS, porque
   los mounts de Docker son el cuello al leer los discos externos.

4. Levanta las bases, corre `alembic upgrade head` y `norm aplicar-indice`.
   Verifica que el índice de escritura se llama `archivos-mac-01-000001`.

5. Configura la replicación de MinIO: el bucket `snapshots` de esta Mac debe
   replicarse al MinIO del VPS a través del túnel (10.77.0.1). Las
   credenciales del VPS están en su .env.prod: sácalas por SSH, no las
   escribas en ningún archivo del repo.

6. Programa `norm replicar` cada 30 min con launchd.

7. Corre `norm doctor` y no pares hasta que todo salga en [OK] salvo lo que
   sea legítimamente un aviso. Explícame lo que quede en amarillo.

No indexes ningún disco todavía: primero quiero ver el nodo sano.
```

---

## 4. Qué debería quedar cuando termine

```
$ norm doctor
Perfil: hibrido-ingesta  ·  nodo_id: mac-01
  capacidades: ingesta, archivo_maestro, destino_eligible
  sin capacidad: entidades, publico

 [OK] Postgres - esquema en 0007
 [OK] OpenSearch - alias 'archivos': 1 índice(s), 0 docs
 [OK] MinIO - buckets almacen, frio
 [OK] Réplica - último éxito hace 0.0 h — emisor (toma snapshot)
```

Fíjate en **`sin capacidad: entidades`**. Es correcto y deliberado: las entidades se
resuelven **solo en el VPS**. Si las resolvieran los dos nodos, cada uno lo haría sobre
su trozo del índice y ambos empujarían al AEB con `modo_merge: reemplazar`
(*last-write-wins*), sobrescribiéndose en bucle con versiones parciales de la misma
persona.

---

## 5. Cómo comprobar que los dos nodos están de verdad conectados

Cuando la Mac haya indexado algo, en el VPS:

```bash
ssh mawitherock
cd /srv/azazel/normalizacion-backend
docker compose -f deploy/docker-compose.prod.yml --env-file .env.prod \
  --profile datos --profile app exec -T api norm replicar
```

Debe listar índices `archivos-mac-01-*` restaurados. Y en https://163-172-149-0.sslip.io
las búsquedas ya deberían encontrar archivos que la Mac procesó — **sin que los teras
hayan viajado**: solo se replica el índice (~4 % del origen), no los archivos.

---

## 6. Primer disco (cuando el nodo esté sano)

En modo híbrido el `disco_id` es **obligatorio**: derivarlo del nombre de la carpeta hace
que dos nodos con carpetas homónimas produzcan el mismo identificador y sus `archivo_id`
colisionen.

```bash
uv run norm pipeline /Volumes/MI_DISCO --disco-id RESPALDO-2026-08
```

Queda registrado como `mac-01:RESPALDO-2026-08`. **Ese identificador es permanente:**
re-catalogar el mismo disco con otro id cambiaría todos sus `archivo_id` y lo duplicaría
entero en la cola y en el índice.

---

## 7. Dos cosas que conviene saber

**El dominio.** Hoy se usa `sslip.io`, un DNS comodín público, para poder tener
certificado real sin comprar dominio. Es de terceros: si dejara de resolver, el
certificado no renovaría. Con dominio propio eso desaparece — registro A a la IP,
cambiar `NORM_DOMINIO` en el `.env.prod` del VPS, y `docker compose up -d caddy`.

**La puerta de integridad.** En `mac-01` funciona como siempre. En el VPS, en cambio,
ningún origen puede declararse *seguro para desechar* hasta que sus blobs lleguen al
archivo maestro (esta Mac) — si no, borrarías un dump cuya única copia vive en un VPS de
900 GB.
