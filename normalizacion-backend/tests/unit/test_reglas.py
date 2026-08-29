"""Tests del Filtro 1 (T0-T2): funciones puras, sin base de datos.

Incluye los invariantes del DoD de la Fase 1.5: determinismo y never-trust-extension.
"""

from __future__ import annotations

import random
import zipfile
from pathlib import Path

from normalizacion.core.config import PerillasFiltro
from normalizacion.core.modelo import RutaDecision
from normalizacion.ingesta.precalificacion.reglas import (
    analizar_head,
    detectar_tipo,
    entropia_shannon,
    evaluar_t0,
    precalificar_archivo,
    refinar_tipo_texto,
)

PERILLAS = PerillasFiltro()

CSV = b"id,nombre,monto\n1,ana,10.5\n2,luis,22.0\n3,eva,9.99\n4,juan,1.25\n"


class TestT0KillRules:
    def test_archivo_vacio(self) -> None:
        motivo = evaluar_t0(PERILLAS, nombre="x.dat", extension=".dat", ruta="a/x.dat", tamano=0)
        assert motivo == "kill_t0:vacio"

    def test_nombre_basura(self) -> None:
        motivo = evaluar_t0(
            PERILLAS, nombre="Thumbs.db", extension=".db", ruta="f/Thumbs.db", tamano=512
        )
        assert motivo == "kill_t0:nombre_basura"

    def test_extension_basura(self) -> None:
        motivo = evaluar_t0(PERILLAS, nombre="a.TMP", extension=".tmp", ruta="a.TMP", tamano=9)
        assert motivo == "kill_t0:extension_basura"

    def test_ruta_de_cache(self) -> None:
        motivo = evaluar_t0(
            PERILLAS,
            nombre="index.js",
            extension=".js",
            ruta=r"proyecto\node_modules\lib\index.js",
            tamano=20,
        )
        assert motivo == "kill_t0:ruta_cache"

    def test_archivo_normal_sobrevive(self) -> None:
        motivo = evaluar_t0(
            PERILLAS, nombre="ventas.csv", extension=".csv", ruta="datos/ventas.csv", tamano=100
        )
        assert motivo is None


class TestEntropia:
    def test_ceros_es_cero(self) -> None:
        assert entropia_shannon(b"\x00" * 4096) == 0.0

    def test_aleatorio_supera_umbral_comprimido(self) -> None:
        datos = random.Random(7).randbytes(8192)
        assert entropia_shannon(datos) > PERILLAS.entropia_comprimido_min

    def test_texto_queda_bajo_umbral_texto(self) -> None:
        texto = (b"el sistema no tolera perdida de datos. " * 200)[:4096]
        assert entropia_shannon(texto) < PERILLAS.entropia_texto_max + 1.0


class TestDeteccionT1:
    def test_pdf_por_firma(self) -> None:
        d = detectar_tipo(b"%PDF-1.7 blah")
        assert d.tipo == "application/pdf" and not d.es_contenedor

    def test_jpeg_por_firma(self) -> None:
        assert detectar_tipo(b"\xff\xd8\xff\xe0resto").tipo == "image/jpeg"

    def test_exe_por_firma(self) -> None:
        assert detectar_tipo(b"MZ\x90\x00resto").tipo == "application/x-dosexec"

    def test_ole_legado(self) -> None:
        d = detectar_tipo(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100)
        assert d.tipo == "application/x-ole-storage"

    def test_zip_office_docx(self, tmp_path: Path) -> None:
        """El detector estructural distingue DOCX de ZIP genérico (libmagic no puede)."""
        ruta = tmp_path / "x.bin"  # extensión irrelevante a propósito
        with zipfile.ZipFile(ruta, "w") as zf:
            zf.writestr("word/document.xml", "<w:document/>")
        head = ruta.read_bytes()[:4096]
        d = detectar_tipo(head, ruta)
        assert d.tipo is not None and d.tipo.endswith("wordprocessingml.document")
        assert not d.es_contenedor

    def test_zip_generico_es_contenedor(self, tmp_path: Path) -> None:
        ruta = tmp_path / "c.zip"
        with zipfile.ZipFile(ruta, "w") as zf:
            zf.writestr("cualquier.txt", "hola")
        d = detectar_tipo(ruta.read_bytes()[:4096], ruta)
        assert d.tipo == "application/zip" and d.es_contenedor

    def test_nulos_sin_bom_es_binario_opaco(self) -> None:
        assert detectar_tipo(b"abc\x00def" * 100).tipo == "application/octet-stream"

    def test_texto_queda_para_t2(self) -> None:
        """El texto plano NO se cierra como binario opaco en T1: sigue a T2.

        El resultado exacto depende de si libmagic está instalado —es una capa
        OPCIONAL de T1— y ambas ramas son correctas:
          · sin libmagic (Mac de dev): `tipo=None`, detector "texto" → decide T2.
          · con libmagic (Linux, imagen de producción): lo tipifica como `text/*`,
            que es mejor información y sigue rutando como texto.
        Antes se exigía sólo la primera, así que el test pasaba o fallaba según la
        máquina — medía el entorno, no la conducta."""
        from normalizacion.ingesta.precalificacion.reglas import _LIBMAGIC

        d = detectar_tipo(CSV)
        if _LIBMAGIC is None:
            assert d.tipo is None and d.detector == "texto"
        else:
            assert d.tipo is not None and d.tipo.startswith("text/")
        assert not d.es_contenedor


