# Runbooks de operación (Fase 6)

Cada alerta de `prometheus-alertas.yml` tiene aquí su respuesta. Regla general:
**nada de este sistema borra datos** — toda intervención es reversible.

## ErroresEnDeadLetter

Hay filas en `ERROR`. **1)** Ver el desglose: `norm estado` y los motivos en Grafana
(panel "Errores por motivo") o `SELECT error_motivo, COUNT(*) FROM archivos WHERE
estado='ERROR' GROUP BY 1`. **2)** Si el motivo es `agotado:almacen`/`agotado:indice`
→ la dependencia estuvo caída: verifica MinIO/OpenSearch y corre
`norm reprocesar-errores`. **3)** Si es `verificacion_fallida` → blob corrupto:
NO reprocesar a ciegas; revisar el blob y el disco origen (que NO debe desecharse —
la puerta ya lo está reteniendo). **4)** `extraccion_*` no llega aquí (son flags,
no errores) — si lo ves, es un bug.

## BacklogEstancado

Hay trabajo en cola pero nada avanza. **1)** ¿Está pausado? `norm estado` /
panel "Pausado" → `norm reanudar`. **2)** ¿Hay workers corriendo? Revisa systemd/
procesos. **3)** ¿Dependencia caída? Los logs de los workers dirán
`fallo_transitorio` con el tipo (almacen/indice/io_fuente) — restaura el servicio;
las filas vuelven solas (backoff). **4)** ¿Leases atorados? Los huérfanos se
rescatan al arrancar cualquier worker.

## SistemaPausadoOlvidado

La pausa lleva 30+ minutos. Si el mantenimiento terminó: `norm reanudar`.
Si es intencional (p. ej. ventana larga), silencia la alerta en Alertmanager.

## DiscosSinPuertaVerde

Un disco lleva horas sin "seguro para desechar". `norm estado-disco <id>` muestra
qué lo retiene: pendientes en flujo (espera/da más workers), COLD sin mover
(`norm mover-frio`), INDEXADO sin verificar (`norm verificar`) o ERROR (ver el
runbook de dead-letter). **JAMÁS desechar el disco físico con la puerta roja.**

## ExportadorCaido

Sin métricas no hay visibilidad (operación a ciegas). **1)** ¿Proceso vivo?
`norm exportador` (systemd `norm-exportador` en prod). **2)** ¿Postgres accesible
desde esa máquina? El exportador loguea `exportador_sin_postgres`. **3)** Mientras
tanto, `norm estado` da el backlog directo de la base.
