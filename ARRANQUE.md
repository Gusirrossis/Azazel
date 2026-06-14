# Arranque rápido (corte de luz / actualización)

Scripts de un clic para volver a levantar todo el sistema y reanudar la
indexación. **Nada se pierde**: la cola es durable (Postgres) y el catálogo es
incremental — al reanudar no se reprocesa lo ya hecho.

## En la Mac (producción, nativo)

1. Doble clic en **`arrancar.command`**.
   - Levanta Postgres y OpenSearch (servicios de Homebrew).
   - Pone el backend al día (dependencias + esquema + índice — cubre el caso de
     haber actualizado la versión, todo idempotente).
   - Arranca la **API** (`:8000`) y el **front** (`:5173`) en segundo plano.
   - Abre el navegador.
2. En el front, pulsa **«Re-indexar»** → la indexación retoma donde quedó.
3. Para apagar la API y el front: doble clic en **`apagar.command`** (las bases
   quedan vivas; consumen casi nada y aceleran el siguiente arranque).

> La primera vez, si macOS bloquea el `.command`, autorízalo en
> *Ajustes → Privacidad y seguridad → Abrir igualmente*, o en Terminal:
> `chmod +x arrancar.command apagar.command`.
>
> Logs: `~/azazel-logs/api.log` y `~/azazel-logs/front.log`.

## En Windows (Docker, pruebas)

1. (Una vez) abre `arrancar.bat` y ajusta `NORM_CARPETA_DATOS` y
   `NORM_CARPETA_DESTINO` a tus carpetas de origen y destino.
2. Doble clic en **`arrancar.bat`** → levanta los contenedores y abre el front
   en `:8080`.
3. Pulsa **«Re-indexar»** en el front.
4. Para apagar: doble clic en **`apagar.bat`** (los datos se conservan en los
   volúmenes).

## Por qué basta con «Re-indexar»

Al arrancar, la API marca como *fallida* la corrida que el corte dejó colgada
"en curso" (si la hubo) y libera el lock. El avance real vive en la cola, así
que re-ejecutar la misma carpeta —el botón **Re-indexar** usa la última corrida—
retoma de forma incremental: solo lo nuevo o lo que quedó pendiente genera
trabajo.