class TestSenalesT2:
    def test_csv_consistente(self) -> None:
        senales = analizar_head(PERILLAS, CSV)
        assert senales["es_csv"] is True
        assert senales["columnas"] == 3
        assert refinar_tipo_texto(senales) == "text/csv"

    def test_ndjson(self) -> None:
        buf = b'{"a": 1}\n{"a": 2}\n{"a": 3}\n'
        senales = analizar_head(PERILLAS, buf)
        assert senales["es_ndjson"] is True
        assert refinar_tipo_texto(senales) == "application/x-ndjson"

    def test_json_multilinea(self) -> None:
        buf = b'{\n  "clave": [1, 2, 3]\n}\n'
        senales = analizar_head(PERILLAS, buf)
        assert senales["es_json"] is True

    def test_xml(self) -> None:
        senales = analizar_head(PERILLAS, b'<?xml version="1.0"?><raiz/>')
        assert senales["es_xml"] is True
        assert refinar_tipo_texto(senales) == "application/xml"

    def test_texto_plano(self) -> None:
        senales = analizar_head(PERILLAS, b"esto es una nota cualquiera sin estructura\n" * 5)
        assert senales["texto_legible"] is True
        assert refinar_tipo_texto(senales) == "text/plain"


class TestPrecalificarArchivo:
    def _csv_en(self, tmp_path: Path, nombre: str) -> Path:
        ruta = tmp_path / nombre
        ruta.write_bytes(CSV * 30)
        return ruta

    def test_csv_va_a_hot(self, tmp_path: Path) -> None:
        ruta = self._csv_en(tmp_path, "ventas.csv")
        r = precalificar_archivo(
            PERILLAS,
            ruta,
            nombre="ventas.csv",
            extension=".csv",
            ruta_relativa="d/ventas.csv",
            tamano=ruta.stat().st_size,
        )
        assert r.ruta is RutaDecision.HOT
        assert r.tipo_real == "text/csv"
        assert r.puntaje >= PERILLAS.umbral_hot

    def test_extension_mentirosa_no_engana(self, tmp_path: Path) -> None:
        """INVARIANTE: un .jpg que ES un CSV se va a HOT como tabular (riesgo F4)."""
        ruta = self._csv_en(tmp_path, "vacaciones.jpg")
        r = precalificar_archivo(
            PERILLAS,
            ruta,
            nombre="vacaciones.jpg",
            extension=".jpg",
            ruta_relativa="fotos/vacaciones.jpg",
            tamano=ruta.stat().st_size,
        )
        assert r.ruta is RutaDecision.HOT
        assert r.tipo_real == "text/csv"
        assert r.senales["extension_miente"] is True

    def test_jpeg_real_va_a_cold(self, tmp_path: Path) -> None:
        """Lista BLANCA (default): lo que no es de interés va a frío reversible."""
        ruta = tmp_path / "foto.jpg"
        ruta.write_bytes(b"\xff\xd8\xff\xe0" + random.Random(1).randbytes(5000))
        r = precalificar_archivo(
            PERILLAS,
            ruta,
            nombre="foto.jpg",
            extension=".jpg",
            ruta_relativa="fotos/foto.jpg",
            tamano=ruta.stat().st_size,
        )
        assert r.ruta is RutaDecision.COLD
        assert r.motivo == "fuera_de_lista_blanca"

    def test_imagen_ilegible_va_a_hot_con_ocr_activo(self, tmp_path: Path) -> None:
        """Con `ocr_activo`, una imagen que no se puede clasificar va a HOT.

        Estos bytes son basura con firma JPEG: Pillow no los abre, así que el
        clasificador (Fase 3) no puede decidir. Ante la duda, HOT — recall primero:
        gastar OCR de más cuesta segundos, mandar un documento a frío por error lo
        deja fuera de toda consulta.

        El caso de una imagen que SÍ se puede clasificar está en `test_ocr_politica`,
        que construye PNGs válidos.
        """
        ruta = tmp_path / "escaneo.jpg"
        ruta.write_bytes(b"\xff\xd8\xff\xe0" + random.Random(1).randbytes(5000))
        r = precalificar_archivo(
            PerillasFiltro(ocr_activo=True),
            ruta,
            nombre="escaneo.jpg",
            extension=".jpg",
            ruta_relativa="fotos/escaneo.jpg",
            tamano=ruta.stat().st_size,
        )
        assert r.ruta is RutaDecision.HOT
        assert r.motivo.startswith("imagen_ocr")
        assert r.senales.get("ocr") is True

    def test_imagen_sigue_yendo_a_frio_sin_ocr(self, tmp_path: Path) -> None:
        """Sin `ocr_activo`, `image/*` no está en la lista blanca y va a frío. Es el
        estado por defecto, y el motivo por el que hoy no hay ni una imagen indexada."""
        ruta = tmp_path / "foto.jpg"
        ruta.write_bytes(b"\xff\xd8\xff\xe0" + random.Random(1).randbytes(5000))
        r = precalificar_archivo(
            PerillasFiltro(ocr_activo=False),
            ruta,
            nombre="foto.jpg",
            extension=".jpg",
            ruta_relativa="fotos/foto.jpg",
            tamano=ruta.stat().st_size,
        )
        assert r.ruta is RutaDecision.COLD
        assert r.motivo == "fuera_de_lista_blanca"

    def test_modo_lista_negra_legado(self, tmp_path: Path) -> None:
        """El modo negro (legacy) sigue disponible por configuración."""
        from normalizacion.core.config import PerillasFiltro

        ruta = tmp_path / "foto.jpg"
        ruta.write_bytes(b"\xff\xd8\xff\xe0" + random.Random(1).randbytes(2000))
        r = precalificar_archivo(
            PerillasFiltro(modo_lista="negra"),
            ruta,
            nombre="foto.jpg",
            extension=".jpg",
            ruta_relativa="foto.jpg",
            tamano=ruta.stat().st_size,
        )
        assert r.ruta is RutaDecision.COLD
        assert r.motivo == "tipo_no_objetivo"

    def test_sql_dump_es_de_interes(self, tmp_path: Path) -> None:
        """Decisión del usuario: los dumps SQL traen información tabular valiosa."""
        ruta = tmp_path / "respaldo.sql"
        ruta.write_bytes(
            b"-- dump\nCREATE TABLE clientes (id INT, nombre TEXT);\n"
            b"INSERT INTO clientes VALUES (1, 'ana');\n"
            b"INSERT INTO clientes VALUES (2, 'luis');\n" * 5
        )
        r = precalificar_archivo(
            PERILLAS,
            ruta,
            nombre="respaldo.sql",
            extension=".sql",
            ruta_relativa="respaldo.sql",
            tamano=ruta.stat().st_size,
        )
        assert r.tipo_real == "application/sql"
        assert r.ruta is RutaDecision.HOT

    def test_correo_eml_es_de_interes(self, tmp_path: Path) -> None:
        """Propuesta aceptada (2026-06-10): los correos entran a la lista blanca."""
        ruta = tmp_path / "mensaje.eml"
        ruta.write_bytes(
            b"Return-Path: <ana@ejemplo.mx>\r\n"
            b"Received: from servidor (10.0.0.1) by mx.ejemplo.mx\r\n"
            b"Message-ID: <abc123@ejemplo.mx>\r\n"
            b"MIME-Version: 1.0\r\n"
            b"Subject: contrato adjunto\r\n\r\n"
            b"Hola, te mando los datos: nombre, telefono, direccion.\r\n"
        )
        r = precalificar_archivo(
            PERILLAS,
            ruta,
            nombre="mensaje.eml",
            extension=".eml",
            ruta_relativa="mensaje.eml",
            tamano=ruta.stat().st_size,
        )
        assert r.tipo_real == "message/rfc822"
        assert r.ruta is RutaDecision.HOT

    def test_pst_por_firma_es_de_interes(self, tmp_path: Path) -> None:
        ruta = tmp_path / "buzon.pst"
        ruta.write_bytes(b"!BDN" + b"\x00" * 600)
        r = precalificar_archivo(
            PERILLAS,
            ruta,
            nombre="buzon.pst",
            extension=".pst",
            ruta_relativa="buzon.pst",
            tamano=ruta.stat().st_size,
        )
        assert r.tipo_real == "application/vnd.ms-outlook-pst"
        assert r.ruta is RutaDecision.HOT

    def test_html_excluido_va_a_frio(self, tmp_path: Path) -> None:
        """Decisión del usuario (2026-06-10): HTML = markup sin información orgánica."""
        ruta = tmp_path / "pagina.html"
        ruta.write_bytes(
            b"<!DOCTYPE html>\n<html><head><title>Inicio</title></head>"
            b"<body><p>bienvenido al portal</p></body></html>\n"
        )
        r = precalificar_archivo(
            PERILLAS,
            ruta,
            nombre="pagina.html",
            extension=".html",
            ruta_relativa="pagina.html",
            tamano=ruta.stat().st_size,
        )
        assert r.tipo_real == "text/html"
        assert r.ruta is RutaDecision.COLD
        assert r.motivo == "fuera_de_lista_blanca"

    def test_dbf_estructural_es_de_interes(self, tmp_path: Path) -> None:
        """DBF (dBase/FoxPro): el formato de los padrones legados — detector estructural."""
        cabecera = bytes([0x03, 99, 6, 10])  # versión dBase III + fecha 1999-06-10
        cabecera += (3).to_bytes(4, "little")  # nº de registros
        cabecera += (97).to_bytes(2, "little")  # largo de cabecera (32 + 2*32 + 1)
        cabecera += (50).to_bytes(2, "little")  # largo de registro
        cabecera += b"\x00" * 20
        ruta = tmp_path / "padron.dbf"
        ruta.write_bytes(cabecera + b"\x20" * 200)
        r = precalificar_archivo(
            PERILLAS,
            ruta,
            nombre="padron.dbf",
            extension=".dbf",
            ruta_relativa="padron.dbf",
            tamano=ruta.stat().st_size,
        )
        assert r.tipo_real == "application/x-dbf"
        assert r.ruta is RutaDecision.HOT

    def test_dumps_de_bases_por_firma(self, tmp_path: Path) -> None:
        casos = [
            ("respaldo.dump", b"PGDMP" + b"\x00\x01" * 100, "application/x-pgdump"),
            ("carta.wpd", b"\xffWPC" + b"\x00" * 100, "application/vnd.wordperfect"),
            ("escaneo.djvu", b"AT&TFORM" + b"\x00" * 100, "image/vnd.djvu"),
        ]
        for nombre, contenido, tipo_esperado in casos:
            ruta = tmp_path / nombre
            ruta.write_bytes(contenido)
            r = precalificar_archivo(
                PERILLAS,
                ruta,
                nombre=nombre,
                extension=Path(nombre).suffix,
                ruta_relativa=nombre,
                tamano=ruta.stat().st_size,
            )
            assert r.tipo_real == tipo_esperado, nombre
            assert r.ruta is RutaDecision.HOT, nombre

    def test_imagen_de_disco_es_contenedor_preservado(self, tmp_path: Path) -> None:
        """VHDX/ISO/etc.: contenedores sin exploración aún → jamás a frío."""
        ruta = tmp_path / "maquina.vhdx"
        ruta.write_bytes(b"vhdxfile" + b"\x00" * 2000)
        r = precalificar_archivo(
            PERILLAS,
            ruta,
            nombre="maquina.vhdx",
            extension=".vhdx",
            ruta_relativa="maquina.vhdx",
            tamano=ruta.stat().st_size,
        )
        assert r.tipo_real == "application/x-vhdx"
        assert r.senales["es_contenedor"] is True
        assert r.ruta is RutaDecision.HOT

    def test_sqlite_y_access_y_parquet_de_interes(self, tmp_path: Path) -> None:
        casos = [
            ("datos.db", b"SQLite format 3\x00" + b"\x00" * 100, "application/vnd.sqlite3"),
            (
                "base.mdb",
                b"\x00\x01\x00\x00Standard Jet DB" + b"\x00" * 50,
                "application/x-msaccess",
            ),
            ("tabla.parquet", b"PAR1" + bytes(range(256)) * 4, "application/vnd.apache.parquet"),
        ]
        for nombre, contenido, tipo_esperado in casos:
            ruta = tmp_path / nombre
            ruta.write_bytes(contenido)
            r = precalificar_archivo(
                PERILLAS,
                ruta,
                nombre=nombre,
                extension=Path(nombre).suffix,
                ruta_relativa=nombre,
                tamano=ruta.stat().st_size,
            )
            assert r.tipo_real == tipo_esperado, nombre
            assert r.ruta is RutaDecision.HOT, nombre

    def test_contenedor_se_preserva_en_hot(self, tmp_path: Path) -> None:
        ruta = tmp_path / "c.zip"
        with zipfile.ZipFile(ruta, "w") as zf:
            zf.writestr("x.txt", "hola")
        r = precalificar_archivo(
            PERILLAS,
            ruta,
            nombre="c.zip",
            extension=".zip",
            ruta_relativa="c.zip",
            tamano=ruta.stat().st_size,
        )
        assert r.ruta is RutaDecision.HOT
        assert r.motivo == "contenedor_pendiente_t3"
        assert r.senales["es_contenedor"] is True
        # PRIORIDAD del usuario: los comprimidos van primero en la cola
        assert r.puntaje == PERILLAS.prioridad_contenedores
        assert r.puntaje > PERILLAS.umbral_hot

    def test_es_determinista(self, tmp_path: Path) -> None:
        """INVARIANTE (DoD): mismo archivo → mismo puntaje, siempre."""
        ruta = self._csv_en(tmp_path, "x.csv")
        kwargs: dict[str, object] = {
            "nombre": "x.csv",
            "extension": ".csv",
            "ruta_relativa": "x.csv",
            "tamano": ruta.stat().st_size,
        }
        a = precalificar_archivo(PERILLAS, ruta, **kwargs)  # type: ignore[arg-type]
        b = precalificar_archivo(PERILLAS, ruta, **kwargs)  # type: ignore[arg-type]
        assert a == b

    def test_gris_respeta_la_perilla(self, tmp_path: Path) -> None:
        """Texto plano chico cae en la franja gris: la perilla decide su destino sin T4."""
        ruta = tmp_path / "nota.txt"
        ruta.write_bytes(
            b"primera linea de una nota breve\n"
            b"otra linea distinta sin delimitadores\n"
            b"y un cierre cualquiera del texto\n"
        )
        comunes: dict[str, object] = {
            "nombre": "nota.txt",
            "extension": ".txt",
            "ruta_relativa": "nota.txt",
            "tamano": ruta.stat().st_size,
        }
        a_hot = precalificar_archivo(PERILLAS, ruta, **comunes)  # type: ignore[arg-type]
        assert a_hot.ruta is RutaDecision.HOT
        assert a_hot.motivo.startswith("gris_sin_t4")

        conservador = PerillasFiltro(gris_sin_t4_a_hot=False)
        a_cold = precalificar_archivo(conservador, ruta, **comunes)  # type: ignore[arg-type]
        assert a_cold.ruta is RutaDecision.COLD
