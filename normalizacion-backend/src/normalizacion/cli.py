"""CLI de control: `norm …` (cada máquina arranca solo su comando vía systemd)."""

from __future__ import annotations

from typing import Any

import typer

from normalizacion import __version__
from normalizacion.core.config import cargar_config
from normalizacion.core.observabilidad import configurar_logging

app = typer.Typer(
    name="norm",
    help="Sistema de normalización masiva de datos.",
    no_args_is_help=True,
)


def _exigir(config: Any, capaz: bool, que: str) -> None:
    """⚙K16: corta con un motivo claro si este nodo no tiene la capacidad.

    Salir con código 2 (no 1) distingue "este nodo no hace eso" de "falló": un
    systemd que arranque el comando equivocado en el nodo equivocado se ve en el log
    como configuración, no como avería."""
    if not capaz:
        typer.secho(
            f"Este nodo tiene perfil '{config.despliegue.perfil}' y no {que}.", fg="red"
        )
        raise typer.Exit(code=2)


@app.command()
def version() -> None:
    """Versión del paquete y del filtro de precalificación."""
    config = cargar_config()
    typer.echo(f"normalizacion {__version__} · filtro {config.filtro.version_filtro}")


@app.command()
def estado() -> None:
    """Estado de la cola: conteo de filas por estado y por disco."""
    import psycopg

    config = cargar_config()
    try:
        with psycopg.connect(config.postgres_dsn, connect_timeout=5) as conn:
            filas = conn.execute(
                "SELECT disco_id, estado, COUNT(*) FROM archivos"
                " GROUP BY disco_id, estado ORDER BY disco_id, estado"
            ).fetchall()
    except psycopg.OperationalError as exc:
        typer.secho(f"No hay conexión a Postgres ({config.postgres_dsn}): {exc}", fg="red")
        typer.echo(
            "¿Está arriba el perfil 'cola'?  "
            "docker compose -f deploy/docker-compose.dev.yml --profile cola up -d"
        )
        raise typer.Exit(code=1) from exc

    if not filas:
        typer.echo("Cola vacía: aún no se ha catalogado ningún disco.")
        return
    for disco_id, est, cuenta in filas:
        typer.echo(f"{disco_id:<24} {est:<14} {cuenta}")


@app.command()
def proyectar(
    csv: str = typer.Argument(help="CSV con filas de personas a proyectar a entidades"),
    tipo: str = typer.Option("persona", help="Tipo de entidad (receta)"),
    umbral: float = typer.Option(0.6, help="Confianza mínima para auto-mapear una columna"),
) -> None:
    """E2+E3: lee un CSV, PROPONE el mapeo columna→campo, lo aplica y proyecta a
    entidades canónicas resueltas por ancla (CURP/RFC/email). Idempotente."""
    import polars as pl

    from normalizacion.core import despliegue
    from normalizacion.entidades import mapeo as M
    from normalizacion.entidades.pipeline import proyectar as _proyectar
    from normalizacion.entidades.receta import obtener_receta

    config = cargar_config()
    _exigir(config, despliegue.de_config(config).corre_entidades, "resuelve entidades")
    receta = obtener_receta(tipo)
    df = pl.read_csv(csv, infer_schema_length=0)  # todo como texto
    columnas = df.columns
    muestras = {c: df[c].head(20).to_list() for c in columnas}
    propuestas = M.proponer_mapeo(receta, columnas, muestras)
    asignacion = M.asignacion_desde_propuesta(propuestas, umbral)

    typer.echo("Mapeo propuesto (columna → campo):")
    for col, p in propuestas.items():
        marca = "✓" if asignacion.get(col) else "·"
        typer.echo(f"  {marca} {col:<24} → {p['campo'] or '—':<16} ({p['confianza']:.0%})")

    filas = df.to_dicts()
    r = _proyectar(config, receta, asignacion, filas, procedencia={"ruta": csv})
    typer.secho(f"\nProyección: {r.como_dict()}", fg="green")


@app.command()
def catalogo(
    ruta: str = typer.Argument(help="Raíz del disco montado (solo lectura)"),
    disco_id: str | None = typer.Option(
        None, help="Identificador del disco (default: nombre de la carpeta)"
    ),
) -> None:
    """Fase 1: inventaría un disco completo hacia la cola (rápido, sin abrir archivos)."""
    from pathlib import Path

    from normalizacion.ingesta.catalogo.walker import catalogar_disco

    config = cargar_config()
    resumen = catalogar_disco(config, Path(ruta), disco_id)
    typer.echo(f"Disco '{resumen.disco_id}' catalogado:")
    typer.echo(f"  archivos vistos:  {resumen.archivos_vistos}")
    typer.echo(f"  nuevos en cola:   {resumen.archivos_nuevos}")
    typer.echo(f"  directorios:      {resumen.directorios}")
    typer.echo(f"  errores de I/O:   {resumen.errores}")
    if resumen.archivos_nuevos == 0 and resumen.archivos_vistos > 0:
        typer.echo("  (re-scan: todo ya estaba en la cola — idempotencia)")


