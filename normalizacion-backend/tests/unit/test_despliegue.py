"""⚙ K16 — topología: capacidades por perfil e identidad de disco por nodo.

El test más importante de este archivo es `TestLocalNoCambiaNada`: el perfil `local`
debe comportarse EXACTAMENTE como el sistema de siempre. Si una fase del plan
híbrido lo rompe, el build falla aquí antes de llegar a producción.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from normalizacion.core import despliegue
from normalizacion.core.config import Config, PerillasDespliegue


def _cfg(**kwargs: object) -> Config:
    """Config con el perfil LOCAL fijado EXPLÍCITAMENTE.

    `Config(_env_file=None)` desactiva el archivo .env pero SIGUE leyendo las
    variables de entorno. Como la CI corre esta suite en los tres perfiles
    (NORM_DESPLIEGUE__PERFIL en el entorno), heredarlo haría que los tests que
    dicen comprobar `local` comprobaran otra cosa — y pasarían o fallarían según
    quién los lance. `PerillasDespliegue()` es un BaseModel normal, no lee entorno,
    así que fija local/local pase lo que pase.

    El default del CÓDIGO se verifica aparte, en `TestDefaults`."""
    kwargs.setdefault("despliegue", PerillasDespliegue())
    return Config(_env_file=None, **kwargs)  # type: ignore[arg-type]


def _hibrido(perfil: str, nodo: str) -> Config:
    return _cfg(despliegue=PerillasDespliegue(perfil=perfil, nodo_id=nodo))  # type: ignore[arg-type]


class TestDefaults:
    def test_el_default_es_local(self) -> None:
        """Nadie que no lo pida explícitamente entra en modo híbrido.

        Se comprueba sobre `PerillasDespliegue()` y no sobre `Config()`: el default
        que importa es el del CÓDIGO. Config lee el entorno, y la CI lanza esta
        suite con NORM_DESPLIEGUE__PERFIL puesto — mirarlo ahí mediría el entorno
        del runner, no el default."""
        d = PerillasDespliegue()
        assert d.perfil == "local"
        assert d.nodo_id == "local"
        assert d.es_local()

    def test_perfil_por_entorno(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """La topología se fija por entorno (NO por `config_overrides`: cambiarla con
        procesos vivos no es coherente).

        Construye `Config` directamente en vez de usar `_cfg()`: ese helper fija el
        perfil local a propósito, y aquí lo que se comprueba es justo lo contrario
        —que el entorno mande—."""
        monkeypatch.setenv("NORM_DESPLIEGUE__PERFIL", "hibrido-servicio")
        monkeypatch.setenv("NORM_DESPLIEGUE__NODO_ID", "vps-01")
        d = Config(_env_file=None).despliegue
        assert d.perfil == "hibrido-servicio"
        assert d.nodo_id == "vps-01"
        assert not d.es_local()

    def test_perfil_desconocido_no_valida(self) -> None:
        with pytest.raises(ValidationError):
            PerillasDespliegue(perfil="lo-que-sea")  # type: ignore[arg-type]

    @pytest.mark.parametrize("malo", ["", "VPS-01", "vps 01", "-vps", "vps_01", "x" * 33])
    def test_nodo_id_invalido_no_valida(self, malo: str) -> None:
        """El nodo_id entra en identificadores PERMANENTES (disco_id, índice): se
        valida en el borde, no cuando ya contaminó la base."""
        with pytest.raises(ValidationError):
            PerillasDespliegue(nodo_id=malo)


class TestCapacidades:
    def test_local_lo_enciende_todo_menos_publico(self) -> None:
        t = despliegue.de_config(_cfg())
        assert t.corre_ingesta
        assert t.corre_entidades
        assert t.es_archivo_maestro
        assert t.destino_eligible
        assert not t.sirve_publico  # una sola máquina no se expone a internet

    def test_mac_ingiere_y_es_maestro_pero_no_resuelve(self) -> None:
        t = despliegue.de_config(_hibrido("hibrido-ingesta", "mac-01"))
        assert t.corre_ingesta
        assert t.es_archivo_maestro
        assert t.destino_eligible  # tiene los discos externos donde elegir destino
        assert not t.corre_entidades
        assert not t.sirve_publico

    def test_vps_resuelve_y_sirve_pero_no_es_maestro(self) -> None:
        t = despliegue.de_config(_hibrido("hibrido-servicio", "vps-01"))
        assert t.corre_ingesta  # fuentes de red (dumps, padrones, descargas)
        assert t.corre_entidades
        assert t.sirve_publico
        assert not t.es_archivo_maestro
        assert not t.destino_eligible  # replica blobs: necesita un almacén único

    def test_un_solo_resolvedor_de_entidades(self) -> None:
        """Dos nodos resolviendo producirían conjuntos incompletos y distintos que
        se pisarían en el AEB (el cable manda modo_merge='reemplazar')."""
        perfiles = ["hibrido-ingesta", "hibrido-servicio"]
        resolvedores = [
            p for p in perfiles if despliegue.derivar(PerillasDespliegue(perfil=p)).corre_entidades  # type: ignore[arg-type]
        ]
        assert resolvedores == ["hibrido-servicio"]

    def test_un_solo_archivo_maestro(self) -> None:
        perfiles = ["hibrido-ingesta", "hibrido-servicio"]
        maestros = [
            p
            for p in perfiles
            if despliegue.derivar(PerillasDespliegue(perfil=p)).es_archivo_maestro  # type: ignore[arg-type]
        ]
        assert maestros == ["hibrido-ingesta"]

    def test_quien_no_es_maestro_no_elige_destino(self) -> None:
        """Invariante estructural: el nodo que replica blobs hacia fuera necesita un
        almacén único y direccionable, así que no puede tener el selector."""
        for perfil in ("local", "hibrido-ingesta", "hibrido-servicio"):
            t = despliegue.derivar(PerillasDespliegue(perfil=perfil))  # type: ignore[arg-type]
            if not t.es_archivo_maestro:
                assert not t.destino_eligible, perfil


class TestIdentidadDeDisco:
    def test_local_no_prefija(self) -> None:
        c = _cfg()
        assert despliegue.prefijo_disco(c) == ""
        assert despliegue.normalizar_disco_id(c, "RESPALDO") == "RESPALDO"

    def test_hibrido_prefija_discos_nuevos(self) -> None:
        c = _hibrido("hibrido-ingesta", "mac-01")
        assert despliegue.normalizar_disco_id(c, "RESPALDO") == "mac-01:RESPALDO"

    def test_prefijar_es_idempotente(self) -> None:
        """Re-catalogar el mismo disco no lo re-prefija (sería un disco distinto,
        con TODOS sus archivo_id nuevos → duplicado entero en cola e índice)."""
        c = _hibrido("hibrido-ingesta", "mac-01")
        una_vez = despliegue.normalizar_disco_id(c, "RESPALDO")
        assert despliegue.normalizar_disco_id(c, una_vez) == una_vez

    def test_nodos_distintos_no_colisionan(self) -> None:
        mac = despliegue.normalizar_disco_id(_hibrido("hibrido-ingesta", "mac-01"), "DATOS")
        vps = despliegue.normalizar_disco_id(_hibrido("hibrido-servicio", "vps-01"), "DATOS")
        assert mac != vps

    def test_disco_id_vacio_no_pasa(self) -> None:
        with pytest.raises(ValueError):
            despliegue.normalizar_disco_id(_cfg(), "   ")

    def test_solo_el_hibrido_exige_disco_id_explicito(self) -> None:
        assert not despliegue.exige_disco_id_explicito(_cfg())
        assert despliegue.exige_disco_id_explicito(_hibrido("hibrido-ingesta", "mac-01"))


class TestResolverDiscoIdRespetaLoExistente:
    """La trampa de migración: un disco YA catalogado nunca cambia de id.

    `archivo_id = sha256(f"{disco_id}:{ruta_rel}|…")`, e `insertar_pendientes` hace
    `ON CONFLICT (archivo_id) DO NOTHING`. Si al re-catalogar cambiara el disco_id,
    las filas viejas quedarían y las nuevas se insertarían: el disco DUPLICADO
    entero en la cola y en el índice, con la puerta contando el doble."""

    def test_disco_legado_sin_prefijo_conserva_su_id(self) -> None:
        c = _hibrido("hibrido-ingesta", "mac-01")
        resuelto = despliegue.resolver_disco_id(
            c, "RESPALDO", ya_existe=lambda d: d == "RESPALDO"
        )
        assert resuelto == "RESPALDO", "un disco ya catalogado no se re-prefija jamás"

    def test_disco_nuevo_estrena_namespace(self) -> None:
        c = _hibrido("hibrido-ingesta", "mac-01")
        resuelto = despliegue.resolver_disco_id(c, "RESPALDO", ya_existe=lambda d: False)
        assert resuelto == "mac-01:RESPALDO"

    def test_disco_ya_prefijado_es_estable(self) -> None:
        c = _hibrido("hibrido-ingesta", "mac-01")
        for existe in (True, False):
            assert (
                despliegue.resolver_disco_id(
                    c, "mac-01:RESPALDO", ya_existe=lambda d, e=existe: e  # type: ignore[misc]
                )
                == "mac-01:RESPALDO"
            )

    def test_en_local_es_siempre_la_identidad(self) -> None:
        c = _cfg()
        for existe in (True, False):
            assert (
                despliegue.resolver_disco_id(c, "RESPALDO", ya_existe=lambda d, e=existe: e)  # type: ignore[misc]
                == "RESPALDO"
            )


class TestIndiceDeEscritura:
    """El fallo más caro y más silencioso del híbrido: si los dos nodos escribieran
    en el mismo índice, restaurar el snapshot del otro borraría el propio. No
    revienta — sólo faltan documentos."""

    def test_local_conserva_el_nombre_historico(self) -> None:
        from normalizacion.core.indexador.opensearch import indice_escritura

        assert indice_escritura(_cfg()) == "archivos-000001"

    def test_cada_nodo_escribe_en_el_suyo(self) -> None:
        from normalizacion.core.indexador.opensearch import indice_escritura

        mac = indice_escritura(_hibrido("hibrido-ingesta", "mac-01"))
        vps = indice_escritura(_hibrido("hibrido-servicio", "vps-01"))
        assert mac == "archivos-mac-01-000001"
        assert vps == "archivos-vps-01-000001"
        assert mac != vps

    def test_los_indices_por_nodo_caen_bajo_el_patron_de_la_plantilla(self) -> None:
        """La plantilla declara `index_patterns: ["archivos-*"]`; si los nombres por
        nodo no encajaran, se indexaría SIN mapping (nombre como `text`, no
        `wildcard`) y la búsqueda por nombre dejaría de funcionar."""
        import json
        from pathlib import Path

        from normalizacion.core.indexador.opensearch import indice_escritura

        raiz = Path(__file__).resolve().parents[2]
        patrones = json.loads(
            (raiz / "deploy" / "mappings" / "archivos.json").read_text(encoding="utf-8")
        )["index_patterns"]
        assert patrones == ["archivos-*"]
        configs = (
            _cfg(),
            _hibrido("hibrido-ingesta", "mac-01"),
            _hibrido("hibrido-servicio", "vps-01"),
        )
        for c in configs:
            assert indice_escritura(c).startswith("archivos-")

    def test_el_sink_escribe_al_alias_no_a_un_indice_fijo(self) -> None:
        """Un índice fijo hace que la rotación de ISM sea inútil: se crearía el
        índice nuevo y el sink seguiría escribiendo en el viejo para siempre."""
        from normalizacion.core.indexador.opensearch import SinkOpenSearch

        for c in (_cfg(), _hibrido("hibrido-servicio", "vps-01")):
            sink = SinkOpenSearch(c, cliente=object())
            assert sink._indice == c.indice_alias


class TestLocalNoCambiaNada:
    """Regresión: el default debe seguir siendo el sistema de siempre.

    Cada aserción de aquí congela algo que una fase del plan híbrido podría romper
    sin querer. Si cambia un valor, es una decisión — no un accidente."""

    def test_identificadores_de_archivo_intactos(self) -> None:
        """GOLDEN: `archivo_id` es sha256(f"{disco_id}:{ruta_rel}|{tamaño}|{mtime_ns}").

        Este hash literal es el contrato con TODO el corpus ya indexado: la cola y el
        índice (`_id = archivo_id`) lo usan como clave. Si este valor cambia, el
        corpus existente queda huérfano y habría que reindexarlo entero. Introducir
        K16 no lo toca, y ninguna fase futura debe tocarlo sin quererlo."""
        from normalizacion.ingesta.catalogo.walker import construir_fila

        fila = construir_fila(
            disco_id="d1", ruta_relativa="datos/x.csv", nombre="x.csv", tamano=10, mtime_ns=1
        )
        assert fila.archivo_id == (
            "ded1bbf5490f484e87fdadf4034b6cb82a4107105a875491d757dbe8a9886e32"
        )

    def test_indice_de_escritura_igual_que_siempre(self) -> None:
        """En `local`, el índice de escritura conserva su nombre histórico: sin él,
        el corpus ya indexado quedaría huérfano del alias de escritura."""
        from normalizacion.core.indexador.opensearch import indice_escritura

        assert indice_escritura(_cfg()) == "archivos-000001"

    def test_secciones_de_config_esperadas(self) -> None:
        """Congela la forma de la Config: añadir o quitar una sección es explícito."""
        assert set(Config.model_fields) >= {
            "filtro", "worker", "indexador", "recursos", "despliegue",
            "postgres_dsn", "opensearch_url", "indice_alias", "almacen_backend",
        }

    def test_perillas_criticas_sin_deriva(self) -> None:
        c = _cfg()
        assert c.indice_alias == "archivos"
        assert c.filtro.version_filtro == "reglas-v3-lista-blanca"
        assert c.filtro.umbral_hot == 65
        assert c.recursos.mem_por_worker_mb == 700
        assert c.recursos.politica == "conservador"
