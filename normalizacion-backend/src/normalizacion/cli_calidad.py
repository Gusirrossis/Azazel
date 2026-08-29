"""Subcomandos `norm calidad …` — medir la extracción contra un conjunto dorado.

Flujo completo:

    norm calidad muestrear --salida dorado/     # 1. la herramienta elige y exporta
    # 2. una PERSONA transcribe dorado/verdad/*.txt y *.anclas
    norm calidad evaluar --conjunto dorado/ --guardar linea_base.json
    # 3. se cambia algo del OCR
    norm calidad evaluar --conjunto dorado/ --contra linea_base.json

El paso 2 no se puede automatizar: si la verdad la produjera el propio OCR, se estaría
midiendo contra sí mismo y todo saldría perfecto.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from normalizacion.core.config import cargar_config

app = typer.Typer(name="calidad", help="Medir la calidad de extracción (conjunto dorado).")

#: Carpeta por defecto del conjunto.
_DORADO = "dorado"


@app.command("muestrear")
def muestrear(
    salida: str = typer.Option(_DORADO, help="Carpeta donde dejar el conjunto"),
    total: int = typer.Option(150, help="Documentos a muestrear en total"),
    semilla: int = typer.Option(20260828, help="Semilla: misma semilla = misma muestra"),
) -> None:
    """Elige documentos estratificados y los exporta desde el almacén para anotar."""
    from normalizacion.calidad import conjunto

    config = cargar_config()
    carpeta = Path(salida)
    typer.echo("Muestreando por estratos…")
    muestras = conjunto.muestrear(config, total=total, semilla=semilla)
    if not muestras:
        typer.secho(
            "No se encontró nada que muestrear. ¿Hay archivos con hash_contenido en la cola?",
            fg="red",
        )
        raise typer.Exit(code=1)

    por_estrato: dict[str, int] = {}
    for m in muestras:
        por_estrato[m.estrato] = por_estrato.get(m.estrato, 0) + 1
    for estrato, cuantos in sorted(por_estrato.items()):
        typer.echo(f"  {estrato:<22} {cuantos:>4}")

    typer.echo(f"\nCopiando {len(muestras)} documentos del almacén a {salida}/ …")
    manifiesto = conjunto.exportar(config, muestras, carpeta)
    typer.secho(f"\nListo: {manifiesto}", fg="green")
    typer.echo(
        f"\nAhora hay que transcribir a mano, para cada documento de {salida}/docs/:\n"
        f"  · {salida}/verdad/<hash>.txt     el texto tal como se lee\n"
        f"  · {salida}/verdad/<hash>.anclas  una CURP o RFC por línea\n\n"
        "Si un documento no tiene texto que leer (una foto, una página en blanco),\n"
        "escribe `(sin texto)` en su archivo .anclas: eso lo marca como anotado y\n"
        "sirve para comprobar que el clasificador NO le gasta OCR.\n\n"
        "Se puede anotar por partes: `evaluar` mide con lo que haya."
    )


@app.command("evaluar")
def evaluar(
    conjunto_dir: str = typer.Option(_DORADO, "--conjunto", help="Carpeta del conjunto"),
    guardar: str | None = typer.Option(None, help="Guarda el informe JSON aquí"),
    contra: str | None = typer.Option(None, help="Compara contra un informe anterior"),
    detalle: bool = typer.Option(False, help="Lista el resultado documento a documento"),
) -> None:
    """Mide la configuración ACTUAL contra la verdad anotada."""
    from normalizacion.calidad import evaluador, metricas

    config = cargar_config()
    carpeta = Path(conjunto_dir)
    if not (carpeta / "manifiesto.json").exists():
        typer.secho(
            f"No hay conjunto en {carpeta}/. Créalo con `norm calidad muestrear`.", fg="red"
        )
        raise typer.Exit(code=2)

    informe = evaluador.evaluar_conjunto(config, carpeta)
    if "error" in informe:
        typer.secho(f"{informe['error']} ({informe['sin_anotar']} sin anotar).", fg="yellow")
        raise typer.Exit(code=1)

    total = informe["total"]
    typer.echo("")
    typer.secho("  TOTAL", bold=True)
    _fila(total)
    if informe["sin_anotar"]:
        typer.secho(f"  ({informe['sin_anotar']} documentos aún sin anotar)", fg="yellow")

    typer.echo("\n  POR ESTRATO")
    for estrato, datos in informe["por_estrato"].items():
        typer.echo(f"  {estrato}")
        _fila(datos)

    if total.get("peores"):
        typer.echo("\n  Peor recall de anclas:")
        for h in total["peores"]:
            typer.echo(f"    {h}")

    if detalle:
        typer.echo("\n  DETALLE")
        for d in informe["detalle"]:
            a = d["anclas"]
            typer.echo(
                f"    {d['documento'][:12]}  cer={d['cer']:<7} recall={a['recall']:<7}"
                f" conf={d['confianza']} {' '.join(d['flags'][:3])}"
            )

    if contra:
        anterior = json.loads(Path(contra).read_text(encoding="utf-8"))
        typer.echo(f"\n  CONTRA {contra}")
        for clave, v in metricas.comparar(anterior.get("total", {}), total).items():
            signo = "+" if v["delta"] > 0 else ""
            # El CER y los ms MEJORAN al bajar; el recall y la confianza, al subir.
            mejor_bajando = clave.startswith(("cer", "wer", "ms"))
            va_bien = (v["delta"] < 0) if mejor_bajando else (v["delta"] > 0)
            color = "green" if va_bien else ("red" if v["delta"] else None)
            typer.secho(
                f"    {clave:<18} {v['antes']} → {v['despues']}  ({signo}{v['delta']})",
                fg=color,
            )

    if guardar:
        Path(guardar).write_text(
            json.dumps(informe, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        typer.secho(f"\n  Informe guardado en {guardar}", fg="green")


def _fila(datos: dict) -> None:
    typer.echo(
        f"    documentos={datos.get('documentos')}  "
        f"recall_anclas={datos.get('recall_anclas')}  "
        f"precision={datos.get('precision_anclas')}  "
        f"cer={datos.get('cer_medio')}  "
        f"conf={datos.get('confianza_media')}  "
        f"ms/doc={datos.get('ms_medio')}"
    )