@app.command()
def precalifica() -> None:
    """Fase 1.5: doble filtro T0-T2 — puntúa los PENDIENTE y los enruta a HOT/COLD."""
    from normalizacion.ingesta.precalificacion.precalificador import precalificar_pendientes

    config = cargar_config()
    resumen = precalificar_pendientes(config)
    typer.echo(f"Precalificación (filtro {config.filtro.version_filtro}):")
    typer.echo(f"  procesados:   {resumen.procesados}")
    typer.echo(f"  -> HOT:       {resumen.hot}")
    typer.echo(f"  -> COLD:      {resumen.cold}  (frio reversible - nada se borra)")
    typer.echo(f"  re-encolados: {resumen.re_encolados}  (entradas internas de contenedores)")
    typer.echo(f"  errores:      {resumen.errores}")


@app.command()
def perfil(disco_id: str = typer.Argument(help="Identificador del disco")) -> None:
    """Perfil del disco: qué trae, ANTES de gastar el pipeline (estilo brunnhilde)."""
    from normalizacion.ingesta.precalificacion.precalificador import perfil_disco

    p = perfil_disco(cargar_config(), disco_id)
    typer.echo(f"Perfil del disco '{p.disco_id}' — {p.total} archivos")
    typer.echo("\nPor ruta/estado:")
    for ruta, archivos, tamano in p.por_ruta:
        typer.echo(f"  {ruta:<14} {archivos:>8} archivos  {tamano / 1_048_576:>10.1f} MiB")
    typer.echo("\nTop tipos reales:")
    for tipo, cuenta in p.top_tipos:
        typer.echo(f"  {tipo:<70} {cuenta:>6}")
    typer.echo("\nPor motivo de decisión:")
    for motivo, cuenta in p.por_motivo:
        typer.echo(f"  {motivo:<40} {cuenta:>6}")


@app.command()
def worker(
    sin_indice: bool = typer.Option(
        False, "--sin-indice", help="No indexar a OpenSearch (solo persistir blobs)"
    ),
) -> None:
    """Fase 2: worker HOT — lectura única: hash + copia al almacén (dedup) + doc + índice."""
    from normalizacion.core.indexador import Sink, SinkNulo
    from normalizacion.ingesta.workers.orquestador import procesar_hot

    config = cargar_config()
    sink: Sink
    if sin_indice:
        sink = SinkNulo()
    else:
        from normalizacion.core.indexador.opensearch import SinkOpenSearch

        sink = SinkOpenSearch(config)
    resumen = procesar_hot(config, sink=sink)
    typer.echo(f"Worker HOT (almacén: {config.almacen_backend}):")
    typer.echo(f"  procesados:      {resumen.procesados}")
    typer.echo(f"  blobs nuevos:    {resumen.blobs_nuevos}")
    typer.echo(f"  deduplicados:    {resumen.deduplicados}  (mismo contenido: no se re-copia)")
    typer.echo(f"  bytes copiados:  {resumen.bytes_copiados}")
    typer.echo(f"  huerfanos:       {resumen.huerfanos_rescatados}")
    typer.echo(f"  transitorios:    {resumen.transitorios}  (volveran solos con backoff)")
    typer.echo(f"  errores:         {resumen.errores}")


@app.command("mover-frio")
def mover_frio_cmd() -> None:
    """Fase 2: mueve los COLD al almacén frío (también el frío es dato que sobrevive)."""
    from normalizacion.ingesta.workers.verificador import mover_frio

    resumen = mover_frio(cargar_config())
    typer.echo(
        f"Mover a frio: {resumen.movidos} movidos, "
        f"{resumen.deduplicados} deduplicados, {resumen.errores} errores"
    )


@app.command()
def verificar() -> None:
    """Fase 2: re-lee cada blob y lo compara con su hash (VERIFICADO -> HECHO)."""
    from normalizacion.ingesta.workers.verificador import verificar_indexados

    resumen = verificar_indexados(cargar_config())
    typer.echo(f"Verificacion: {resumen.verificados} OK, {resumen.fallidos} fallidos")
    if resumen.fallidos:
        typer.secho("  HAY BLOBS QUE NO VERIFICAN: la puerta quedara BLOQUEADA", fg="red")


