"""Filtro 1 de la precalificación: cascada de reglas deterministas T0-T2 (funciones puras).

Diseño (DISENO_T0_T4_Y_PIPELINE.md):
- T0: kill-rules sobre metadata, sin abrir el archivo.
- T1: tipo real — detectores ESTRUCTURALES primero (OLE, ZIP-Office: 100% certeros,
  lección tika/unstructured), luego firmas binarias, luego libmagic SI está disponible
  (opcional: en Windows dev no hay libmagic; en los workers Linux siempre estará).
  La extensión JAMÁS decide: solo aporta la señal "extension_coincide".
- T2: estructura del head (entropía estilo binwalk, csv.Sniffer + consistencia de
  columnas, JSON/NDJSON/XML, encoding + ratio de imprimibles).
- Puntaje = base + suma de (señal por peso) con overrides; umbrales del router en config (K8).

INVARIANTE (con test): mismas entradas → mismo resultado. Nada aquí toca la base de datos.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from normalizacion.core.config import PerillasFiltro
from normalizacion.core.modelo import RutaDecision

# libmagic es opcional: capa adicional en Linux; los detectores estructurales no la necesitan
try:  # pragma: no cover - depende del sistema
    import magic as _magic  # type: ignore[import-not-found,import-untyped,unused-ignore]

    _LIBMAGIC: Any | None = _magic.Magic(mime=True)
except Exception:  # pragma: no cover
    _LIBMAGIC = None


# ---------------------------------------------------------------- T1: firmas

_FIRMAS: tuple[tuple[int, bytes, str], ...] = (
    (0, b"%PDF", "application/pdf"),
    (0, b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "application/x-ole-storage"),
    (0, b"\xff\xd8\xff", "image/jpeg"),
    (0, b"\x89PNG\r\n\x1a\n", "image/png"),
    (0, b"GIF8", "image/gif"),
    (0, b"RIFF", "audio/x-riff"),
    (0, b"ID3", "audio/mpeg"),
    (0, b"MZ", "application/x-dosexec"),
    (0, b"\x7fELF", "application/x-executable"),
    (0, b"\x1f\x8b", "application/gzip"),
    (0, b"7z\xbc\xaf\x27\x1c", "application/x-7z-compressed"),
    (0, b"Rar!\x1a\x07", "application/x-rar-compressed"),
    (0, b"BZh", "application/x-bzip2"),
    (0, b"SQLite format 3\x00", "application/vnd.sqlite3"),
    (0, b"{\\rtf", "application/rtf"),
    (0, b"PAR1", "application/vnd.apache.parquet"),
    (0, b"!BDN", "application/vnd.ms-outlook-pst"),  # .pst/.ost
    (0, b"PGDMP", "application/x-pgdump"),  # pg_dump formato custom
    (0, b"Obj\x01", "application/avro"),
    (0, b"\xffWPC", "application/vnd.wordperfect"),
    (0, b"AT&TFORM", "image/vnd.djvu"),
    (0, b"\xcf\xad\x12\xfe", "application/x-dbx"),  # Outlook Express
    (0, b"\x1a\x00\x00\x04\x00\x00", "application/x-nsf"),  # Lotus Notes
    (0, b"\xfd7zXZ\x00", "application/x-xz"),
    # Imágenes de disco: contenedores PRESERVADOS sin explorar (exploración futura)
    (0, b"conectix", "application/x-vhd"),
    (0, b"vhdxfile", "application/x-vhdx"),
    (0, b"KDMV", "application/x-vmdk"),
    (0, b"QFI\xfb", "application/x-qcow"),
    (0, b"EVF\x09\x0d\x0a\xff\x00", "application/x-ewf"),  # E01 forense
    # Firmas CORTAS al final (3-4 bytes ASCII): un falso positivo va a HOT = ruido
    # recuperable, jamás pérdida (principio de recall del filtro)
    (0, b"TAPE", "application/x-mssql-backup"),  # .bak (MTF)
    (0, b"ORC", "application/orc"),
    (4, b"Standard Jet DB", "application/x-msaccess"),  # .mdb
    (4, b"Standard ACE DB", "application/x-msaccess"),  # .accdb
    (4, b"ftyp", "video/mp4"),
    (257, b"ustar", "application/x-tar"),
    (32769, b"CD001", "application/x-iso9660-image"),  # head T2 (64 KB) sí lo alcanza
)

CONTENEDORES: frozenset[str] = frozenset(
    {
        "application/zip",
        "application/gzip",
        "application/x-7z-compressed",
        "application/x-rar-compressed",
        "application/x-bzip2",
        "application/x-tar",
        "application/x-xz",
        # Imágenes de disco: pueden traer un filesystem completo de información →
        # se preservan ÍNTEGRAS en el almacén (formato_no_soportado) hasta que
        # exista exploración interna; jamás van a frío
        "application/x-iso9660-image",
        "application/x-vhd",
        "application/x-vhdx",
        "application/x-vmdk",
        "application/x-qcow",
        "application/x-ewf",
    }
)

DOCUMENTOS: frozenset[str] = frozenset(
    {
        "application/pdf",
        "application/rtf",
        "application/x-ole-storage",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.oasis.opendocument.text",
        "application/vnd.oasis.opendocument.presentation",
        "application/epub+zip",
        "message/rfc822",
        "application/vnd.ms-outlook-pst",
        "application/vnd.wordperfect",
        "image/vnd.djvu",
        "application/vnd.ms-xpsdocument",
        "application/x-iwork",
        "application/x-dbx",
        "application/x-nsf",
    }
)

TABULARES: frozenset[str] = frozenset(
    {
        "text/csv",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.oasis.opendocument.spreadsheet",
        "application/vnd.sqlite3",
        "application/x-ndjson",
        "application/json",
        "application/sql",
        "application/x-msaccess",
        "application/vnd.apache.parquet",
        "application/x-dbf",
        "application/x-pgdump",
        "application/x-mssql-backup",
        "application/avro",
        "application/orc",
    }
)

# Dumps SQL: texto plano con datos tabulares valiosísimos (decisión del usuario)
_SQL_RE = re.compile(
    r"(?im)^\s*(CREATE\s+TABLE|INSERT\s+INTO|SELECT\s+.+\s+FROM|ALTER\s+TABLE|COPY\s+\w+)"
)

# Correos .eml/mbox: texto con cabeceras RFC 822 (≥2 cabeceras distintas = correo)
_EML_RE = re.compile(r"(?im)^(received|return-path|delivered-to|message-id|mime-version):")

# DBF (dBase/FoxPro) no tiene magic confiable: detector ESTRUCTURAL del header
# (versión conocida + fecha válida + longitudes sanas). Formato clave en sistemas
# legados de gobierno (padrones, nóminas).
_DBF_VERSIONES = frozenset(
    {0x02, 0x03, 0x30, 0x31, 0x32, 0x43, 0x63, 0x83, 0x8B, 0x8E, 0xCB, 0xE5, 0xF5, 0xFB}
)


def _es_dbf(buf: bytes) -> bool:
    if len(buf) < 32 or buf[0] not in _DBF_VERSIONES:
        return False
    mes, dia = buf[2], buf[3]
    largo_cabecera = int.from_bytes(buf[8:10], "little")
    largo_registro = int.from_bytes(buf[10:12], "little")
    return 1 <= mes <= 12 and 1 <= dia <= 31 and largo_cabecera >= 33 and largo_registro >= 1


def pasa_lista(perillas: PerillasFiltro, tipo: str) -> bool:
    """¿Este tipo real entra al embudo caro? (lista blanca o negra según config)."""
    if perillas.modo_lista == "blanca":
        if tipo in perillas.tipos_excluidos:  # excepciones a los prefijos (ej. text/html)
            return False
        return tipo in perillas.tipos_interes or tipo.startswith(perillas.tipos_interes_prefijos)
    return not (
        tipo in perillas.tipos_no_objetivo or tipo.startswith(perillas.tipos_no_objetivo_prefijos)
    )


def motivo_lista(perillas: PerillasFiltro) -> str:
    return "fuera_de_lista_blanca" if perillas.modo_lista == "blanca" else "tipo_no_objetivo"


_ZIP_OFFICE: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\.fdseq$"), "application/vnd.ms-xpsdocument"),
    (re.compile(r"^(Index/Document\.iwa|index\.apxl)$"), "application/x-iwork"),
    (
        re.compile(r"^word/document.*\.xml$"),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ),
    (
        re.compile(r"^xl/workbook.*\.xml$"),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ),
    (
        re.compile(r"^ppt/presentation.*\.xml$"),
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ),
)

_XML_INICIO = re.compile(rb"^\s*<\?xml|^\s*<[A-Za-z][\w:.-]*[\s>]")


@dataclass(frozen=True)
class ResultadoPrecalificacion:
    """Salida auditable del filtro para una fila (se guarda completa en la cola)."""

    puntaje: int
    ruta: RutaDecision
    tipo_real: str | None
    motivo: str
    senales: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------- T0


def evaluar_t0(
    perillas: PerillasFiltro, *, nombre: str, extension: str | None, ruta: str, tamano: int
) -> str | None:
    """Kill-rules sobre metadata pura (⚙K1). Devuelve el motivo del kill o None."""
    if tamano == 0:
        return "kill_t0:vacio"
    if nombre.lower() in perillas.kill_nombres:
        return "kill_t0:nombre_basura"
    if extension is not None and extension.lower() in perillas.kill_extensiones:
        return "kill_t0:extension_basura"
    ruta_norm = ruta.replace("\\", "/").lower()
    for patron in perillas.kill_rutas_patrones:
        if re.search(patron, ruta_norm):
            return "kill_t0:ruta_cache"
    return None


# ---------------------------------------------------------------- T1


@dataclass(frozen=True)
class DeteccionT1:
    tipo: str | None  # None = sin firma binaria → candidato a texto (lo refina T2)
    es_contenedor: bool
    detector: str  # quién decidió: zip_office | ole | firma | libmagic | nulos | texto


def detectar_tipo(buf: bytes, abrible: Path | IO[bytes] | None = None) -> DeteccionT1:
    """Cascada T1. Los detectores estructurales van ANTES que libmagic (tika/unstructured):
    libmagic reporta 'zip' genérico para todo Office moderno.

    `abrible` puede ser una ruta o un file-like seekable (entradas internas de T3)."""
    # ① ZIP: distinguir Office/ODT de contenedor genérico por sus entradas internas
    if buf.startswith(b"PK\x03\x04"):
        if abrible is not None:
            try:
                if not isinstance(abrible, Path):
                    abrible.seek(0)
                with zipfile.ZipFile(abrible) as zf:
                    nombres = zf.namelist()
                    for patron, tipo_office in _ZIP_OFFICE:
                        if any(patron.match(n) for n in nombres):
                            return DeteccionT1(tipo_office, False, "zip_office")
                    if "mimetype" in nombres:
                        mime = zf.read("mimetype").decode("ascii", "replace").strip()
                        return DeteccionT1(mime, False, "zip_office")
            except (zipfile.BadZipFile, OSError):
                pass  # zip corrupto/truncado: se preserva como contenedor genérico
        return DeteccionT1("application/zip", True, "firma")

    # ② OLE (Office legado: DOC/XLS/PPT/MSG)
    # ③ resto de firmas binarias por offset
    for offset, firma, tipo in _FIRMAS:
        if buf[offset : offset + len(firma)] == firma:
            return DeteccionT1(
                tipo,
                tipo in CONTENEDORES,
                "ole" if offset == 0 and firma.startswith(b"\xd0\xcf") else "firma",
            )

    # ③bis DBF: estructural (sin magic); típico de padrones/sistemas legados
    if _es_dbf(buf):
        return DeteccionT1("application/x-dbf", False, "dbf")

    # ④ libmagic como capa adicional (si está disponible en esta máquina)
    if _LIBMAGIC is not None:  # pragma: no cover - depende del sistema
        try:
            tipo_magic = str(_LIBMAGIC.from_buffer(buf))
            if tipo_magic and tipo_magic not in ("text/plain", "application/octet-stream"):
                return DeteccionT1(tipo_magic, tipo_magic in CONTENEDORES, "libmagic")
        except Exception:
            pass

    # ⑤ ¿binario opaco o texto? (null bytes sin BOM UTF-16 → binario; heurística Tika)
    con_bom_utf16 = buf.startswith((b"\xff\xfe", b"\xfe\xff"))
    if not con_bom_utf16 and b"\x00" in buf[:1024]:
        return DeteccionT1("application/octet-stream", False, "nulos")
    return DeteccionT1(None, False, "texto")  # candidato a texto: T2 lo refina


# ---------------------------------------------------------------- T2


def entropia_shannon(datos: bytes) -> float:
    """Entropía en [0, 8] (binwalk): >7.5 comprimido/cifrado, <3.5 texto/estructurado."""
    if not datos:
        return 0.0
    total = len(datos)
    return -sum((c / total) * math.log2(c / total) for c in Counter(datos).values())


def _decodificar(buf: bytes) -> tuple[str | None, str]:
    """(texto, encoding). BOM primero; luego UTF-8; latin-1 como último recurso legible."""
    if buf.startswith(b"\xef\xbb\xbf"):
        return buf.decode("utf-8-sig", "replace"), "utf-8-sig"
    if buf.startswith(b"\xff\xfe"):
        return buf.decode("utf-16-le", "replace"), "utf-16-le"
    if buf.startswith(b"\xfe\xff"):
        return buf.decode("utf-16-be", "replace"), "utf-16-be"
    try:
        return buf.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return buf.decode("latin-1", "replace"), "latin-1"


def _es_tabular(texto: str, lineas_consistencia: int) -> tuple[bool, int]:
    """csv.Sniffer + consistencia de nº de columnas en las primeras N líneas."""
    lineas = [ln for ln in texto.splitlines() if ln.strip()][:lineas_consistencia]
    if len(lineas) < 3:
        return False, 0
    muestra = "\n".join(lineas)
    try:
        dialecto = csv.Sniffer().sniff(muestra, delimiters=",;\t|")
    except csv.Error:
        return False, 0
    conteos = [len(f) for f in csv.reader(io.StringIO(muestra), dialecto)]
    consistente = len(set(conteos)) == 1 and conteos[0] >= 2
    return consistente, conteos[0] if consistente else 0


def _es_json_o_ndjson(texto: str) -> tuple[bool, bool]:
    lineas = [ln for ln in texto.splitlines() if ln.strip()]
    if not lineas:
        return False, False
    try:
        primero = json.loads(lineas[0])
    except (json.JSONDecodeError, ValueError):
        # JSON multilínea: intentar el bloque completo (head truncado puede fallar — ok)
        try:
            json.loads(texto)
            return True, False
        except (json.JSONDecodeError, ValueError):
            return False, False
    if isinstance(primero, dict) and len(lineas) >= 2:
        try:
            if isinstance(json.loads(lineas[1]), dict):
                return True, True  # dict por línea = NDJSON
        except (json.JSONDecodeError, ValueError):
            pass
    return True, False


def analizar_head(perillas: PerillasFiltro, buf: bytes) -> dict[str, Any]:
    """Señales T2 del head (8-64 KB, ⚙K5/K6). Todas se guardan: auditables y features del T4."""
    senales: dict[str, Any] = {"entropia": round(entropia_shannon(buf), 3)}
    texto, encoding = _decodificar(buf)
    senales["encoding"] = encoding
    if texto:
        imprimibles = sum(1 for c in texto if c.isprintable() or c in "\n\r\t")
        senales["ratio_imprimibles"] = round(imprimibles / len(texto), 3)
    else:
        senales["ratio_imprimibles"] = 0.0

    legible = senales["ratio_imprimibles"] >= perillas.ratio_imprimibles_min
    senales["texto_legible"] = legible
    senales["es_csv"] = False
    senales["es_json"] = False
    senales["es_ndjson"] = False
    senales["es_xml"] = False
    senales["es_sql"] = False
    senales["es_correo"] = False
    senales["es_html"] = False
    if legible and texto:
        es_json, es_ndjson = _es_json_o_ndjson(texto)
        senales["es_json"], senales["es_ndjson"] = es_json, es_ndjson
        if not es_json:
            # cabeceras antes que SQL/CSV: el CUERPO de un correo puede contener ambos
            senales["es_correo"] = len({m.lower() for m in _EML_RE.findall(texto[:4096])}) >= 2
            bajo = texto[:2048].lower()
            senales["es_html"] = not senales["es_correo"] and (
                "<!doctype html" in bajo or "<html" in bajo
            )
            if not senales["es_correo"] and not senales["es_html"]:
                senales["es_sql"] = bool(_SQL_RE.search(texto[:8192]))
                if not senales["es_sql"]:
                    es_csv, columnas = _es_tabular(texto, perillas.lineas_consistencia_csv)
                    senales["es_csv"] = es_csv
                    if es_csv:
                        senales["columnas"] = columnas
            senales["es_xml"] = not senales["es_html"] and bool(_XML_INICIO.match(buf))
    return senales


def refinar_tipo_texto(senales: dict[str, Any]) -> str:
    """Para candidatos a texto, T2 fija el tipo real."""
    if senales.get("es_ndjson"):
        return "application/x-ndjson"
    if senales.get("es_json"):
        return "application/json"
    if senales.get("es_correo"):
        return "message/rfc822"
    if senales.get("es_html"):
        return "text/html"  # excluido de la lista blanca (decisión del usuario)
    if senales.get("es_sql"):
        return "application/sql"
    if senales.get("es_csv"):
        return "text/csv"
    if senales.get("es_xml"):
        return "application/xml"
    return "text/plain" if senales.get("texto_legible") else "application/octet-stream"


# ---------------------------------------------------------------- puntaje y router


def _extension_coincide(extension: str | None, tipo: str) -> bool:
    if not extension:
        return False
    mapa = {
        ".csv": "text/csv",
        ".tsv": "text/csv",
        ".json": "application/json",
        ".ndjson": "application/x-ndjson",
        ".xml": "application/xml",
        ".sql": "application/sql",
        ".db": "application/vnd.sqlite3",
        ".parquet": "application/vnd.apache.parquet",
        ".eml": "message/rfc822",
        ".mbox": "message/rfc822",
        ".pst": "application/vnd.ms-outlook-pst",
        ".msg": "application/x-ole-storage",
        ".txt": "text/plain",
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".zip": "application/zip",
    }
    return mapa.get(extension.lower()) == tipo


def puntuar(
    perillas: PerillasFiltro,
    *,
    tipo_real: str,
    senales: dict[str, Any],
    tamano: int,
    extension: str | None,
) -> tuple[int, str]:
    """Puntaje 1-100 = base + suma de (señal por peso) (K7). Devuelve (puntaje, dominante)."""
    pesos = perillas.pesos
    puntos = perillas.puntaje_base
    dominante = "sin_senal"

    es_dato = senales.get("es_csv", False) or tipo_real in TABULARES
    if es_dato:
        puntos += pesos["tabular"]
        dominante = "tabular"
    elif senales.get("es_json") or senales.get("es_ndjson") or senales.get("es_xml"):
        puntos += pesos["estructurado"]
        dominante = "estructurado"
    elif tipo_real in DOCUMENTOS:
        puntos += pesos["documento"]
        dominante = "documento"
    elif senales.get("texto_legible"):
        puntos += pesos["texto_legible"]
        dominante = "texto"

    estructurado = dominante in ("tabular", "estructurado")
    if estructurado and tamano > 100_000:
        puntos += pesos["tamano_contextual"]
    if estructurado and tamano < 200:
        puntos += pesos["minusculo_para_dato"]
    if _extension_coincide(extension, tipo_real):
        puntos += pesos["extension_coincide"]
    if (
        senales.get("entropia", 0.0) > perillas.entropia_comprimido_min
        and tipo_real not in CONTENEDORES
        and dominante == "sin_senal"
    ):
        puntos += pesos["comprimido_cifrado"]
        dominante = "comprimido_cifrado"
    if not senales.get("texto_legible") and dominante == "sin_senal":
        puntos += pesos["ruido_binario"]
        dominante = "ruido_binario"

    return max(0, min(100, puntos)), dominante


def precalificar_archivo(
    perillas: PerillasFiltro,
    ruta_archivo: Path,
    *,
    nombre: str,
    extension: str | None,
    ruta_relativa: str,
    tamano: int,
) -> ResultadoPrecalificacion:
    """Conveniencia para archivos del filesystem: lee el head y delega."""
    kill = evaluar_t0(
        perillas, nombre=nombre, extension=extension, ruta=ruta_relativa, tamano=tamano
    )
    if kill:
        return ResultadoPrecalificacion(0, RutaDecision.COLD, None, kill, {"tier": "T0"})
    with ruta_archivo.open("rb") as f:
        head = f.read(perillas.head_t2_bytes)
    return precalificar_contenido(
        perillas,
        head=head,
        abrible=ruta_archivo,
        nombre=nombre,
        extension=extension,
        ruta_relativa=ruta_relativa,
        tamano=tamano,
    )


def _rutear_imagen(
    perillas: PerillasFiltro,
    tipo: str,
    senales: dict[str, Any],
    abrible: Path | IO[bytes] | None,
) -> ResultadoPrecalificacion:
    """Decide el destino de una imagen cuando el OCR está activo (⚙ ocr_politica_imagen).

    El scoring de texto no sirve aquí: una imagen no tiene señales textuales en el head
    y siempre saldría COLD. Pero mandarlas TODAS a HOT (lo que se hacía antes) paga OCR
    por fotos y fondos de pantalla. El clasificador de `precalificacion.imagen` separa
    escaneo de fotografía mirando aspecto, saturación y proporción de blanco.
    """
    from normalizacion.ingesta.precalificacion import imagen as clasificador

    politica = perillas.ocr_politica_imagen
    senales["ocr"] = True
    senales["ocr_politica"] = politica

    if politica == "ninguna":
        return ResultadoPrecalificacion(
            5, RutaDecision.COLD, tipo, "imagen_ocr_desactivado", senales
        )

    if politica == "todas" or abrible is None:
        # Sin `abrible` no hay píxeles que mirar (entrada de contenedor ya consumida):
        # se manda a HOT, que es el lado seguro del error.
        return ResultadoPrecalificacion(
            perillas.umbral_hot, RutaDecision.HOT, tipo, "imagen_ocr", senales
        )

    fuente: IO[bytes] | None = None
    try:
        fuente = abrible.open("rb") if isinstance(abrible, Path) else abrible
        clase = clasificador.clasificar(fuente, ancho_min=perillas.imagen_ancho_min_documento)
    finally:
        if fuente is not None and isinstance(abrible, Path):
            fuente.close()

    senales["imagen"] = clase.senales
    if clase.es_documento:
        return ResultadoPrecalificacion(
            perillas.umbral_hot, RutaDecision.HOT, tipo, f"imagen_ocr:{clase.motivo}", senales
        )
    # No es documento: a frío, que es REVERSIBLE. Si mañana se decide OCR-ear también
    # las fotos, `rescore-frio` las devuelve a la cola sin haber perdido nada.
    return ResultadoPrecalificacion(
        5, RutaDecision.COLD, tipo, f"imagen_no_ocr:{clase.motivo}", senales
    )


def precalificar_contenido(
    perillas: PerillasFiltro,
    *,
    head: bytes,
    abrible: Path | IO[bytes] | None,
    nombre: str,
    extension: str | None,
    ruta_relativa: str,
    tamano: int,
) -> ResultadoPrecalificacion:
    """Orquesta T0 → T1 → T2 → puntaje → router sobre contenido ya leído. Determinista.

    Sirve igual para archivos del disco que para entradas internas de contenedores
    (T3), que llegan como file-like sin existir en el filesystem."""
    # T0 — sin tocar el contenido
    kill = evaluar_t0(
        perillas, nombre=nombre, extension=extension, ruta=ruta_relativa, tamano=tamano
    )
    if kill:
        return ResultadoPrecalificacion(0, RutaDecision.COLD, None, kill, {"tier": "T0"})

    # T1 — tipo real
    deteccion = detectar_tipo(head[: perillas.bytes_t1], abrible)
    senales: dict[str, Any] = {"tier": "T1", "detector": deteccion.detector}

    if deteccion.es_contenedor:
        # Los contenedores tienen PRIORIDAD (decisión del usuario: la mayoría de lo
        # útil viene dentro). T3 los explora; siempre se preservan íntegros.
        senales["es_contenedor"] = True
        return ResultadoPrecalificacion(
            perillas.prioridad_contenedores,
            RutaDecision.HOT,
            deteccion.tipo,
            "contenedor_pendiente_t3",
            senales,
        )

    tipo = deteccion.tipo
    if tipo is not None and perillas.ocr_activo and tipo.startswith("image/"):
        return _rutear_imagen(perillas, tipo, senales, abrible)
    if tipo is not None and not pasa_lista(perillas, tipo):
        return ResultadoPrecalificacion(5, RutaDecision.COLD, tipo, motivo_lista(perillas), senales)

    # T2 — estructura del head (solo texto/estructurado/documento llega aquí)
    senales.update(analizar_head(perillas, head))
    senales["tier"] = "T2"
    if tipo is None:
        tipo = refinar_tipo_texto(senales)
        if not pasa_lista(perillas, tipo):
            return ResultadoPrecalificacion(
                5, RutaDecision.COLD, tipo, motivo_lista(perillas), senales
            )

    senales["extension_miente"] = bool(extension) and not _extension_coincide(extension, tipo)
    puntaje, dominante = puntuar(
        perillas, tipo_real=tipo, senales=senales, tamano=tamano, extension=extension
    )

    # Router (⚙K8)
    if puntaje >= perillas.umbral_hot:
        return ResultadoPrecalificacion(puntaje, RutaDecision.HOT, tipo, dominante, senales)
    if puntaje < perillas.umbral_cold:
        return ResultadoPrecalificacion(puntaje, RutaDecision.COLD, tipo, dominante, senales)
    # Franja gris: sin T4 todavía → recall primero (⚙ gris_sin_t4_a_hot)
    ruta_gris = RutaDecision.HOT if perillas.gris_sin_t4_a_hot else RutaDecision.COLD
    return ResultadoPrecalificacion(puntaje, ruta_gris, tipo, f"gris_sin_t4:{dominante}", senales)
