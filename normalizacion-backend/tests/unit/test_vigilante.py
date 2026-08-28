"""Vigilante de carpeta (perfil `online`): huella, disparo, round-robin, reclamación.

Sin BD ni FS de verdad para el pipeline: se inyectan `iniciar_corrida`/
`ejecutar_corrida` y `evaluar_puerta`. Lo que se prueba es la LÓGICA del vigilante
—cuándo dispara, a quién, y cuándo NO borra el origen—, que es donde está el riesgo.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from normalizacion.core.config import Config, PerillasDespliegue
from normalizacion.ingesta.vigilante import Firma, Vigilante, firmar, fuentes, lista_para_procesar


def _cfg_online() -> Config:
    return Config(
        _env_file=None,  # type: ignore[call-arg]
        despliegue=PerillasDespliegue(perfil="online", nodo_id="vps-01"),
    )


def _archivo(raiz: Path, rel: str, contenido: bytes = b"x", mtime_ns: int | None = None) -> Path:
    p = raiz / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(contenido)
    if mtime_ns is not None:
        os.utime(p, ns=(mtime_ns, mtime_ns))
    return p


class TestFirma:
    def test_cuenta_archivos_bytes_y_mtime(self, tmp_path: Path) -> None:
        _archivo(tmp_path, "a.txt", b"hola", mtime_ns=1000)
        _archivo(tmp_path, "sub/b.txt", b"xy", mtime_ns=5000)
        f = firmar(tmp_path)
        assert f.n_archivos == 2
        assert f.bytes_totales == 6
        assert f.mtime_max_ns == 5000

    def test_ignora_la_sentinela(self, tmp_path: Path) -> None:
        _archivo(tmp_path, "dato.csv", b"abc")
        _archivo(tmp_path, ".listo", b"")
        assert firmar(tmp_path, ignorar=".listo").n_archivos == 1
        assert firmar(tmp_path).n_archivos == 2

    def test_carpeta_vacia_es_vacia(self, tmp_path: Path) -> None:
        assert firmar(tmp_path).vacia


class TestListaParaProcesar:
    def test_vacia_nunca(self) -> None:
        assert not lista_para_procesar(
            Firma(0, 0, 0), ahora_ns=10**12, quiescencia_ns=0,
            sentinela_presente=True, sentinela_requerida=True,
        )

    def test_sentinela_manda_cuando_se_exige(self) -> None:
        f = Firma(1, 10, 10**18)  # mtime recentísimo: sin sentinela NO pasaría quiescencia
        assert lista_para_procesar(
            f, ahora_ns=10**18, quiescencia_ns=10**11,
            sentinela_presente=True, sentinela_requerida=True,
        )
        assert not lista_para_procesar(
            f, ahora_ns=10**18, quiescencia_ns=10**11,
            sentinela_presente=False, sentinela_requerida=True,
        )

    def test_quiescencia_cuando_no_hay_sentinela(self) -> None:
        f = Firma(1, 10, 1000)
        # ahora muy posterior al último write → quieto → sí
        assert lista_para_procesar(
            f, ahora_ns=1000 + 10**12, quiescencia_ns=10**11,
            sentinela_presente=False, sentinela_requerida=False,
        )
        # ahora justo tras el write → aún escribiéndose → no
        assert not lista_para_procesar(
            f, ahora_ns=1000 + 5, quiescencia_ns=10**11,
            sentinela_presente=False, sentinela_requerida=False,
        )


class TestFuentes:
    def test_solo_subcarpetas_ordenadas(self, tmp_path: Path) -> None:
        (tmp_path / "beta").mkdir()
        (tmp_path / "alfa").mkdir()
        _archivo(tmp_path, "suelto.txt")  # archivo suelto en la raíz: se ignora
        assert [p.name for p in fuentes(tmp_path)] == ["alfa", "beta"]


class _PipelineFalso:
    """Sustituye iniciar_corrida/ejecutar_corrida sin tocar la BD."""

    def __init__(self) -> None:
        self.iniciadas: list[str] = []
        self.ejecutadas: list[str] = []
        self.en_curso = False  # simula 'ya hay una corrida' → RuntimeError
        self.falla = False
        self._id = 0

    def iniciar(self, config, fuente, disco_id=None):
        if self.en_curso:
            raise RuntimeError("ya hay una corrida en curso (id 1)")
        self._id += 1
        self.iniciadas.append(disco_id)
        return self._id, disco_id

    def ejecutar(self, config, corrida_id, fuente, disco_id, workers=None):
        if self.falla:
            raise RuntimeError("boom")
        self.ejecutadas.append(disco_id)
        return []


@pytest.fixture
def falso(monkeypatch: pytest.MonkeyPatch) -> _PipelineFalso:
    from normalizacion.ingesta import pipeline

    f = _PipelineFalso()
    monkeypatch.setattr(pipeline, "iniciar_corrida", f.iniciar)
    monkeypatch.setattr(pipeline, "ejecutar_corrida", f.ejecutar)
    return f


def _vig_con_reloj(raiz: Path, ahora_ns: int, **kw) -> Vigilante:
    return Vigilante(_cfg_online(), raiz, reloj_ns=lambda: ahora_ns, quiescencia_s=0.0, **kw)


class TestUnCiclo:
    def test_procesa_una_fuente_lista(self, tmp_path: Path, falso: _PipelineFalso) -> None:
        _archivo(tmp_path, "alfa/d.csv", b"1", mtime_ns=1000)
        v = _vig_con_reloj(tmp_path, ahora_ns=10**12)
        assert v.un_ciclo() == "alfa"
        assert falso.ejecutadas == ["alfa"]

    def test_round_robin_reparte_entre_fuentes(self, tmp_path: Path, falso: _PipelineFalso) -> None:
        _archivo(tmp_path, "alfa/d.csv", b"1", mtime_ns=1000)
        _archivo(tmp_path, "beta/d.csv", b"1", mtime_ns=1000)
        v = _vig_con_reloj(tmp_path, ahora_ns=10**12)
        assert v.un_ciclo() == "alfa"  # primera
        assert v.un_ciclo() == "beta"  # el cursor avanzó: no repite alfa
        # alfa ya no cambia → nada nuevo; beta tampoco → ciclo vacío
        assert v.un_ciclo() is None

    def test_no_reprocesa_sin_cambios(self, tmp_path: Path, falso: _PipelineFalso) -> None:
        _archivo(tmp_path, "alfa/d.csv", b"1", mtime_ns=1000)
        v = _vig_con_reloj(tmp_path, ahora_ns=10**12)
        assert v.un_ciclo() == "alfa"
        assert v.un_ciclo() is None
        assert falso.ejecutadas == ["alfa"]  # una sola vez

    def test_contenido_nuevo_reactiva(self, tmp_path: Path, falso: _PipelineFalso) -> None:
        _archivo(tmp_path, "alfa/d.csv", b"1", mtime_ns=1000)
        v = _vig_con_reloj(tmp_path, ahora_ns=10**12)
        assert v.un_ciclo() == "alfa"
        _archivo(tmp_path, "alfa/e.csv", b"2", mtime_ns=2000)  # llega otro archivo
        assert v.un_ciclo() == "alfa"  # firma cambió → reprocesa
        assert falso.ejecutadas == ["alfa", "alfa"]

    def test_respeta_quiescencia(self, tmp_path: Path, falso: _PipelineFalso) -> None:
        _archivo(tmp_path, "alfa/d.csv", b"1", mtime_ns=10**12)  # escrito 'ahora mismo'
        v = Vigilante(_cfg_online(), tmp_path, reloj_ns=lambda: 10**12 + 1, quiescencia_s=60.0)
        assert v.un_ciclo() is None  # aún caliente
        assert falso.ejecutadas == []

    def test_corrida_ajena_bloquea_y_no_avanza(self, tmp_path: Path, falso: _PipelineFalso) -> None:
        _archivo(tmp_path, "alfa/d.csv", b"1", mtime_ns=1000)
        falso.en_curso = True
        v = _vig_con_reloj(tmp_path, ahora_ns=10**12)
        assert v.un_ciclo() is None  # bloqueada → duerme
        assert falso.iniciadas == [] and falso.ejecutadas == []

    def test_fallo_pone_cooldown(self, tmp_path: Path, falso: _PipelineFalso) -> None:
        _archivo(tmp_path, "alfa/d.csv", b"1", mtime_ns=1000)
        falso.falla = True
        v = Vigilante(
            _cfg_online(), tmp_path, reloj_ns=lambda: 10**12,
            quiescencia_s=0.0, cooldown_fallo_s=300.0,
        )
        assert v.un_ciclo() == "alfa"  # se intentó
        assert v.un_ciclo() is None  # en cooldown, no reintenta ya


class TestReclamacion:
    """La parte destructiva: SOLO borra con puerta verde Y firma estable."""

    def _puerta(self, monkeypatch: pytest.MonkeyPatch, *, seguro: bool) -> None:
        from normalizacion.ingesta.workers import verificador

        class _E:
            seguro_para_desechar = seguro
            motivo_bloqueo = None if seguro else "datos_sin_poner_a_salvo"
            pendientes = 0 if seguro else 3
            errores = 0

        monkeypatch.setattr(verificador, "evaluar_puerta", lambda c, d: _E())

    def test_puerta_roja_no_borra(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from normalizacion.ingesta.reclamacion import reclamar_origen

        _archivo(tmp_path, "d.csv", b"1")
        self._puerta(monkeypatch, seguro=False)
        f = firmar(tmp_path)
        assert reclamar_origen(_cfg_online(), "vps-01:alfa", tmp_path, f) is False
        assert (tmp_path / "d.csv").exists()  # intacto

    def test_firma_cambiada_no_borra(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from normalizacion.ingesta.reclamacion import reclamar_origen

        _archivo(tmp_path, "d.csv", b"1")
        self._puerta(monkeypatch, seguro=True)
        vieja = firmar(tmp_path)
        _archivo(tmp_path, "nuevo.csv", b"22")  # llegó algo tras catalogar
        assert reclamar_origen(_cfg_online(), "vps-01:alfa", tmp_path, vieja) is False
        assert (tmp_path / "d.csv").exists() and (tmp_path / "nuevo.csv").exists()

    def test_verde_y_estable_vacia_pero_conserva_carpeta(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from normalizacion.ingesta.reclamacion import reclamar_origen

        _archivo(tmp_path, "d.csv", b"1")
        _archivo(tmp_path, "sub/e.csv", b"2")
        self._puerta(monkeypatch, seguro=True)
        f = firmar(tmp_path)
        assert reclamar_origen(_cfg_online(), "vps-01:alfa", tmp_path, f) is True
        assert tmp_path.is_dir()  # la carpeta-fuente se conserva
        assert list(tmp_path.iterdir()) == []  # pero vacía

    def test_no_maestro_rechaza(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from normalizacion.ingesta.reclamacion import reclamar_origen

        _archivo(tmp_path, "d.csv", b"1")
        self._puerta(monkeypatch, seguro=True)
        cfg = Config(
            _env_file=None,  # type: ignore[call-arg]
            despliegue=PerillasDespliegue(perfil="hibrido-servicio", nodo_id="vps-01"),
        )
        assert reclamar_origen(cfg, "vps-01:alfa", tmp_path, firmar(tmp_path)) is False
        assert (tmp_path / "d.csv").exists()
