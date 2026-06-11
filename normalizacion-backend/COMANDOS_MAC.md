# Comandos de inicio en la Mac (M4 Max) — 100% nativo, sin Docker

Solo comandos, en orden. Cada uno dice qué hace. Copiar y pegar en la Terminal.

Todo corre nativo en macOS: Postgres y OpenSearch con Homebrew, la app con uv.
Sin Docker no hay VM de por medio: los 64 GB quedan completos para el trabajo.

---

## 0 · Llevar el código a la Mac

El código vive SOLO en la máquina Windows (no hay GitHub), dentro de la carpeta
padre Azazel. Copia la carpeta Azazel COMPLETA a la Mac (AirDrop, USB o red):

```
~/Azazel/normalizacion-backend
~/Azazel/normalizacion-front
```

---

## 1 · Instalar herramientas (una sola vez)

```bash
# Homebrew (si la Mac no lo tiene)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Python (uv), Node, git, detectores nativos (libmagic = tipos, unar = RAR)
brew install uv node git libmagic unar

# Las dos bases del sistema
brew install postgresql@16 opensearch
```

---

## 2 · Preparar las bases (una sola vez)

```bash
# Arranca Postgres y déjalo como servicio (revive solo tras reiniciar)
brew services start postgresql@16

# Crea el usuario y la base que el sistema espera (los del .env por defecto)
psql -d postgres -c "CREATE ROLE norm LOGIN PASSWORD 'norm' CREATEDB;"
createdb -O norm normalizacion

# Arranca OpenSearch como servicio (queda en http://localhost:9200)
brew services start opensearch
```

Opcional pero recomendado en el M4 (más memoria para el índice): editar
`$(brew --prefix)/etc/opensearch/jvm.options` y dejar `-Xms8g` y `-Xmx8g`,
luego `brew services restart opensearch`.

---

## 3 · Preparar la aplicación (una sola vez)

```bash
cd ~/Azazel/normalizacion-backend

# Instala Python 3.12 + todas las dependencias del proyecto
uv sync --extra workers --extra api

# Crea tu configuración a partir del ejemplo
cp .env.ejemplo .env
```

Editar `.env` (`nano .env`) y dejar el almacén en el disco externo de DESTINO
(distinto al de origen):

```ini
NORM_ALMACEN_BACKEND=local
NORM_ALMACEN_LOCAL_RAIZ=/Volumes/DESTINO/almacen
NORM_ALMACEN_FRIO_LOCAL_RAIZ=/Volumes/DESTINO/frio
```

Después:

```bash
# Crea/actualiza las tablas en Postgres (idempotente: siempre es seguro)
uv run alembic upgrade head

# Crea/actualiza el índice de búsqueda en OpenSearch (idempotente)
uv run norm aplicar-indice
```

---

## 4 · Arrancar (cada sesión de trabajo)

```bash
# Terminal 1 — la API (lee los discos nativo, a velocidad completa)
cd ~/Azazel/normalizacion-backend
uv run norm api

# Terminal 2 — la interfaz web
cd ~/Azazel/normalizacion-front
npm install        # solo la primera vez
npm run dev        # abre http://localhost:5173
```

(Postgres y OpenSearch ya están vivos como servicios de brew — no se tocan.)

---

## 5 · Indexar

**Desde el navegador** (lo normal): `http://localhost:5173` → **📂 Indexar carpeta…**
→ eliges origen, destino y workers (Automático = 14 en el M4 Max) → Indexar.
Progreso por fase en vivo; la búsqueda se actualiza sola mientras corre.

**Por terminal** (equivalente):

```bash
# Todo el ciclo sobre una carpeta, con 12 workers en paralelo
uv run norm pipeline /Volumes/ORIGEN/muestra-piloto --workers 12
```

---

## 6 · Comandos útiles del día a día

```bash
uv run norm estado-disco <disco_id>   # ¿cómo va un disco? (conteos por estado)
uv run norm puerta <disco_id>         # ¿ya es SEGURO desechar el origen?
uv run norm pausar                    # frena el sistema (drena el lote y para)
uv run norm reanudar                  # lo vuelve a echar a andar
uv run norm reprocesar-errores        # reintenta lo que quedó en ERROR
uv run norm rescore-frio              # re-evalúa el frío con reglas nuevas
uv run norm exportador                # métricas Prometheus en :9108 (opcional)
```

---

## 7 · Apagar

```bash
# Ctrl+C en las terminales de la API y el front. Las bases pueden quedarse
# corriendo (no consumen casi nada en reposo). Para apagarlas también:
brew services stop opensearch
brew services stop postgresql@16
```

---

## Si algo no arranca

```bash
brew services list                       # ¿postgres y opensearch dicen "started"?
curl http://localhost:9200               # ¿OpenSearch responde?
psql -U norm -d normalizacion -c "\dt"   # ¿Postgres responde y tiene tablas?
uv run pytest -m "not integracion" -q    # la suite rápida del proyecto
```

---

*Monitoreo con gráficas (Grafana/Prometheus) y MinIO quedan fuera del modo
nativo a propósito: para el piloto no hacen falta (el front ya muestra fases,
duraciones y archivos/s por corrida). Regresan con Docker en producción.*
