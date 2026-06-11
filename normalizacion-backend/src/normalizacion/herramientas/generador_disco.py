"""Generador determinista de un "disco" sintético — la fixture de TODO el pipeline.

Misma semilla → mismo árbol byte a byte (los zips usan fecha fija). Contiene a propósito
los casos que el sistema debe manejar: tabulares útiles, documentos, basura de T0,
multimedia, una extensión mentirosa, duplicados exactos, contenedores anidados
(cajas dentro de cajas) y una zip-bomb de juguete que viola el guard de ratio (⚙K4).

Regla del plan: jamás se prueba primero con datos reales.
"""

from __future__ import annotations

import hashlib
import io
import random
import zipfile
from pathlib import Path

FECHA_ZIP = (2020, 1, 1, 0, 0, 0)  # timestamps fijos → zips deterministas


def _zip_bytes(entradas: dict[str, bytes]) -> bytes:
    """Crea un ZIP determinista en memoria."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for nombre, datos in sorted(entradas.items()):
            info = zipfile.ZipInfo(nombre, date_time=FECHA_ZIP)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, datos)
    return buf.getvalue()


def _csv(rng: random.Random, filas: int) -> bytes:
    """CSV con columnas consistentes (lo que T2 debe reconocer como tabular)."""
    lineas = ["id,fecha,cliente,monto,moneda"]
    for i in range(filas):
        lineas.append(
            f"{i},2023-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d},"
            f"cliente_{rng.randint(1, 500)},{rng.randint(10, 99999) / 100:.2f},MXN"
        )
    return ("\n".join(lineas) + "\n").encode("utf-8")


def _ndjson(rng: random.Random, filas: int) -> bytes:
    lineas = [
        f'{{"evento": "compra", "id": {i}, "monto": {rng.randint(1, 9999)}}}' for i in range(filas)
    ]
    return ("\n".join(lineas) + "\n").encode("utf-8")


def _texto_legible(rng: random.Random, parrafos: int) -> bytes:
    frases = [
        "El sistema no tolera perdida de datos.",
        "Cada archivo se lee una sola vez y se puede reanudar.",
        "La precalificacion enruta, nunca borra.",
        "Los discos de origen son desechables; el almacen es permanente.",
    ]
    cuerpo = "\n\n".join(" ".join(rng.choices(frases, k=8)) for _ in range(parrafos))
    return cuerpo.encode("utf-8")


def generar_disco(destino: Path, semilla: int = 42) -> dict[str, int]:
    """Genera el disco sintético en `destino` y devuelve el manifiesto de conteos."""
    rng = random.Random(semilla)
    archivos: dict[str, bytes] = {}

    # --- útiles: tabulares y estructurados (deben terminar en HOT) ---
    archivos["datos/ventas_2023.csv"] = _csv(rng, 200)
    archivos["datos/clientes.csv"] = _csv(rng, 80)
    archivos["datos/eventos.ndjson"] = _ndjson(rng, 100)
    archivos["datos/config.xml"] = (
        b'<?xml version="1.0" encoding="UTF-8"?>\n<config><entorno>dev</entorno></config>\n'
    )
    archivos["datos/notas.txt"] = _texto_legible(rng, 40)

    # --- documentos (tipo real detectable por estructura, no por extensión) ---
    archivos["documentos/reporte.pdf"] = (
        b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n" + _texto_legible(rng, 5) + b"\n%%EOF\n"
    )
    archivos["documentos/contrato.docx"] = _zip_bytes(
        {
            "[Content_Types].xml": b"<Types/>",
            "word/document.xml": b"<w:document><w:body>Contrato de servicios</w:body></w:document>",
        }
    )
    archivos["documentos/inventario.xlsx"] = _zip_bytes(
        {
            "[Content_Types].xml": b"<Types/>",
            "xl/workbook.xml": b"<workbook><sheets><sheet name='Hoja1'/></sheets></workbook>",
        }
    )

    # --- extensión mentirosa: dice .jpg pero ES un CSV (T1 debe mandarlo a HOT) ---
    archivos["fotos/vacaciones.jpg"] = _csv(rng, 50)

    # --- multimedia y binarios reales (tipos no objetivo → COLD por K3) ---
    archivos["multimedia/foto_real.jpg"] = b"\xff\xd8\xff\xe0\x00\x10JFIF" + rng.randbytes(20_000)
    archivos["multimedia/video.mp4"] = b"\x00\x00\x00\x18ftypmp42" + rng.randbytes(50_000)
    archivos["binarios/app.exe"] = b"MZ\x90\x00" + rng.randbytes(30_000)

    # --- basura de T0 (kill-rules K1: nombre, extensión, ruta de caché, 0 bytes) ---
    archivos["basura/temporal.tmp"] = rng.randbytes(100)
    archivos["basura/thumbs.db"] = rng.randbytes(512)
    archivos["basura/vacio.dat"] = b""
    archivos["proyecto/node_modules/lib/index.js"] = b"module.exports = {};\n"

    # --- duplicados exactos entre rutas distintas (dedup: un solo blob) ---
    # CSV a propósito: van a HOT y ejercitan el dedup del worker en el flujo principal
    contenido_duplicado = _csv(rng, 40)
    archivos["duplicados/copia_a.csv"] = contenido_duplicado
    archivos["duplicados/sub/copia_b.csv"] = contenido_duplicado

    # --- contenedores: cajas dentro de cajas (T3 recursivo, BFS) ---
    caja_interna = _zip_bytes({"datos_internos.csv": _csv(rng, 30)})
    archivos["contenedores/cajas.zip"] = _zip_bytes(
        {"leeme.txt": b"caja exterior", "caja2.zip": caja_interna}
    )

    # --- zip-bomb de juguete: 4 MiB de ceros → ratio de compresión >> 100 (guard K4) ---
    archivos["contenedores/bomba.zip"] = _zip_bytes({"relleno.bin": b"\x00" * (4 * 1024 * 1024)})

    for ruta_rel, datos in archivos.items():
        ruta = destino / ruta_rel
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_bytes(datos)

    return {
        "archivos": len(archivos),
        "utiles_tabulares": 6,  # 2 csv + ndjson + jpg-mentiroso + 2 duplicados csv
        "contenedores": 2,
        "duplicados_pares": 1,
        "basura_t0": 4,
        "no_objetivo": 3,
    }


def hash_arbol(raiz: Path) -> str:
    """Huella sha256 del árbol completo (rutas relativas + contenidos)."""
    h = hashlib.sha256()
    for ruta in sorted(p for p in raiz.rglob("*") if p.is_file()):
        h.update(str(ruta.relative_to(raiz)).replace("\\", "/").encode("utf-8"))
        h.update(b"\x00")
        h.update(ruta.read_bytes())
    return h.hexdigest()
