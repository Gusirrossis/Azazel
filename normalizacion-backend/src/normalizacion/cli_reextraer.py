"""Subcomando `norm reextraer` — rehacer la extracción de lo que se leyó mal.

Lee los bytes del ALMACÉN por `hash_contenido`, así que funciona sin el disco original.
Reindexa por `archivo_id`, que es idempotente: correrlo dos veces no duplica nada.

Casos típicos:

    norm reextraer --listar                      # ¿cuánto trabajo hay? (en seco)
    norm reextraer --confianza-menor 50          # lo peor leído, primero
    norm reextraer --bandera ocr_pdf_parcial     # los que se quedaron a medias
    norm reextraer --bandera ocr_descartado_confianza
    norm reextraer --version-vieja               # todo lo extraído con una versión previa
"""

from __future__ import annotations

import typer

from normalizacion.core.config import cargar_config

app = typer.Typer(name="reextraer", help="Reproceso dirigido de la extracción.")


@app.callback(invoke_without_command=True)
def reextraer(
    listar: bool = typer.Option(
        False, "--listar", help="Solo dice cuántos candidatos hay, sin tocar nada"
    ),
    confianza_menor: float | None = typer.Option(
        None, "--confianza-menor", help="Solo los que quedaron por debajo de esta confianza"
    ),
    bandera: str | None = typer.Option(
        None, "--bandera", help="Solo los que llevan esta bandera (ocr_pdf_parcial…)"
    ),
    tipo: str | None = typer.Option(
        None, "--tipo", help="Filtra por tipo real; admite comodín (image/*)"
    ),
    motor: str | None = typer.Option(None, "--motor", help="nativo | ocr | mixto"),
    version_vieja: bool = typer.Option(
        False, "--version-vieja", help="Todo lo extraído con una versión anterior del extractor"
    ),
    limite: int = typer.Option(500, help="Tope de contenidos por corrida"),
) -> None:
    """Re-extrae y reindexa lo que cumpla el filtro. Sin filtros no hace nada."""
    from normalizacion import reextraccion
    from normalizacion.core import cache_extraccion

    config = cargar_config()
    hay_filtro = any([confianza_menor is not None, bandera, tipo, motor, version_vieja])
    if not hay_filtro:
        typer.secho(
            "Hace falta al menos un filtro: --confianza-menor, --bandera, --tipo,"
            " --motor o --version-vieja.\n"
            "Reextraer TODO sin querer costaría una pasada de OCR sobre el corpus entero.",
            fg="red",
        )
        raise typer.Exit(code=2)

    resumen = reextraccion.reextraer(
        config,
        confianza_menor_a=confianza_menor,
        motor=motor,
        tipo_real=tipo,
        con_bandera=bandera,
        # Sin `--version-vieja` no se filtra por versión: se quiere reprocesar por
        # calidad, no por antigüedad, y filtrar por ambas cosas a la vez dejaría
        # fuera justo lo que ya está en la versión actual y salió mal igualmente.
        version_distinta_de=cache_extraccion.clave_version(config) if version_vieja else None,
        limite=limite,
        solo_listar=listar,
    )

    if listar:
        typer.echo(f"Candidatos a reextraer: {resumen.candidatos}")
        if resumen.candidatos >= limite:
            typer.secho(f"  (topado en --limite {limite}; puede haber más)", fg="yellow")
        return

    typer.echo("Reproceso de extracción:")
    typer.echo(f"  candidatos:   {resumen.candidatos}")
    typer.echo(f"  reextraídos:  {resumen.reextraidos}")
    typer.echo(f"  mejoraron:    {resumen.mejorados}")
    typer.echo(f"  empeoraron:   {resumen.empeorados}")
    typer.echo(f"  sin cambio:   {resumen.sin_cambio}")
    typer.echo(f"  reindexados:  {resumen.reindexados}")
    if resumen.errores:
        typer.secho(f"  errores:      {resumen.errores}", fg="red")
    if resumen.empeorados > resumen.mejorados:
        typer.secho(
            "\n  Empeoraron más de las que mejoraron: conviene revisar la configuración"
            "\n  contra el conjunto dorado antes de seguir (`norm calidad evaluar`).",
            fg="yellow",
        )


@app.command("estado")
def estado() -> None:
    """Cuánto ahorró la caché de extracción y cómo va la calidad del OCR."""
    from normalizacion.core import cache_extraccion

    datos = cache_extraccion.estadisticas(cargar_config())
    if not datos:
        typer.echo("Sin extracciones registradas todavía.")
        return
    typer.echo("Caché de extracción por contenido:")
    typer.echo(f"  contenidos únicos:  {datos['extracciones']}")
    typer.echo(f"  reusos:             {datos['reusos']}  (extracciones que NO hubo que rehacer)")
    typer.echo(f"  tiempo ahorrado:    {datos['ms_ahorrados'] / 1000:.0f}s")
    typer.echo(f"  con OCR:            {datos['con_ocr']}")
    typer.echo(f"  confianza media:    {datos['confianza_media']}")
    typer.echo(f"  confianza < 60:     {datos['confianza_baja']}  ← candidatos a reextraer")
