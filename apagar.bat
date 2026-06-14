@echo off
REM ===========================================================================
REM Azazel - APAGAR los contenedores de Docker (Windows). Doble clic.
REM Los datos (Postgres, OpenSearch, MinIO) viven en volumenes y NO se pierden.
REM ===========================================================================
setlocal
cd /d "%~dp0normalizacion-backend"

echo Deteniendo los contenedores de Azazel...
docker compose -f deploy/docker-compose.dev.yml --profile full --profile app down

echo.
echo Listo. Los datos se conservan en los volumenes; vuelve a arrancar.bat cuando quieras.
pause