@app.command()
def puerta(disco_id: str = typer.Argument(help="Identificador del disco")) -> None:
    """La puerta SAGRADA: ¿es seguro desechar este disco? (100% a salvo o NO)."""
    from normalizacion.ingesta.workers.verificador import DiscoDesconocido, evaluar_puerta

    try:
        e = evaluar_puerta(cargar_config(), disco_id)
    except DiscoDesconocido as exc:
        # NO se responde "no seguro": este nodo no vio el disco, así que cualquier
        # veredicto sería inventado (⚙K16).
        typer.secho(f"Sin veredicto: {exc}", fg="yellow", bold=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"Puerta de integridad — disco '{e.disco_id}'")
    typer.echo(f"  total:        {e.total}")
    typer.echo(f"  HECHO:        {e.hechos}")
    typer.echo(f"  COLD movidos: {e.cold_movidos}")
    typer.echo(f"  pendientes:   {e.pendientes}  (incluye {e.errores} en ERROR)")
    if e.seguro_para_desechar:
        typer.secho("  >>> SEGURO PARA DESECHAR <<<", fg="green", bold=True)
    elif e.motivo_bloqueo == "pendiente_replica_al_maestro":
        typer.secho(
            "  >>> NO desechar: a salvo aquí, pero sus blobs aún no llegaron"
            " al archivo maestro <<<",
            fg="red",
            bold=True,
        )
    else:
        typer.secho("  >>> NO desechar: hay datos sin poner a salvo <<<", fg="red", bold=True)


@app.command("aplicar-indice")
def aplicar_indice_cmd(
    deploy: str = typer.Option("deploy", help="Carpeta con mappings/ e ism/"),
) -> None:
    """Aplica el index template + politica ISM y crea el indice con su alias."""
    from pathlib import Path

    from normalizacion.core.indexador.opensearch import aplicar_indice

    aplicar_indice(cargar_config(), Path(deploy))
    typer.echo("Template + ISM + indice aplicados.")


@app.command("backfill-entidades")
def backfill_entidades_cmd(
    lote: int = typer.Option(500, help="Documentos por lote (1-2000)"),
    max_docs: int | None = typer.Option(None, help="Tope de docs esta corrida (sin tope = todo)"),
    reiniciar: bool = typer.Option(False, "--reiniciar", help="Ignora el cursor y empieza de cero"),
) -> None:
    """Resuelve entidades de los registros YA INDEXADOS (los que traen CURP/RFC).

    Recorre el indice de OpenSearch e idempotentemente crea/fusiona personas por su
    ancla. Reanudable: guarda el avance en `control` y re-ejecutar no duplica. Pensado
    para correr en la Mac sobre el lago ya indexado."""
    from normalizacion.core import despliegue
    from normalizacion.entidades.backfill import backfill_desde_indice

    config = cargar_config()
    _exigir(config, despliegue.de_config(config).corre_entidades, "resuelve entidades")

    def _tic(r: Any) -> None:
        typer.echo(f"  …{r.docs} docs · {r.con_persona} con persona · "
                   f"{r.entidades_nuevas} nuevas · {r.entidades_fusionadas} fusionadas")

    r = backfill_desde_indice(
        config, lote=lote, max_docs=max_docs, reiniciar=reiniciar, on_progress=_tic,
    )
    typer.secho(f"\nBackfill: {r.como_dict()}", fg="green")


@app.command()
def buscar(texto: str = typer.Argument(help="Texto a buscar en el nombre")) -> None:
    """Demo de búsqueda por nombre (wildcard) contra el alias de lectura."""
    from normalizacion.core.indexador.opensearch import buscar_por_nombre

    resultados = buscar_por_nombre(cargar_config(), texto)
    if not resultados:
        typer.echo("Sin resultados.")
        return
    for doc in resultados:
        typer.echo(
            f"  {doc['nombre']:<28} {doc.get('tipo_real') or '?':<60} puntaje={doc.get('puntaje')}"
        )


@app.command()
def api(
    host: str = typer.Option("127.0.0.1", help="Interfaz de escucha"),
    puerto: int = typer.Option(8000, help="Puerto"),
) -> None:
    """Fase 5: levanta la API de búsqueda (FastAPI + uvicorn)."""
    import uvicorn

    uvicorn.run("normalizacion.api.main:app", host=host, port=puerto)


@app.command()
def openapi() -> None:
    """Imprime el contrato OpenAPI (JSON) — para el equipo del front."""
    import json

    from normalizacion.api.main import crear_app

    typer.echo(json.dumps(crear_app(cargar_config()).openapi(), ensure_ascii=False, indent=2))


