@echo off
REM ===========================================================================
REM Azazel - ARRANQUE RAPIDO en Docker (Windows). Doble clic.
REM
REM Levanta todos los contenedores (Postgres, OpenSearch, MinIO, API, front) y
REM abre el navegador. Tras un corte o una actualizacion: ejecuta esto y luego
REM pulsa "Re-indexar" en el front para reanudar.
REM
REM Carpetas de tu contenido: define NORM_CARPETA_DATOS y NORM_CARPETA_DESTINO
REM abajo (o como variables del sistema). Si las dejas vacias, usa carpetas de
REM prueba dentro del repo.
REM ===========================================================================
setlocal
cd /d "%~dp0normalizacion-backend"

REM --- EDITA AQUI tus carpetas (origen y destino del indexado) ---------------
if not defined NORM_CARPETA_DATOS    set "NORM_CARPETA_DATOS=C:\Users\Public\normalizacion-datos"
if not defined NORM_CARPETA_DESTINO  set "NORM_CARPETA_DESTINO=C:\Users\Public\normalizacion-destino"
REM ---------------------------------------------------------------------------

echo ============================================================
echo  Arrancando Azazel en Docker...
echo  Datos:   %NORM_CARPETA_DATOS%
echo  Destino: %NORM_CARPETA_DESTINO%
echo ============================================================

docker compose -f deploy/docker-compose.dev.yml --profile full --profile app up -d --build --wait
if errorlevel 1 (
  echo.
  echo  [ERROR] No se pudo levantar. Esta Docker Desktop abierto?
  pause
  exit /b 1
)

echo.
echo  Abriendo el front...
start "" http://localhost:8080

echo.
echo ============================================================
echo  Listo. En el navegador pulsa "Re-indexar" para reanudar.
echo  (Para apagar: doble clic en apagar.bat)
echo ============================================================
pause
