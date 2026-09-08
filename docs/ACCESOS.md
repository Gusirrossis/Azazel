# ACCESOS — las URLs canónicas de cada nodo

**Estas URLs no cambian.** Si algo deja de funcionar, se arregla el nodo para que la URL
siga sirviendo; no se inventa una URL nueva.

Última verificación end-to-end (login + sesión + CORS): **2026-09-08**.

---

## Los tres nodos

| Nodo | Qué es | URL |
|---|---|---|
| **Matriz** | El planeta. Resuelve entidades y sirve a Lilith | https://162-35-188-181.sslip.io |
| **Luna storage** | Normaliza `/home` del VPS de almacenamiento | **https://205.209.102.101:8443** |
| **Luna Lilith** | Normaliza los datos de Lilith sin moverlos | http://162.35.188.166:3000 |

**Credenciales del panel — las mismas en los tres** (verificado con login + panel en
cada uno; la anterior da 401 en los tres):

```
usuario:     maui
contraseña:  Maui-S4xqrMk7lk8cws2q
```

> Si se rota la contraseña, **se rota en los TRES nodos en la misma pasada**. La primera
> vez se cambió solo en dos y la matriz se quedó con la vieja durante un día, con este
> documento afirmando que eran iguales. Cada usuario vive en la base de SU nodo: no hay
> nada que las sincronice sola.

> El certificado de la luna storage es propio (no de una CA): el navegador avisa la
> primera vez y hay que aceptar la excepción. Es esperado.

---

## Claves de API (header `x-api-key`)

**Una por nodo, y NO son intercambiables.** Comprobado nodo por nodo: la clave de uno
da 401 en los otros dos.

| Nodo | Clave |
|---|---|
| Matriz | `gt3xV7QhEBpcMmc1dhCTXaKUkmT5` |
| Luna storage | `azazel_4745fb35499479e6749feac2e5925edf77c65eccd1460f4a` |
| Luna Lilith | `azazel_e78d06bf847ba1460c634c0d834b1745f3782eb8eab26015` |

> ⚠️ **La de la matriz no se rota a la ligera: es la que usa Lilith para federar.**
> Cambiarla deja a Lilith sin poder consultar a Azazel. Si hay que rotarla, se cambia
> a la vez en el `.env` de Lilith.

Que sean distintas es deliberado: permite revocar la de un consumidor sin dejar sin
servicio a los demás (es la Fase 1 de `PLAN-FEDERACION-LILITH`).

```bash
curl -k -H "x-api-key: <clave del nodo>" https://205.209.102.101:8443/api/panel
```

---

## Entrar por SSH

```bash
# Matriz
ssh -i <llave azazel> root@162.35.188.181

# Luna storage — a través del bastión `azazel`
ssh azazel
sshpass -e ssh secureuser@205.209.102.101        # SSHPASS en el entorno

# Luna Lilith
ssh -i "C:\Users\Asus Xeon\.ssh\azazel_vps2" -o IdentitiesOnly=yes root@162.35.188.166
```

| Nodo | Código | Compose |
|---|---|---|
| Matriz | `/srv/azazel` | `docker compose` (v2 plugin) |
| Luna storage | `/srv/azazel` | `docker-compose` (v2 binario) |
| Luna Lilith | `/opt/azazel-luna` | `docker compose` (v2 plugin) |

> Ojo: en la luna storage es `docker-compose` **con guion**; en los otros dos, sin él.
> Usar el que no toca da un `Usage: docker [OPTIONS]` que despista.

---

## Por qué cada URL es la que es

**Luna storage — el `:8443` y no el `:3000`.** nginx termina TLS en 8443 con un
certificado propio y hace de proxy al front. El 80/443 de esa máquina los sirven
corintio, tecno y michoacán, y Caddy peleaba con ellos por el puerto en un bucle de
reinicios. El `:3000` también responde, pero **la URL buena es el 8443**: va cifrada.

**Luna Lilith — el `:3000` en HTTP.** El 80/443 son de Lilith (su Caddy) y no se tocan.

**Si el panel deja de dejar entrar, mira el CORS antes que nada.** Un origen que falta
en `NORM_API_CORS_ORIGENES` da un panel que carga y no loguea, y **no se ve con `curl`**:
CORS lo aplica el navegador, no el servidor. Para reproducirlo hay que mandar la cabecera:

```bash
curl -k -i -X POST https://205.209.102.101:8443/api/auth/login \
  -H "Origin: https://205.209.102.101:8443" \
  -H "Content-Type: application/json" \
  -d '{"usuario":"maui","contrasena":"..."}' | grep -i access-control
```

---

## Operación diaria

```bash
# ¿Qué tal va todo?               (el centinela corre solo cada 2 min)
tail /srv/azazel/centinela-alertas.log        # storage y matriz
tail /opt/azazel-luna/centinela-alertas.log   # Lilith

# Parar / reanudar la ingesta
docker exec normalizacion-api-1 norm pausar
docker exec normalizacion-api-1 norm reanudar

# Replicar a la matriz a mano (va solo por cron: storage 15 min, Lilith 20 min)
/srv/azazel/replicar_a_matriz.sh          # storage
/opt/azazel-luna/replicar_a_matriz.sh     # Lilith
tail -20 .../replica.log
```

**El centinela frena solo.** Si el disco pasa del 93 % ejecuta `norm pausar`: detiene la
ingesta sin matarla. Nace de un incidente real —el disco llegó al 99 % con la corrida
corriendo y la alerta se escribió tres veces sin que nada la parara—. Cuando haya sitio,
`norm reanudar`.

---

## Indexar

Desde el panel: eliges carpeta y listo. **No hay que rellenar nada más** — ni `disco_id`
(se deriva de la ruta) ni carpeta de destino (el selector está oculto a propósito: estos
nodos no guardan copia, así que no habría dónde ponerla).

Solo una corrida a la vez por nodo. Re-lanzar sobre la misma carpeta es **incremental y
seguro**: lo ya hecho no se repite.