@app.command()
def pausar() -> None:
    """Pausa GLOBAL: todos los procesos drenan su lote actual y se detienen."""
    import psycopg

    from normalizacion.core import cola

    with psycopg.connect(cargar_config().postgres_dsn) as conn:
        cola.fijar_pausa(conn, True)
        conn.commit()
    typer.secho("Sistema PAUSADO (los lotes en curso terminan y se detienen).", fg="yellow")


@app.command()
def reanudar() -> None:
    """Quita la pausa global."""
    import psycopg

    from normalizacion.core import cola

    with psycopg.connect(cargar_config().postgres_dsn) as conn:
        cola.fijar_pausa(conn, False)
        conn.commit()
    typer.secho("Sistema REANUDADO.", fg="green")


@app.command("reprocesar-errores")
def reprocesar_errores_cmd(
    motivo: str | None = typer.Option(
        None, help="Solo errores cuyo motivo empiece así (ej. 'io_frio%')"
    ),
) -> None:
    """Dead-letter de vuelta a su etapa (PENDIENTE/PRECALIFICADO/COLD según el caso)."""
    import psycopg

    from normalizacion.core import cola

    with psycopg.connect(cargar_config().postgres_dsn) as conn:
        destinos = cola.reprocesar_errores(conn, motivo)
        conn.commit()
    if not destinos:
        typer.echo("No había errores que reprocesar.")
        return
    for estado, cuenta in sorted(destinos.items()):
        typer.echo(f"  -> {estado}: {cuenta}")


@app.command("rescore-frio")
def rescore_frio_cmd(
    disco_id: str | None = typer.Option(None, help="Limitar a un disco"),
) -> None:
    """Re-puntúa el frío con el filtro vigente (COLD -> PENDIENTE; reversibilidad)."""
    import psycopg

    from normalizacion.core import cola

    config = cargar_config()
    with psycopg.connect(config.postgres_dsn) as conn:
        cuantos = cola.rescore_frio(conn, disco_id)
        conn.commit()
    typer.echo(
        f"{cuantos} archivos COLD vuelven a PENDIENTE para re-puntuarse"
        f" (filtro {config.filtro.version_filtro}). Corre `norm precalifica`."
    )


@app.command("estado-disco")
def estado_disco_cmd(disco_id: str = typer.Argument(help="Identificador del disco")) -> None:
    """Vista del operador: puerta + desglose del disco en un solo comando."""
    from normalizacion.ingesta.precalificacion.precalificador import perfil_disco
    from normalizacion.ingesta.workers.verificador import DiscoDesconocido, evaluar_puerta

    config = cargar_config()
    try:
        e = evaluar_puerta(config, disco_id)
    except DiscoDesconocido as exc:
        typer.secho(f"Sin veredicto: {exc}", fg="yellow", bold=True)
        raise typer.Exit(code=2) from exc
    typer.echo(
        f"Disco '{disco_id}': total={e.total} HECHO={e.hechos}"
        f" COLD_movidos={e.cold_movidos} pendientes={e.pendientes} ERROR={e.errores}"
    )
    color = "green" if e.seguro_para_desechar else "red"
    veredicto = "SEGURO PARA DESECHAR" if e.seguro_para_desechar else "NO desechar"
    typer.secho(f"  Puerta: {veredicto}", fg=color, bold=True)
    p = perfil_disco(config, disco_id)
    for motivo, cuenta in p.por_motivo[:8]:
        typer.echo(f"  {motivo:<44} {cuenta:>6}")


@app.command()
def exportador(
    puerto: int = typer.Option(9108, help="Puerto del endpoint /metrics"),
    intervalo: float = typer.Option(15.0, help="Segundos entre refrescos desde Postgres"),
) -> None:
    """Fase 6: exporta métricas Prometheus (backlog, rutas, errores, discos, pausa)."""
    from normalizacion.core.observabilidad.metricas import correr_exportador

    correr_exportador(cargar_config(), puerto, intervalo)


