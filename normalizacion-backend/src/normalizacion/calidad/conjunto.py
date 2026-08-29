"""El conjunto dorado: muestreo estratificado, formato de anotación y evaluación.

**Muestreo estratificado, no aleatorio.** Una muestra al azar de este corpus daría
casi puro PDF de texto nativo, que es lo que ya funciona. Lo que hay que medir es
donde duele: escaneos, fotos de documentos, fotocopias. El muestreo reparte el cupo
por estratos y dentro de cada uno sí toma al azar, con semilla fija para que la
muestra sea reproducible.

**Los documentos salen del ALMACÉN, no del disco original.** Se copian por
`hash_contenido`, así que el conjunto sigue siendo válido aunque el disco de origen
ya no exista — que es todo el punto del almacén direccionado por contenido.

Formato del conjunto (una carpeta):

    dorado/
      manifiesto.json          ← qué documento es cada cual y de qué estrato
      docs/<hash>.<ext>        ← el archivo, copiado del almacén
      verdad/<hash>.txt        ← el texto transcrito A MANO  (lo pone una persona)
      verdad/<hash>.anclas     ← una CURP o RFC por línea    (lo pone una persona)

Los dos archivos de `verdad/` son el trabajo humano; todo lo demás lo genera la
herramienta. Un documento sin `verdad/` se salta con un aviso, así que se puede
anotar de a poco y medir con lo que haya.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg

from normalizacion.core.config import Config

MANIFIESTO = "manifiesto.json"

#: Estratos y su cupo relativo. El reparto refleja DÓNDE está el riesgo, no cómo se
#: distribuye el corpus: el PDF nativo es la mayoría de los archivos y casi nada del
#: problema, así que se lleva el cupo mínimo que permite detectar una regresión.
ESTRATOS: dict[str, dict[str, Any]] = {
    "pdf_nativo": {
        "cupo": 0.15,
        "descripcion": "PDF con texto propio — el control: aquí no debe empeorar nada",
        "sql": "a.tipo_real = 'application/pdf'",
    },
    "pdf_escaneado": {
        "cupo": 0.40,
        "descripcion": "PDF sin texto nativo: el grueso del corpus y del riesgo",
        "sql": "a.tipo_real = 'application/pdf'",
    },
    "imagen_documento": {
        "cupo": 0.30,
        "descripcion": "Credenciales, actas y oficios fotografiados o escaneados",
        "sql": "a.tipo_real LIKE 'image/%'",
    },
    "imagen_no_documento": {
        "cupo": 0.15,
        "descripcion": "Fotos y capturas: mide que el clasificador NO las mande a OCR",
        "sql": "a.tipo_real LIKE 'image/%'",
    },
}


@dataclass(frozen=True, slots=True)
class Muestra:
    hash_contenido: str
    archivo_id: str
    nombre: str
    tipo_real: str | None
    tamano: int
    estrato: str


def muestrear(
    config: Config, *, total: int = 150, semilla: int = 20260828
) -> list[Muestra]:
    """Elige los documentos del conjunto, repartidos por estrato.

    La semilla se fija a propósito: dos personas que corran esto obtienen la MISMA
    muestra, y ampliar el conjunto más adelante no invalida lo ya anotado.
    """
    azar = random.Random(semilla)
    elegidas: list[Muestra] = []
    vistos: set[str] = set()

    with psycopg.connect(config.postgres_dsn, connect_timeout=10) as conn:
        for nombre_estrato, definicion in ESTRATOS.items():
            cupo = max(1, round(total * definicion["cupo"]))
            # Se pide de más y se filtra en Python: el criterio que distingue
            # "escaneado" de "nativo" no vive en la cola (depende del texto extraído),
            # así que se cruza con `extracciones` cuando existe.
            filas = conn.execute(
                "SELECT a.hash_contenido, a.archivo_id, a.nombre, a.tipo_real, a.tamano,"
                "       e.motor, e.chars"
                " FROM archivos a"
                " LEFT JOIN extracciones e ON e.hash_contenido = a.hash_contenido"
                f" WHERE a.hash_contenido IS NOT NULL AND {definicion['sql']}"
                " LIMIT 4000",
            ).fetchall()

            candidatas = [
                f for f in filas
                if f[0] not in vistos and _encaja(nombre_estrato, motor=f[5], chars=f[6])
            ]
            azar.shuffle(candidatas)
            for f in candidatas[:cupo]:
                vistos.add(f[0])
                elegidas.append(
                    Muestra(
                        hash_contenido=f[0], archivo_id=f[1], nombre=f[2],
                        tipo_real=f[3], tamano=f[4], estrato=nombre_estrato,
                    )
                )
    return elegidas


def _encaja(estrato: str, *, motor: str | None, chars: int | None) -> bool:
    """¿Esta fila pertenece al estrato? Se decide con lo que ya se sabe de su extracción.

    Sin fila en `extracciones` (nunca procesado) solo se puede decidir por tipo, así
    que se acepta: es preferible una muestra con algún documento mal clasificado que
    una muestra que excluye justo lo que aún no se ha procesado.
    """
    if motor is None:
        return True
    if estrato == "pdf_nativo":
        return motor == "nativo" and (chars or 0) > 200
    if estrato == "pdf_escaneado":
        return motor in ("ocr", "mixto") or (chars or 0) <= 200
    return True  # los estratos de imagen no se pueden separar sin mirar píxeles


def exportar(config: Config, muestras: list[Muestra], destino: Path) -> Path:
    """Copia los documentos del ALMACÉN y deja el manifiesto y los huecos de anotación."""
    from normalizacion.core.almacen import crear_almacen

    destino.mkdir(parents=True, exist_ok=True)
    (destino / "docs").mkdir(exist_ok=True)
    (destino / "verdad").mkdir(exist_ok=True)

    almacen = crear_almacen(config)
    manifiesto: list[dict[str, Any]] = []
    for m in muestras:
        extension = Path(m.nombre).suffix or _extension_por_tipo(m.tipo_real)
        ruta = destino / "docs" / f"{m.hash_contenido}{extension}"
        if not ruta.exists():
            try:
                with almacen.leer(m.hash_contenido) as fuente, ruta.open("wb") as salida:
                    while bloque := fuente.read(1024 * 1024):
                        salida.write(bloque)
            except Exception as exc:
                manifiesto.append({**_como_dict(m), "error": str(exc)[:150]})
                continue
        # Se crean los huecos de anotación vacíos: así se ve de un vistazo qué falta
        # por transcribir, y `evaluar` sabe distinguir "sin anotar" de "sin texto".
        for sufijo in (".txt", ".anclas"):
            hueco = destino / "verdad" / f"{m.hash_contenido}{sufijo}"
            if not hueco.exists():
                hueco.write_text("", encoding="utf-8")
        manifiesto.append({**_como_dict(m), "archivo": f"docs/{ruta.name}"})

    ruta_manifiesto = destino / MANIFIESTO
    ruta_manifiesto.write_text(
        json.dumps(
            {"estratos": {k: v["descripcion"] for k, v in ESTRATOS.items()},
             "documentos": manifiesto},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    return ruta_manifiesto


def _como_dict(m: Muestra) -> dict[str, Any]:
    return {
        "hash_contenido": m.hash_contenido, "archivo_id": m.archivo_id,
        "nombre": m.nombre, "tipo_real": m.tipo_real,
        "tamano": m.tamano, "estrato": m.estrato,
    }


def _extension_por_tipo(tipo: str | None) -> str:
    return {
        "application/pdf": ".pdf", "image/jpeg": ".jpg", "image/png": ".png",
        "image/tiff": ".tif", "image/gif": ".gif",
    }.get(tipo or "", ".bin")


@dataclass(frozen=True, slots=True)
class Anotado:
    """Un documento del conjunto con su verdad ya transcrita."""

    hash_contenido: str
    ruta: Path
    estrato: str
    tipo_real: str | None
    texto: str
    anclas: list[str]


def cargar(carpeta: Path) -> tuple[list[Anotado], list[str]]:
    """Lee el conjunto. Devuelve `(anotados, sin_anotar)`.

    Los que no tienen verdad NO son un error: el conjunto se anota poco a poco y hay
    que poder medir con lo que ya está listo. Se devuelven aparte para que el informe
    diga sobre cuántos documentos se está midiendo de verdad.
    """
    manifiesto = json.loads((carpeta / MANIFIESTO).read_text(encoding="utf-8"))
    anotados: list[Anotado] = []
    sin_anotar: list[str] = []

    for entrada in manifiesto.get("documentos", []):
        h = entrada.get("hash_contenido")
        archivo = entrada.get("archivo")
        if not h or not archivo:
            continue
        ruta_texto = carpeta / "verdad" / f"{h}.txt"
        ruta_anclas = carpeta / "verdad" / f"{h}.anclas"
        texto = ruta_texto.read_text(encoding="utf-8").strip() if ruta_texto.exists() else ""
        crudo = ruta_anclas.read_text(encoding="utf-8") if ruta_anclas.exists() else ""
        lista = [ln.strip() for ln in crudo.splitlines() if ln.strip()]

        # Un documento del estrato "no documento" se anota con texto VACÍO a propósito:
        # su verdad es "aquí no hay nada que leer". Se distingue de "sin anotar" porque
        # su archivo de anclas trae la marca explícita.
        vacio_intencional = "(sin texto)" in crudo
        if not texto and not lista and not vacio_intencional:
            sin_anotar.append(h)
            continue
        anotados.append(
            Anotado(
                hash_contenido=h,
                ruta=carpeta / archivo,
                estrato=entrada.get("estrato", "?"),
                tipo_real=entrada.get("tipo_real"),
                texto=texto,
                anclas=[a for a in lista if not a.startswith("(")],
            )
        )
    return anotados, sin_anotar
