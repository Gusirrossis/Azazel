"""RAR con `unrar` oficial: listar y extraer con la MISMA herramienta y no servir NUNCA una
entrada incompleta.

Incidente ('Matrix.rar' en la matriz, 24-09): un RAR5 sano (`unrar t`: rc=0, 0 errores).
`unar` dejó 45 entradas a 0 B y 3 truncadas, marcó la caché como completa y se sirvieron
como buenas: 41 acabaron en COLD como `application/x-empty` y 3 se indexaron sin su cola.
`lsar` leyó el tamaño de la entrada de 21,4 GB como 2^64 - 50.125.454, el guard de ratio
la apartó como bomba y nunca se encoló.

Revisión (unrar 6.21 real en la matriz): un RAR dañado o truncado se perdía ENTERO. Con 1
byte dañado, `unrar x` sale con rc=3 y deja 3 de 4 entradas idénticas; sobre un prefijo de
40 MB de 'Matrix.rar', `unrar lt` sale con rc=1 listando las 18 y `unrar x` recupera 17. El
código servía 0 en los dos casos, y un SIGTERM durante `unrar lt` dejaba el RAR en ERROR 6 h.

Sin binarios reales (en Windows no hay ninguno): `_Simulador` hace de `unrar`, `lsar` y
`unar` según `argv[0]`, con el formato de salida medido en la matriz (unrar 6.21).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import normalizacion.ingesta.precalificacion.contenedores as C
from normalizacion.core.config import PerillasFiltro

PERILLAS = PerillasFiltro()
MAGIA_RAR5 = b"Rar!\x1a\x07\x01\x00"

#: Cabecera y bloque de carpeta tal como los imprime `unrar lt` (medido, nombres cambiados).
_CABECERA_LT = (
    "\nUNRAR 6.21 freeware      Copyright (c) 1993-2023 Alexander Roshal\n\n"
    "Archive: /d/x.rar\nDetails: RAR 5\n"
)
_CARPETA_LT = (
    "        Name: DBs\n"
    "        Type: Directory\n"
    "       mtime: 2026-04-25 03:11:22,161806400\n"
    "  Attributes: ...D...\n"
    "     Host OS: Windows\n"
    " Compression: RAR 5.0(v50) -m0 -md=0K\n"
)


def _bloque_lt(ruta: str, tamano: int | str, empacado: int) -> str:
    return (
        f"        Name: {ruta}\n"
        "        Type: File\n"
        f"        Size: {tamano}\n"
        f" Packed size: {empacado}\n"
        "       Ratio: 4%\n"
        "       mtime: 2015-11-26 19:50:34,000000000\n"
        "  Attributes: ..A....\n"
        "       CRC32: 1CB992C0\n"
        "     Host OS: Windows\n"
        " Compression: RAR 5.0(v50) -m3 -md=32M\n"
    )


def _salida_lt(declarados: dict[str, int], empacados: dict[str, int] | None = None) -> str:
    empacados = empacados or {}
    bloques = [_CABECERA_LT, _CARPETA_LT]
    bloques += [
        _bloque_lt(r, t, empacados.get(r, max(t // 20, 1))) for r, t in declarados.items()
    ]
    return "\n".join(bloques)


def _json_lsar(declarados: dict[str, int]) -> str:
    return json.dumps(
        {
            "lsarContents": [
                {
                    "XADFileName": r,
                    "XADFileSize": t,
                    "XADCompressedSize": max(t // 20, 1),
                    "XADLastModificationDate": "2015-11-26 19:50:34 +0000",
                }
                for r, t in declarados.items()
            ]
        }
    )


class _Simulador:
    """Hace de `unrar` (lt / x), `lsar` y `unar`. Todos listan `declarados`; los que
    extraen escriben `arbol`, que puede NO coincidir con lo declarado (el fallo de unar, o
    la entrada que unrar borra por CRC). `al_extraer` corre al empezar cada extracción."""

    def __init__(
        self,
        declarados: dict[str, int],
        arbol: dict[str, bytes],
        *,
        rc_unrar_x: int = 0,
        rc_unrar_lt: int = 0,
        rc_unar: int = 0,
        empacados: dict[str, int] | None = None,
    ) -> None:
        self.declarados = declarados
        self.arbol = arbol
        self.rc_unrar_x = rc_unrar_x
        self.rc_unrar_lt = rc_unrar_lt
        self.rc_unar = rc_unar
        self.empacados = empacados
        self.al_extraer: Callable[[], None] | None = None
        self.llamadas: list[list[str]] = []
        self.opciones: list[dict[str, Any]] = []

    def veces(self, programa: str, orden: str | None = None) -> int:
        return sum(
            1 for a in self.llamadas if a[0] == programa and (orden is None or a[1] == orden)
        )

    def _escribir(self, destino: Path) -> None:
        if self.al_extraer is not None:
            self.al_extraer()
        for ruta, datos in self.arbol.items():
            (destino / ruta).parent.mkdir(parents=True, exist_ok=True)
            (destino / ruta).write_bytes(datos)

    def __call__(self, argv: list[str], **kw: Any) -> subprocess.CompletedProcess[bytes]:
        self.llamadas.append(list(argv))
        self.opciones.append(kw)
        programa, orden = argv[0], argv[1]
        if programa == "unrar" and orden == "lt":
            salida = _salida_lt(self.declarados, self.empacados).encode()
            error = b"Unexpected end of archive" if self.rc_unrar_lt else b""
            return subprocess.CompletedProcess(argv, self.rc_unrar_lt, salida, error)
        if programa == "unrar" and orden == "x":
            self._escribir(Path(argv[-1]))
            error = b"a.sql - checksum error" if self.rc_unrar_x else b""
            return subprocess.CompletedProcess(argv, self.rc_unrar_x, b"", error)
        if programa == "lsar":
            return subprocess.CompletedProcess(argv, 0, _json_lsar(self.declarados).encode(), b"")
        if programa == "unar":
            self._escribir(Path(argv[argv.index("-output-directory") + 1]))
            return subprocess.CompletedProcess(argv, self.rc_unar, b"", b"Failed!")
        raise AssertionError(f"binario inesperado: {argv}")


def _no_hay(nombre: str) -> Any:
    def _lanzar() -> str:
        raise FileNotFoundError(nombre)

    return _lanzar


def _binarios(
    monkeypatch: pytest.MonkeyPatch, sim: _Simulador, *, unrar: bool = True, lsar: bool = True
) -> None:
    # raising=False: con el código de antes `_unrar_bin` no existe, y el test tiene que
    # recorrer su camino real (y fallar por lo que sirve), no por un atributo ausente.
    monkeypatch.setattr(
        C, "_unrar_bin", (lambda: "unrar") if unrar else _no_hay("unrar"), raising=False
    )
    monkeypatch.setattr(C, "_lsar_bin", (lambda: "lsar") if lsar else _no_hay("lsar"))
    monkeypatch.setattr(C, "_unar_bin", lambda: "unar")
    monkeypatch.setattr(C, "_7zz_bin", _no_hay("7zz"))
    monkeypatch.setattr(C.subprocess, "run", sim)


@pytest.fixture
def cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    base = tmp_path / "cache"
    monkeypatch.setattr(C, "_CACHE_BASE", base)
    C._limpiar_cache_7z()
    yield base
    C._limpiar_cache_7z()


def _rar(tmp_path: Path, nombre: str = "m.rar") -> Path:
    ruta = tmp_path / nombre
    ruta.write_bytes(MAGIA_RAR5)
    return ruta


def _servir(ruta: Path, entrada: str) -> bytes:
    with C._paso_rar(io.BytesIO(), entrada, 1024, 1 << 30, ruta_fs=ruta) as f:
        return f.read()


# ------------------------------------------------------------------ listado


class TestListadoUnrar:
    def test_parsea_el_listado_tecnico_real(self) -> None:
        salida = "\n".join(
            [
                _CABECERA_LT,
                _CARPETA_LT,
                _bloque_lt("DBs/grande.sql", 21_424_711_026, 868_558_356),
                _bloque_lt("notas: v2.txt", 10_244, 945),  # ': ' dentro del nombre
                _bloque_lt("raro.bin", "?", 10),  # tamaño que unrar no conoce
            ]
        )
        entradas, ratios, desconocidas = C._parse_lt_unrar(salida)
        assert [e.ruta_interna for e in entradas] == ["DBs/grande.sql", "notas: v2.txt"]
        assert entradas[0].nombre == "grande.sql"
        assert entradas[0].tamano == 21_424_711_026  # el real, no el de lsar 1.10.1
        assert entradas[1].tamano == 10_244
        utc = datetime(2015, 11, 26, 19, 50, 34, tzinfo=UTC)
        assert entradas[0].mtime_ns == int(utc.timestamp()) * 1_000_000_000
        assert len(ratios) == len(entradas)
        assert ratios[0] == pytest.approx(24.67, abs=0.01)  # muy por debajo del guard de 300
        assert desconocidas == ["raro.bin"]

    def test_explorar_lista_con_unrar_y_la_entrada_grande_entra_con_su_tamano(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """La idx 809 de 'Matrix.rar' (21,4 GB de SQL) tenía 0 filas: lsar le dio 1,8e19 B y
        se apartó como bomba. Listada con unrar entra con su tamaño y el contenedor no queda
        `topado`. lsar ni se consulta."""
        sim = _Simulador(
            {"DBs/grande.sql": 21_424_711_026, "b.txt": 10},
            {},
            empacados={"DBs/grande.sql": 868_558_356},
        )
        _binarios(monkeypatch, sim)
        r = C.explorar(PERILLAS, _rar(tmp_path), "application/x-rar-compressed")
        assert r.ok and not r.topado
        assert {e.ruta_interna: e.tamano for e in r.entradas} == {
            "DBs/grande.sql": 21_424_711_026,
            "b.txt": 10,
        }
        assert sim.veces("lsar") == 0
        assert sim.llamadas[0] == ["unrar", "lt", "-p-", "--", str(tmp_path / "m.rar")]
        assert sim.opciones[0]["env"]["TZ"] == "UTC"  # el mtime entra en archivo_id

    def test_unrar_lt_de_algo_que_no_es_rar_no_pasa_por_contenedor_vacio(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Medido: `unrar lt` sobre bytes que no son un RAR sale con rc=0 y sin entradas. Darlo
        por un contenedor vacío lo marcaría explorado sin encolar nada."""
        sim = _Simulador({}, {})
        _binarios(monkeypatch, sim, lsar=False)
        r = C.explorar(PERILLAS, _rar(tmp_path), "application/x-rar-compressed")
        assert not r.ok and r.motivo == "contenedor_corrupto" and r.entradas == ()

    def test_rar_truncado_se_lista_con_unrar_y_queda_topado(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Prefijo de 40 MB de 'Matrix.rar' (medido): `unrar lt` sale con rc=1 y lista las 18
        entradas. Se daba por corrupto y se listaba con lsar, que es otra herramienta que la
        que extrae. Ahora se usa lo listado, y la copia parcial se ve en `topado`."""
        sim = _Simulador({"a.sql": 1000, "b.txt": 4}, {}, rc_unrar_lt=1)
        _binarios(monkeypatch, sim)
        r = C.explorar(PERILLAS, _rar(tmp_path), "application/x-rar-compressed")
        assert r.ok and r.topado
        assert {e.ruta_interna: e.tamano for e in r.entradas} == {"a.sql": 1000, "b.txt": 4}
        assert sim.veces("lsar") == 0

    @pytest.mark.parametrize("rc", [-15, -9, 5, 8, 12, 255])
    def test_un_fallo_del_nodo_al_listar_se_reintenta_sin_caer_a_lsar(
        self, rc: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SIGTERM al parar los workers (-15), OOM (-9), E/S (12)…: el archivo no ha dicho
        nada. Como `contenedor_corrupto` se listaba con lsar (sin la entrada de 21 GB) o se
        quedaba preservado sin explorar. Sube como `OSError` para que se reintente."""
        sim = _Simulador({"a.sql": 1000}, {}, rc_unrar_lt=rc)
        _binarios(monkeypatch, sim)
        with pytest.raises(OSError) as exc:
            C.explorar(PERILLAS, _rar(tmp_path), "application/x-rar-compressed")
        assert not isinstance(exc.value, C.ContenedorIlegible)
        assert sim.veces("lsar") == 0

    def test_lista_y_extrae_en_utc_y_utf8_sea_cual_sea_el_entorno(
        self, tmp_path: Path, cache: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nombre y mtime forman `archivo_id`. `unrar` imprime la hora local (6 h de
        diferencia, medido) y, en Linux, los nombres según el locale: sin UTF-8 cambia el
        nombre no ASCII de 'Matrix.rar' (medido), y el archivo extraído ya no casa con el
        listado."""
        monkeypatch.setenv("TZ", "America/Mexico_City")
        monkeypatch.setenv("LC_ALL", "C")
        sim = _Simulador({"año.txt": 4}, {"año.txt": b"hola"})
        _binarios(monkeypatch, sim)
        assert _servir(_rar(tmp_path), "año.txt") == b"hola"
        entornos = [kw["env"] for a, kw in zip(sim.llamadas, sim.opciones, strict=True)
                    if a[0] == "unrar"]
        assert len(entornos) == 2  # lt y x
        assert all(e["TZ"] == "UTC" and e["LC_ALL"] == "C.UTF-8" for e in entornos)


# ------------------------------------------------------------------ lsar y tamaños imposibles

_TAMANO_LSAR_ROTO = 18_446_744_073_659_426_162  # 2^64 - 50.125.454, medido en 'Matrix.rar'


class TestLsarTamanoImposible:
    _JSON = json.dumps(
        {
            "lsarContents": [
                {"XADFileName": "DBs", "XADIsDirectory": True},
                {"XADFileName": "DBs/grande.sql", "XADFileSize": _TAMANO_LSAR_ROTO,
                 "XADCompressedSize": 868_558_356},
                {"XADFileName": "DBs/a.sql", "XADFileSize": 1000, "XADCompressedSize": 100},
            ]
        }
    )

    def test_no_se_aisla_como_bomba_ni_se_pone_a_0(self) -> None:
        """Ni 0 (T0 la mataría como `kill_t0:vacio`) ni el valor falso (ratio 2e10 → bomba;
        suma 1,8e19 → guard de 1 TiB sobre el contenedor ENTERO): tamaño desconocido."""
        entradas, ratios, desconocidas = C._parse_json_lsar(self._JSON)
        assert [e.ruta_interna for e in entradas] == ["DBs/a.sql"]
        assert all(0 < e.tamano < 2**62 for e in entradas)
        assert desconocidas == ["DBs/grande.sql"]
        assert ratios == [10.0]  # alineado con las entradas, sin el ratio falso

    def test_sin_guard_de_ratio_el_tamano_falso_no_tumba_el_contenedor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Subir solo el umbral de ratio no basta: el 1,8e19 falso hacía saltar el guard de
        descomprimido y el RAR entero se iba a COLD. Queda fuera de la suma, y la entrada se
        ve en `topado` en vez de perderse en silencio."""
        monkeypatch.setattr(C, "_lsar_bin", lambda: "lsar")
        monkeypatch.setattr(
            C.subprocess,
            "run",
            lambda argv, **kw: subprocess.CompletedProcess(argv, 0, self._JSON.encode(), b""),
        )
        r = C._listar_con_lsar(
            PerillasFiltro(t3_ratio_compresion_max=1e30), "m.rar", time.monotonic(), "rar"
        )
        assert r.ok and r.topado
        assert r.tamano_ilegible == 1  # `topado` también lo pone el ratio: esto lo distingue
        assert [e.ruta_interna for e in r.entradas] == ["DBs/a.sql"]

    def test_la_fila_del_rar_dice_cuantas_entradas_no_se_pudieron_medir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`tamano_ilegible` solo sirve si llega a la fila: sin unrar, la entrada de 21,4 GB
        de Matrix se queda fuera y el RAR sale `topado`, igual que si se hubiera aislado una
        bomba por ratio."""
        from normalizacion.core import cola
        from normalizacion.core.config import PerillasWorker
        from normalizacion.core.modelo import Estado
        from normalizacion.ingesta.precalificacion import precalificador

        sim = _Simulador({"DBs/grande.sql": _TAMANO_LSAR_ROTO, "DBs/a.sql": 1000}, {})
        _binarios(monkeypatch, sim, unrar=False)
        ruta = _rar(tmp_path)
        fila = cola.FilaReclamada(
            archivo_id="rar",
            disco_id="d1",
            ruta=ruta.name,
            nombre=ruta.name,
            extension=".rar",
            tamano=6_206_347_578,
            mtime=datetime(2026, 9, 23, tzinfo=UTC),
            estado=Estado.PENDIENTE,
            intentos=0,
        )
        resultado, nuevas = precalificador._procesar_fila(
            PERILLAS, PerillasWorker(), str(tmp_path), fila
        )
        assert resultado.motivo == "contenedor_explorado"
        assert [n.ruta for n in nuevas] == [f"{ruta.name}!DBs/a.sql"]
        assert resultado.senales["contenedor_topado"] is True
        assert resultado.senales["entradas_tamano_ilegible"] == 1

    def test_un_archivo_vacio_ya_no_desalinea_el_aislamiento(self) -> None:
        """Un archivo de 0 B (sin tamaño comprimido) no aportaba ratio, la lista quedaba
        desalineada y `_aislar_ratio` no aislaba NADA en todo el RAR, bombas incluidas."""
        salida = json.dumps(
            {
                "lsarContents": [
                    {"XADFileName": "vacio.txt", "XADFileSize": 0, "XADCompressedSize": 0},
                    {"XADFileName": "bomba.bin", "XADFileSize": 10_000_000,
                     "XADCompressedSize": 1000},
                    {"XADFileName": "datos.csv", "XADFileSize": 5000, "XADCompressedSize": 1000},
                ]
            }
        )
        entradas, ratios, *_ = C._parse_json_lsar(salida)
        conservadas, aisladas = C._aislar_ratio(entradas, ratios, 300.0)
        assert aisladas == 1
        assert [e.ruta_interna for e in conservadas] == ["vacio.txt", "datos.csv"]


# ------------------------------------------------------------------ extracción


class TestExtraccionUnrar:
    def test_extrae_con_la_sintaxis_medida(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Probado con unrar 6.21 en la matriz: el destino acaba en separador (si no, es una
        máscara de nombres), `-p-` evita que un RAR cifrado espere contraseña en stdin."""
        sim = _Simulador({"a.txt": 4}, {"a.txt": b"hola"})
        _binarios(monkeypatch, sim)
        ruta, destino = _rar(tmp_path), tmp_path / "out"
        destino.mkdir()
        assert C._extraer_rar_con_unrar(ruta, destino) is True
        assert sim.llamadas == [
            ["unrar", "x", "-o+", "-y", "-idq", "-p-", "--", str(ruta), str(destino) + os.sep]
        ]
        assert sim.opciones[0]["stdin"] is subprocess.DEVNULL
        assert (destino / "a.txt").read_bytes() == b"hola"

    @pytest.mark.parametrize("rc", [1, 2, 3, 11])
    def test_rc_distinto_de_cero_con_archivos_es_parcial_no_fallo(
        self, rc: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """rc≠0 no tira lo extraído: unrar deja íntegras las entradas que no fallaron y BORRA
        la de CRC malo. Se devuelve PARCIAL para verificarlo contra el listado."""
        sim = _Simulador({"a.txt": 4, "b.txt": 2}, {"a.txt": b"hola"}, rc_unrar_x=rc)
        _binarios(monkeypatch, sim)
        destino = tmp_path / "out"
        destino.mkdir()
        assert C._extraer_rar_con_unrar(_rar(tmp_path), destino) is False
        assert (destino / "a.txt").read_bytes() == b"hola"

    @pytest.mark.parametrize("rc", [0, 1, 3, 11])
    def test_sin_ningun_archivo_extraido_es_fallo_permanente(
        self, rc: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Si no sale ni un archivo (contraseña, cabeceras rotas…) el RAR entero es ilegible.
        Las carpetas que unrar crea antes de fallar no cuentan."""
        sim = _Simulador({"DBs/a.txt": 4}, {}, rc_unrar_x=rc)
        _binarios(monkeypatch, sim)
        destino = tmp_path / "out"
        (destino / "DBs").mkdir(parents=True)
        with pytest.raises(C.ContenedorIlegible) as exc:
            C._extraer_rar_con_unrar(_rar(tmp_path), destino)
        assert not isinstance(exc.value, OSError)

    @pytest.mark.parametrize("rc", [5, 8, 9, 12, 255, -9])
    def test_rc_de_recursos_del_nodo_es_reintentable(
        self, rc: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Escribir (5), memoria (8), crear archivo (9), E/S al leer el RAR (12), interrumpido
        (255) o un SIGKILL del OOM (-9) hablan del nodo: transitorio, aunque haya extraído
        algo, porque un unrar muerto a medias no dice nada de lo que no llegó a escribir."""
        sim = _Simulador({"a.txt": 4, "b.txt": 2}, {"a.txt": b"hola"}, rc_unrar_x=rc)
        _binarios(monkeypatch, sim)
        destino = tmp_path / "out"
        destino.mkdir()
        with pytest.raises(OSError) as exc:
            C._extraer_rar_con_unrar(_rar(tmp_path), destino)
        assert not isinstance(exc.value, C.ContenedorIlegible)

    def test_rar_danado_sirve_las_enteras_y_solo_la_rota_es_error(
        self, tmp_path: Path, cache: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Medido por el revisor con unrar 6.21 (RAR5 con 1 byte dañado en los datos de
        a.txt): rc=3, a.txt BORRADA y las otras 3 idénticas. Se servían 0 de 4 (una como
        ilegible y 3 como «fallo reciente»). Ahora se sirven 3 y a.txt sale como
        `extraccion_incompleta`, con una sola extracción y sin marca de fallo."""
        declarados = {"a.txt": 5, "b.txt": 3, "sub/c.txt": 4, "vacio.txt": 0}
        arbol = {"b.txt": b"dos", "sub/c.txt": b"tres", "vacio.txt": b""}
        sim = _Simulador(declarados, arbol, rc_unrar_x=3)
        _binarios(monkeypatch, sim)
        ruta = _rar(tmp_path)
        with pytest.raises(C.ContenedorIlegible, match="extraccion_incompleta") as exc:
            _servir(ruta, "a.txt")
        assert type(exc.value).__name__ == "ExtraccionIncompleta"
        for entrada, datos in arbol.items():
            assert _servir(ruta, entrada) == datos
        assert sim.veces("unrar", "x") == 1
        assert not list(cache.glob(".*.fallo"))

    def test_rar_truncado_sirve_lo_recuperable(
        self, tmp_path: Path, cache: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Prefijo de 40 MB de 'Matrix.rar' (medido): `lt` rc=1 con 18 entradas, `x` rc=3 con
        17 enteras. Se servían 0 de 18. Se encola lo que lista unrar y la cortada no se
        sirve como buena."""
        declarados = {"a.sql": 1000, "b.txt": 4, "c.txt": 3}
        arbol = {"a.sql": b"x" * 640, "b.txt": b"hola", "c.txt": b"tre"}
        sim = _Simulador(declarados, arbol, rc_unrar_lt=1, rc_unrar_x=3)
        _binarios(monkeypatch, sim)
        ruta = _rar(tmp_path)
        r = C.explorar(PERILLAS, ruta, "application/x-rar-compressed")
        assert r.ok and r.topado and sim.veces("lsar") == 0
        servidas: dict[str, bytes] = {}
        for e in r.entradas:
            try:
                servidas[e.ruta_interna] = _servir(ruta, e.ruta_interna)
            except C.ExtraccionIncompleta:
                servidas[e.ruta_interna] = b"<extraccion_incompleta>"
        assert servidas == {"a.sql": b"<extraccion_incompleta>", "b.txt": b"hola", "c.txt": b"tre"}
        assert sim.veces("unrar", "x") == 1

    def test_un_fallo_del_nodo_al_listar_para_extraer_no_deja_marca(
        self, tmp_path: Path, cache: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reproducido por el revisor: `unrar lt` con rc=-15 (SIGTERM al parar los workers)
        daba ContenedorIlegible y marca de 6 h, y las entradas siguientes fallaban sin
        reintentar."""
        sim = _Simulador({"a.txt": 4}, {"a.txt": b"hola"}, rc_unrar_lt=-15)
        _binarios(monkeypatch, sim)
        ruta = _rar(tmp_path)
        with pytest.raises(OSError) as exc:
            _servir(ruta, "a.txt")
        assert not isinstance(exc.value, C.ContenedorIlegible)
        assert not list(cache.glob(".*.fallo"))
        sim.rc_unrar_lt = 0
        assert _servir(ruta, "a.txt") == b"hola"

    def test_un_rar_del_que_no_sale_nada_no_se_reextrae_por_cada_entrada(
        self, tmp_path: Path, cache: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sin memoria del fallo, cada una de las N entradas re-extrae el RAR entero para
        fallar igual: 1132 entradas por 310 s de `unrar t` en Matrix."""
        sim = _Simulador({"a.txt": 4, "b.txt": 2}, {}, rc_unrar_x=11, rc_unar=1)
        _binarios(monkeypatch, sim)
        ruta = _rar(tmp_path)
        for entrada in ("a.txt", "b.txt"):
            with pytest.raises(C.ContenedorIlegible):
                _servir(ruta, entrada)
        assert sim.veces("unrar", "x") == 1

    def test_la_memoria_del_fallo_caduca_sola(
        self, tmp_path: Path, cache: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Un reproceso tras arreglar la causa no puede depender de que alguien borre nada."""
        sim = _Simulador({"a.txt": 4}, {}, rc_unrar_x=3, rc_unar=1)
        _binarios(monkeypatch, sim)
        ruta = _rar(tmp_path)
        with pytest.raises(C.ContenedorIlegible):
            _servir(ruta, "a.txt")
        marcas = list(cache.glob(".*.fallo"))
        assert len(marcas) == 1
        viejo = time.time() - C._FALLO_TTL_S - 60
        os.utime(marcas[0], (viejo, viejo))
        sim.rc_unrar_x, sim.arbol = 0, {"a.txt": b"hola"}
        assert _servir(ruta, "a.txt") == b"hola"
        assert sim.veces("unrar", "x") == 2
        assert not marcas[0].exists()


class TestUnaExtraccionALaVez:
    def test_dos_hilos_con_el_mismo_rar_lo_extraen_una_sola_vez(
        self, tmp_path: Path, cache: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Con la clave nueva no hay caché de 'Matrix.rar', y cada worker que no veía el
        marcador extraía los 53,5 GB en su propio temporal: `_exigir_disco_libre` se lo
        aprobaba a todos porque nadie había escrito aún. Con un solo worker, además, el hilo
        del precalificador y el del worker compartían el temporal `.tmp.<pid>`."""
        sim = _Simulador({"a.txt": 4}, {"a.txt": b"hola"})
        _binarios(monkeypatch, sim)
        dentro, segunda, soltar = threading.Event(), threading.Event(), threading.Event()
        extracciones: list[int] = []

        def _al_extraer() -> None:
            extracciones.append(1)
            if len(extracciones) == 1:
                dentro.set()
                soltar.wait(10)
            else:
                segunda.set()

        sim.al_extraer = _al_extraer
        ruta = _rar(tmp_path)
        servidas: dict[str, bytes] = {}

        def _pedir(nombre: str) -> None:
            servidas[nombre] = _servir(ruta, "a.txt")

        hilos = [threading.Thread(target=_pedir, args=(n,)) for n in ("uno", "dos")]
        hilos[0].start()
        assert dentro.wait(10)
        hilos[1].start()
        segunda.wait(1.0)  # sin candado, el segundo hilo llega a extraer enseguida
        soltar.set()
        for h in hilos:
            h.join(10)
        assert servidas == {"uno": b"hola", "dos": b"hola"}
        assert len(extracciones) == 1


# ------------------------------------------------------------------ nunca servir incompleto


class TestNuncaServirIncompleto:
    @pytest.mark.parametrize(
        "extraido",
        [b"", b"INSERT 1"],  # a 0 B (45 en Matrix) y truncada (3 en Matrix)
        ids=["a_cero", "truncada"],
    )
    def test_entrada_que_no_mide_lo_declarado_es_error_permanente_y_no_x_empty(
        self, extraido: bytes, tmp_path: Path, cache: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lo que se servía como bueno: 0 B → libmagic `application/x-empty` → COLD con
        `fuera_de_lista_blanca`; truncada → indexada sin su cola. Ahora es un error
        PERMANENTE con motivo `extraccion_incompleta`, a la vista, y el resto del RAR se
        sirve igual."""
        sim = _Simulador(
            {"DBs/a.sql": 1000, "b.txt": 4},
            {"DBs/a.sql": extraido, "b.txt": b"hola"},
            rc_unar=1,
        )
        _binarios(monkeypatch, sim)
        ruta = _rar(tmp_path)
        with pytest.raises(C.ContenedorIlegible, match="extraccion_incompleta") as exc:
            C.abrir_entrada(
                tmp_path, [ruta.name, "DBs/a.sql"], umbral_memoria=1024, limite_bytes=1 << 30
            )
        assert not isinstance(exc.value, OSError)  # permanente: no va a reintentos
        assert type(exc.value).__name__ == "ExtraccionIncompleta"
        assert _servir(ruta, "b.txt") == b"hola"

    def test_sin_unrar_el_parcial_de_unar_se_verifica_contra_lsar(
        self, tmp_path: Path, cache: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El respaldo sigue aceptando el árbol parcial de unar —no se tira lo bueno— pero
        cada entrada se compara con lo que declara lsar."""
        sim = _Simulador(
            {"DBs/a.sql": 1000, "b.txt": 4}, {"DBs/a.sql": b"", "b.txt": b"hola"}, rc_unar=1
        )
        _binarios(monkeypatch, sim, unrar=False)
        ruta = _rar(tmp_path)
        assert _servir(ruta, "b.txt") == b"hola"
        with pytest.raises(C.ContenedorIlegible, match="extraccion_incompleta"):
            _servir(ruta, "DBs/a.sql")
        assert sim.veces("unar") == 1

    def test_sin_listado_un_parcial_de_unar_no_se_da_por_bueno(
        self, tmp_path: Path, cache: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sim = _Simulador({"DBs/a.sql": 1000}, {"DBs/a.sql": b"", "b.txt": b"hola"}, rc_unar=1)
        _binarios(monkeypatch, sim, unrar=False, lsar=False)
        with pytest.raises(C.ContenedorIlegible):
            _servir(_rar(tmp_path), "b.txt")

    def test_entrada_ausente_de_un_arbol_completo_es_permanente(
        self, tmp_path: Path, cache: Path
    ) -> None:
        """Faltar en un árbol COMPLETO no cambia reintentando: antes era un `OSError` que
        iba a reintentos hasta agotarlos. Solo si el LRU está desalojando el dir (sin
        marcador) es reintentable."""
        import py7zr

        ruta = tmp_path / "c.7z"
        with py7zr.SevenZipFile(ruta, "w") as sz:
            sz.writestr(b"uno", "a.txt")
            sz.writestr(b"dos", "b.txt")
        dir_ex = C._dir_7z_extraido(ruta)
        (dir_ex / "b.txt").unlink()  # lo que dejaba unar: una entrada listada que no está
        with pytest.raises(C.ContenedorIlegible, match="extraccion_incompleta"):
            C.abrir_entrada(tmp_path, ["c.7z", "b.txt"], umbral_memoria=1024, limite_bytes=1 << 20)
        (dir_ex / C._MARCADOR).unlink()
        with pytest.raises(OSError):
            C._servir_desde_arbol(dir_ex, "b.txt", 1 << 20, "7z")


# ------------------------------------------------------------------ caché y disco


class TestCacheYDisco:
    def test_la_cache_vieja_de_unar_no_se_reusa_y_se_retira(
        self, tmp_path: Path, cache: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """La caché de 'Matrix.rar' (a7164269…) la hizo unar y está marcada como COMPLETA con
        49 entradas malas. Con la clave de siempre se reusaría para siempre."""
        ruta = _rar(tmp_path)
        st = ruta.stat()
        material = f"{ruta.resolve()}\0{st.st_size}\0{st.st_mtime_ns}".encode()
        clave_vieja = hashlib.sha256(material).hexdigest()
        assert C._clave_persistente(ruta) == clave_vieja  # las cachés de 7z siguen valiendo
        vieja = cache / clave_vieja
        (vieja / "DBs").mkdir(parents=True)
        (vieja / "DBs" / "a.sql").write_bytes(b"")  # lo que dejó unar
        (vieja / C._ARCHIVO_TAM).write_text("0")
        (vieja / C._MARCADOR).write_bytes(b"")

        sim = _Simulador({"DBs/a.sql": 8}, {"DBs/a.sql": b"INSERT 1"}, rc_unar=1)
        _binarios(monkeypatch, sim)
        assert _servir(ruta, "DBs/a.sql") == b"INSERT 1"
        assert sim.veces("unrar", "x") == 1
        assert not vieja.exists()  # 32 GB muertos en Matrix si se quedaba

    def test_reserva_por_lo_declarado_y_no_extrae_si_no_cabe(
        self, tmp_path: Path, cache: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Se reservaba tamaño x4 (24,8 GB para Matrix frente a 53,5 GB reales) y no se
        miraba el disco: un RAR que no cabe lo llenaba. Ahora falla ANTES de escribir, con
        motivo, y como transitorio (es el nodo, no el archivo)."""
        gib = 1024**3
        sim = _Simulador({"DBs/a.sql": 50 * gib}, {}, rc_unar=1)
        _binarios(monkeypatch, sim)
        reservas: list[int] = []
        monkeypatch.setattr(C, "_evictar_si_hace_falta", lambda reservar: reservas.append(reservar))

        class _Uso:
            total = 100 * gib
            used = 60 * gib
            free = 40 * gib

        monkeypatch.setattr(C.shutil, "disk_usage", lambda _p: _Uso())
        with pytest.raises(OSError, match="disco_insuficiente") as exc:
            _servir(_rar(tmp_path), "DBs/a.sql")
        assert not isinstance(exc.value, C.ContenedorIlegible)
        assert reservas == [50 * gib]
        assert sim.veces("unrar", "x") == 0 and sim.veces("unar") == 0


# ------------------------------------------------------------------ de punta a punta


class _ConexionFalsa:
    """Lo único de la BD que usa `precalificar_pendientes` fuera de la cola simulada: la
    consulta del tipo de los padres de los lotes de texto. Aquí ningún padre tiene tipo."""

    def __enter__(self) -> _ConexionFalsa:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def commit(self) -> None:
        return None

    def execute(self, _sql: str, _params: Any = None) -> Any:
        return type("_Cursor", (), {"fetchall": lambda _self: []})()


def _fila(archivo_id: str, cadena: list[str], tamano: int) -> Any:
    from normalizacion.core import cola
    from normalizacion.core.modelo import Estado

    nombre = cadena[-1].rsplit("/", 1)[-1]
    return cola.FilaReclamada(
        archivo_id=archivo_id,
        disco_id="d1",
        ruta="!".join(cadena),
        nombre=nombre,
        extension=Path(nombre).suffix or None,
        tamano=tamano,
        mtime=datetime(2015, 11, 26, tzinfo=UTC),
        estado=Estado.PENDIENTE,
        intentos=0,
        origen_contenedor={
            "cadena": cadena,
            "profundidad": len(cadena) - 1,
            "contenedor_archivo_id": "padre",
            **({"hoja": True} if cadena[-1].startswith("texto/") else {}),
        },
    )


_AVISO = b"Aviso de privacidad para clientes, 2015.\n"


class TestLaColaRecibeLoQueLanzaLaExtraccion:
    """`contenedores` distingue lo permanente (`ExtraccionIncompleta`) de lo transitorio
    (`FalloDelNodo`), pero quien decide es la rama de `precalificar_pendientes` en la que
    cae. Una entrada incompleta en reintentos volvería a leer el mismo árbol hasta agotarlos,
    y un SIGTERM en ERROR dejaría fuera de la cola un RAR sano."""

    def _precalificar(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filas: list[Any]
    ) -> tuple[Any, dict[str, str], dict[str, str], dict[str, Any]]:
        import psycopg

        from normalizacion.core import cola
        from normalizacion.core.config import Config
        from normalizacion.ingesta.precalificacion import precalificador

        errores: dict[str, str] = {}
        transitorios: dict[str, str] = {}
        guardadas: dict[str, Any] = {}
        lotes = iter([filas, []])
        monkeypatch.setattr(psycopg, "connect", lambda _dsn: _ConexionFalsa())
        monkeypatch.setattr(cola, "montajes", lambda _c: {"d1": str(tmp_path)})
        monkeypatch.setattr(cola, "sistema_pausado", lambda _c: False)
        monkeypatch.setattr(cola, "claim", lambda _c, **_kw: next(lotes))
        monkeypatch.setattr(cola, "insertar_pendientes", lambda _c, nuevas: len(nuevas))
        monkeypatch.setattr(
            cola, "guardar_precalificacion", lambda _c, aid, **kw: guardadas.__setitem__(aid, kw)
        )
        monkeypatch.setattr(
            cola,
            "marcar_error",
            lambda _c, aid, _de, motivo, **_k: errores.__setitem__(aid, motivo),
        )

        def _transitorio(_c: Any, aid: str, **kw: Any) -> bool:
            transitorios[aid] = kw["motivo"]
            return True

        monkeypatch.setattr(cola, "fallo_transitorio", _transitorio)
        resumen = precalificador.precalificar_pendientes(Config(_env_file=None))
        return resumen, errores, transitorios, guardadas

    def test_la_entrada_incompleta_y_sus_lotes_van_a_error_una_sola_vez(
        self, tmp_path: Path, cache: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Las 41 de 'Matrix.rar' que unar dejó en 0 B acabaron en COLD como
        `application/x-empty`, y las 3 truncadas se indexaron sin su cola. La entrada y los
        lotes de texto que cuelgan de ella van a ERROR con el motivo a la vista; la entrada
        sana del mismo RAR se precalifica, con UNA sola extracción."""
        sim = _Simulador(
            {"DBs/a.sql": 200_000, "b.txt": len(_AVISO)},
            {"DBs/a.sql": b"INSERT INTO t VALUES (1);\n" * 2500, "b.txt": _AVISO},
            rc_unrar_x=3,
        )
        _binarios(monkeypatch, sim)
        ruta = _rar(tmp_path)
        filas = [
            _fila("a", [ruta.name, "DBs/a.sql"], 200_000),
            _fila("a-lote", [ruta.name, "DBs/a.sql", "texto/0-65536"], 65_536),
            _fila("b", [ruta.name, "b.txt"], len(_AVISO)),
        ]
        resumen, errores, transitorios, guardadas = self._precalificar(tmp_path, monkeypatch, filas)
        assert transitorios == {}
        assert set(errores) == {"a", "a-lote"}
        assert all(m.startswith("ilegible: extraccion_incompleta") for m in errores.values())
        assert set(guardadas) == {"b"}
        assert resumen.errores == 2 and resumen.transitorios == 0
        assert sim.veces("unrar", "x") == 1

    def test_un_fallo_del_nodo_va_a_reintento_y_no_a_error(
        self, tmp_path: Path, cache: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`unrar lt` con rc=-15 (SIGTERM al parar los workers) no dice nada del RAR."""
        sim = _Simulador({"b.txt": len(_AVISO)}, {"b.txt": _AVISO}, rc_unrar_lt=-15)
        _binarios(monkeypatch, sim)
        ruta = _rar(tmp_path)
        resumen, errores, transitorios, guardadas = self._precalificar(
            tmp_path, monkeypatch, [_fila("b", [ruta.name, "b.txt"], len(_AVISO))]
        )
        assert errores == {} and guardadas == {}
        assert transitorios["b"].startswith("io_ilegible: unrar lt salió con -15")
        assert resumen.transitorios == 1
        assert not list(cache.glob(".*.fallo"))