@app.command()
def pipeline(
    ruta: str = typer.Argument(help="Carpeta a procesar de punta a punta"),
    disco_id: str | None = typer.Option(None, help="Identificador (default: nombre carpeta)"),
    sin_indice: bool = typer.Option(False, "--sin-indice", help="Sin OpenSearch (pruebas)"),
    workers: int | None = typer.Option(
        None, "--workers", min=1, max=64, help="Procesos worker en paralelo (default: auto)"
    ),
) -> None:
    """TODO el ciclo: catálogo → filtro → blobs+índice → frío → verificar → puerta."""
    from pathlib import Path

    from normalizacion.ingesta.pipeline import destinos, ejecutar_corrida, iniciar_corrida

    config = cargar_config()
    typer.echo("Destinos configurados (cambia con variables NORM_*):")
    for clave, valor in destinos(config).items():
        typer.echo(f"  {clave:<18} {valor}")
    corrida_id, id_disco = iniciar_corrida(config, Path(ruta), disco_id)
    typer.echo(f"\nCorrida {corrida_id} sobre '{id_disco}':")
    fases = ejecutar_corrida(
        config,
        corrida_id,
        Path(ruta).expanduser().resolve(),
        id_disco,
        usar_indice=not sin_indice,
        workers=workers,
    )
    for f in fases:
        tasa = f.get("archivos_por_segundo")
        extra = f"  ({tasa}/s)" if tasa else ""
        m = f["metricas"]
        resumen = ", ".join(f"{k}={v}" for k, v in m.items() if isinstance(v, int | bool) and v)
        typer.echo(f"  {f['fase']:<16} {f['duracion_s']:>7.2f}s{extra}  {resumen}")
    typer.secho("Corrida COMPLETADA.", fg="green")


@app.command()
def doctor() -> None:
    """¿Está bien configurado ESTE nodo? Perfil, stores, seguridad y réplica.

    Los fallos de topología son silenciosos: un nodo con el perfil equivocado
    arranca perfectamente y hace lo que no le toca. Esto los saca a la luz."""
    from normalizacion.core.doctor import cabecera, diagnosticar

    config = cargar_config()
    cab = cabecera(config)
    activas = [k for k, v in cab["capacidades"].items() if v]
    inactivas = [k for k, v in cab["capacidades"].items() if not v]
    typer.secho(
        f"Perfil: {cab['perfil']}  ·  nodo_id: {cab['nodo_id']}", bold=True
    )
    typer.echo(f"  capacidades: {', '.join(activas) or '—'}")
    if inactivas:
        typer.echo(f"  sin capacidad: {', '.join(inactivas)}")
    typer.echo("")

    # Marcas ASCII a propósito: la consola de Windows (cp1252) revienta con ✓/⚠/✗,
    # y un diagnóstico que se cae al imprimirse no diagnostica nada.
    marcas = {"ok": ("OK", "green"), "aviso": ("!!", "yellow"), "error": ("XX", "red")}
    peor = "ok"
    for c in diagnosticar(config):
        simbolo, color = marcas[c.nivel]
        typer.secho(f" [{simbolo}] {c.titulo}", fg=color, nl=not c.detalle)
        if c.detalle:
            typer.echo(f" - {c.detalle}")
        if c.nivel == "error" or (c.nivel == "aviso" and peor == "ok"):
            peor = c.nivel
    if peor == "error":
        raise typer.Exit(code=1)


@app.command()
def replicar() -> None:
    """⚙K16 — replica lo que le toca a este nodo (snapshot o restore del índice).

    El archivo maestro TOMA snapshot de sus índices; el nodo de servicio RESTAURA
    los ajenos y los engancha al alias como lectura. Pensado para un timer de
    systemd/cron: es idempotente y no interfiere con la ingesta."""
    from normalizacion.core import replicacion

    config = cargar_config()
    r = replicacion.replicar(config)
    typer.echo(f"Nodo '{config.despliegue.nodo_id}' — acción: {r.accion}")
    if r.snapshot:
        typer.echo(f"  snapshot: {r.snapshot}")
    if r.indices:
        typer.echo(f"  índices restaurados: {', '.join(r.indices)}")
    if r.ok:
        typer.secho("  replicación OK", fg="green")
    else:
        typer.secho(f"  FALLÓ: {r.motivo}", fg="red")
        raise typer.Exit(code=1)


@app.command("generar-disco")
def generar_disco_cmd(
    destino: str = typer.Argument(help="Carpeta donde crear el disco sintético"),
    semilla: int = typer.Option(42, help="Misma semilla → mismo árbol byte a byte"),
) -> None:
    """Genera un disco sintético determinista para demos y pruebas."""
    from pathlib import Path

    from normalizacion.herramientas.generador_disco import generar_disco

    manifiesto = generar_disco(Path(destino), semilla=semilla)
    typer.echo(f"Disco sintético en {destino} (semilla {semilla}):")
    for clave, valor in manifiesto.items():
        typer.echo(f"  {clave}: {valor}")


def main() -> None:
    """Entrypoint del script `norm`."""
    configurar_logging()
    app()
