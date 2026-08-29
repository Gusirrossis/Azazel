"""Caché de extracción por contenido: leer una vez lo que ya se leyó.

El almacén deduplica los BYTES; esto deduplica el TRABAJO de entenderlos. Dos rutas
con el mismo archivo comparten `hash_contenido`, así que comparten también su texto,
sus campos y su confianza — y el OCR corre una sola vez.

Además guarda con qué versión de extractor se produjo cada resultado, que es lo que
convierte "mejoramos el OCR" en algo accionable: `norm reextraer` consulta esta tabla
para saber qué rehacer y lee los bytes del almacén, sin depender del disco original.

VERSION_EXTRACTOR es la clave de invalidación. Súbela cuando un cambio deba rehacer el
trabajo ya hecho (motor de OCR, preprocesado, límites); NO la subas por un arreglo que
no cambia la salida, o se tira a la basura el corpus entero de extracciones.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from normalizacion.core.config import Config
from normalizacion.core.modelo import sanear_texto
from normalizacion.core.observabilidad import obtener_logger

log = obtener_logger("cache_extraccion")

#: Versión del contrato de extracción. Subirla invalida la caché (ver arriba).
#:   v1 — línea base: OCR sin confianza ni preprocesado.
#:   v2 — confianza por palabra (image_to_data), deskew + Otsu, 300 dpi, plazo cooperativo.
VERSION_EXTRACTOR = "v2"

#: Banderas que marcan un resultado INCOMPLETO o FALLIDO. Nunca se cachean: hacerlo
#: convertía un fallo transitorio (OpenSearch caído, tesseract sin instalar todavía,
#: un timeout puntual) en la respuesta definitiva para ese contenido y para todas sus
#: copias — y como la fila quedaba con la versión al día, `reextraer` tampoco lo veía.
_BANDERAS_NO_CACHEABLES = (
    "extraccion_timeout",
    "extraccion_fallida",
    "ocr_no_disponible",
    "ocr_omitido_sin_tiempo",
    "ocr_pdf_parcial",
    "texto_parcial",
    "ocr_pdf_fallido",
)

_TIMEOUT = 5


def es_cacheable(flags: list[str]) -> bool:
    """¿Este resultado es definitivo, o fruto de un fallo/interrupción?"""
    return not any(f.startswith(_BANDERAS_NO_CACHEABLES) for f in flags)


def clave_version(config: Config) -> str:
    """Versión efectiva del extractor, con la config que CAMBIA LA SALIDA dentro.

    `ocr_activo` tiene que formar parte de la clave: una extracción hecha con el OCR
    apagado (un PDF escaneado = cero texto) es un resultado perfectamente válido para
    esa configuración, pero servirla como caché al encender el OCR significaba que
    encenderlo no volvía a extraer NADA de lo ya procesado — la función se quedaba sin
    efecto sobre el corpus existente, que es justo donde tiene que actuar.
    """
    sufijo = "+ocr" if config.filtro.ocr_activo else "-ocr"
    return f"{VERSION_EXTRACTOR}{sufijo}"


@dataclass(frozen=True, slots=True)
class Extraccion:
    """Una extracción ya hecha, tal como se guardó."""

    hash_contenido: str
    texto: str | None
    campos: dict[str, Any]
    perfil_calidad: dict[str, Any] | None
    flags: list[str]
    confianza: float | None
    motor: str
    version_extractor: str


def _conectar(config: Config) -> psycopg.Connection:
    return psycopg.connect(config.postgres_dsn, connect_timeout=_TIMEOUT)


def motor_de(flags: list[str], texto: str | None) -> str:
    """De qué salió el texto, mirando las banderas que dejó el extractor.

    Distinguirlo importa para el reproceso: un PDF 'nativo' sin texto no mejora por
    mucho que se afine el OCR (no se le ha intentado), mientras que uno 'ocr' con
    confianza baja es justo el candidato a rehacer.

    Cualquier bandera `ocr_*` significa que el OCR SE INTENTÓ, aunque no diera texto.
    Antes solo contaban las de éxito, así que un OCR intentado y fallido se etiquetaba
    'nativo' — y `norm reextraer --motor ocr` no lo veía nunca, que es justo el
    conjunto que más falta hace reprocesar cuando el OCR mejora.
    """
    if any(f.startswith("ocr_") for f in flags):
        return "ocr" if texto else "ocr_sin_texto"
    return "nativo"


def buscar(
    conn: psycopg.Connection[Any], hash_contenido: str, *, version: str
) -> Extraccion | None:
    """¿Ya extrajimos este contenido con la versión actual? Marca el reuso.

    Recibe la conexión del worker en vez de abrir una propia: esto corre una vez por
    archivo, dentro del bucle caliente, y abrir una conexión por archivo costaría más
    que la propia consulta.
    """
    fila = conn.execute(
        "UPDATE extracciones SET reusos = reusos + 1"
        " WHERE hash_contenido = %s AND version_extractor = %s"
        " RETURNING texto, campos, perfil_calidad, flags, confianza, motor",
        (hash_contenido, version),
    ).fetchone()
    if fila is None:
        return None
    return Extraccion(
        hash_contenido=hash_contenido,
        texto=fila[0],
        campos=dict(fila[1] or {}),
        perfil_calidad=fila[2],
        flags=list(fila[3] or []),
        confianza=fila[4],
        motor=fila[5],
        version_extractor=version,
    )


def guardar(
    conn: psycopg.Connection[Any],
    hash_contenido: str,
    *,
    tipo_real: str | None,
    texto: str | None,
    campos: dict[str, Any],
    perfil_calidad: dict[str, Any] | None,
    flags: list[str],
    confianza: float | None,
    ms: int,
    version: str,
) -> None:
    """Guarda (o reemplaza) el resultado. Idempotente y sin carrera.

    Dos workers pueden extraer el mismo contenido a la vez si lo reclamaron por rutas
    distintas antes de que ninguno terminara; el `ON CONFLICT` deja que gane el último
    sin reventar. Son resultados equivalentes, así que cuál gane da igual.
    """
    conn.execute(
        "INSERT INTO extracciones"
        " (hash_contenido, tipo_real, version_extractor, motor, texto, campos,"
        "  perfil_calidad, flags, confianza, chars, ms)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
        " ON CONFLICT (hash_contenido) DO UPDATE SET"
        "  tipo_real = EXCLUDED.tipo_real, version_extractor = EXCLUDED.version_extractor,"
        "  motor = EXCLUDED.motor, texto = EXCLUDED.texto, campos = EXCLUDED.campos,"
        "  perfil_calidad = EXCLUDED.perfil_calidad, flags = EXCLUDED.flags,"
        "  confianza = EXCLUDED.confianza, chars = EXCLUDED.chars, ms = EXCLUDED.ms,"
        "  creado_en = now()",
        (
            hash_contenido,
            tipo_real,
            version,
            motor_de(flags, texto),
            sanear_texto(texto) if texto else None,
            Jsonb(campos),
            Jsonb(perfil_calidad) if perfil_calidad is not None else None,
            Jsonb(flags),
            confianza,
            len(texto or ""),
            ms,
        ),
    )


def candidatos_reproceso(
    config: Config,
    *,
    version_distinta_de: str = VERSION_EXTRACTOR,
    confianza_menor_a: float | None = None,
    motor: str | None = None,
    tipo_real: str | None = None,
    con_bandera: str | None = None,
    limite: int = 1000,
) -> list[tuple[str, str | None, float | None]]:
    """Contenidos que conviene volver a extraer: `(hash, tipo_real, confianza)`.

    Ordenados por confianza ASCENDENTE — lo peor primero. Si solo hay presupuesto para
    la mitad, que sea la mitad que más gana.
    """
    condiciones: list[str] = []
    params: list[Any] = []
    if version_distinta_de:
        condiciones.append("version_extractor <> %s")
        params.append(version_distinta_de)
    if confianza_menor_a is not None:
        # `IS NULL` fuera: sin OCR no hay confianza que mejorar, y arrastraría todo
        # el corpus de CSVs y textos nativos a la cola de reproceso.
        condiciones.append("confianza IS NOT NULL AND confianza < %s")
        params.append(confianza_menor_a)
    if motor:
        condiciones.append("motor = %s")
        params.append(motor)
    if tipo_real:
        condiciones.append("tipo_real LIKE %s")
        params.append(tipo_real.replace("*", "%"))
    if con_bandera:
        condiciones.append("flags @> %s")
        params.append(Jsonb([con_bandera]))

    donde = f" WHERE {' AND '.join(condiciones)}" if condiciones else ""
    params.append(limite)
    with _conectar(config) as conn:
        filas = conn.execute(
            "SELECT hash_contenido, tipo_real, confianza FROM extracciones"
            f"{donde} ORDER BY confianza ASC NULLS LAST LIMIT %s",
            tuple(params),
        ).fetchall()
    return [(f[0], f[1], f[2]) for f in filas]


def estadisticas(config: Config) -> dict[str, Any]:
    """Resumen para el panel y el `doctor`: cuánto se reusó y cómo va la calidad."""
    with _conectar(config) as conn:
        fila = conn.execute(
            "SELECT count(*), coalesce(sum(reusos), 0), avg(confianza),"
            "       count(*) FILTER (WHERE motor = 'ocr'),"
            "       count(*) FILTER (WHERE confianza IS NOT NULL AND confianza < 60),"
            "       coalesce(sum(ms), 0)"
            " FROM extracciones"
        ).fetchone()
    if fila is None:
        return {}
    total, reusos, confianza_media, con_ocr, dudosas, ms_total = fila
    return {
        "extracciones": total,
        "reusos": reusos,
        # Lo que se habría gastado de más sin la caché, con el coste medio real.
        "ms_ahorrados": int((ms_total / total) * reusos) if total else 0,
        "confianza_media": round(float(confianza_media), 1) if confianza_media else None,
        "con_ocr": con_ocr,
        "confianza_baja": dudosas,
    }
