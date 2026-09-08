"""Modo LUNA — normalizar sin quedarse el dato (rama `Moons`, ver `docs/MOONS.md`).

El test que de verdad importa aquí es `TestPuertaSinCopia`: con almacén nulo el
original es la ÚNICA copia que existe, y `reclamacion.py` vacía la carpeta de origen
en cuanto la puerta da verde. Si esa condición se relaja, el fallo no es un dato mal
contado: es el corpus del usuario borrado. No es un test de regresión, es el cerrojo.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest

from normalizacion.core import despliegue
from normalizacion.core.almacen import AlmacenNulo, crear_almacen
from normalizacion.core.config import Config, PerillasDespliegue


def _cfg(**kwargs: object) -> Config:
    """Config con perfil local explícito (la CI lanza la suite en varios perfiles)."""
    kwargs.setdefault("despliegue", PerillasDespliegue())
    return Config(_env_file=None, **kwargs)  # type: ignore[arg-type]


# ------------------------------------------------------------------ el almacén que no guarda


class TestAlmacenNulo:
    def test_la_fabrica_lo_devuelve_con_backend_ninguno(self) -> None:
        assert isinstance(crear_almacen(_cfg(almacen_backend="ninguno")), AlmacenNulo)

    def test_el_frio_tambien(self) -> None:
        """Sin copia caliente tampoco hay copia fría: el frío existe para que el COLD
        sobreviva al desechado del disco, y una luna no desecha nada."""
        from normalizacion.ingesta.workers.verificador import crear_almacen_frio

        assert isinstance(crear_almacen_frio(_cfg(almacen_backend="ninguno")), AlmacenNulo)

    def test_nunca_dice_que_tiene_el_blob(self) -> None:
        assert AlmacenNulo().existe("a" * 64) is False

    def test_guardar_no_consume_ni_escribe(self) -> None:
        """No-op de verdad: ni escribe ni agota la fuente. El worker sigue leyendo una
        vez y extrayendo; lo único que no ocurre es la escritura del blob."""
        fuente = io.BytesIO(b"contenido que no debe copiarse a ningun sitio")
        AlmacenNulo().guardar("b" * 64, fuente, 44)
        assert fuente.tell() == 0

    def test_leer_lanza_el_error_del_caso_sin_blob(self) -> None:
        """FileNotFoundError y no una excepción propia: es lo que ya levanta
        `AlmacenLocal.leer` cuando el blob no está, así que quien trata ese caso no
        necesita aprender un error nuevo."""
        with pytest.raises(FileNotFoundError) as exc:
            AlmacenNulo().leer("c" * 64)
        assert "no guarda copia" in str(exc.value)

    def test_el_default_sigue_siendo_minio(self) -> None:
        """Nadie entra en modo luna sin pedirlo: un despliegue que no toca nada
        conserva su almacén."""
        assert _cfg().almacen_backend == "minio"


# ------------------------------------------------------------------ disco_id sin pedírselo al operador


class TestDiscoIdDesdeRaiz:
    def test_deriva_de_la_ruta_relativa(self) -> None:
        cfg = _cfg(api_carpeta_raiz="/datos")
        assert despliegue.disco_id_desde_raiz(cfg, Path("/datos/SAT")) == "SAT"

    def test_dos_carpetas_homonimas_no_colisionan(self) -> None:
        """La razón de ser del cambio: el BASENAME colisiona (P1), la ruta relativa no.
        Es lo que permite que el operador solo elija carpeta."""
        cfg = _cfg(api_carpeta_raiz="/datos")
        a = despliegue.disco_id_desde_raiz(cfg, Path("/datos/2026/SAT"))
        b = despliegue.disco_id_desde_raiz(cfg, Path("/datos/2025/SAT"))
        assert a != b
        assert (a, b) == ("2026/SAT", "2025/SAT")

    def test_fuera_de_la_raiz_no_se_deriva(self) -> None:
        """Fuera de la raíz no hay unicidad garantizada: ahí el id explícito sigue
        siendo obligatorio y `disco_id_desde_raiz` se aparta."""
        cfg = _cfg(api_carpeta_raiz="/datos")
        assert despliegue.disco_id_desde_raiz(cfg, Path("/home/otro/SAT")) is None

    def test_la_raiz_misma_no_es_un_disco(self) -> None:
        cfg = _cfg(api_carpeta_raiz="/datos")
        assert despliegue.disco_id_desde_raiz(cfg, Path("/datos")) is None

    def test_el_prefijo_de_nodo_separa_lunas_distintas(self) -> None:
        """Entre nodos la desambigua `normalizar_disco_id`: dos lunas con la misma
        carpeta producen ids distintos."""
        una = despliegue.normalizar_disco_id(
            _cfg(despliegue=PerillasDespliegue(perfil="hibrido-ingesta", nodo_id="luna-01")),  # type: ignore[arg-type]
            "SAT",
        )
        otra = despliegue.normalizar_disco_id(
            _cfg(despliegue=PerillasDespliegue(perfil="hibrido-ingesta", nodo_id="luna-02")),  # type: ignore[arg-type]
            "SAT",
        )
        assert una == "luna-01:SAT"
        assert otra == "luna-02:SAT"
        assert una != otra


# ------------------------------------------------------------------ el cerrojo


class _ConexionFalsa:
    """Postgres de mentira: devuelve el recuento pactado y anota lo que se escribe."""

    def __init__(self, recuento: tuple[int, int, int, int]) -> None:
        self._recuento = recuento
        self.escrituras: list[tuple[str, Any]] = []

    def execute(self, sql: str, params: Any = None) -> Any:
        if sql.lstrip().upper().startswith("UPDATE"):
            self.escrituras.append((sql, params))
        conn = self

        class _Resultado:
            def fetchone(self) -> Any:
                return conn._recuento if "COUNT(*)" in sql else None

        return _Resultado()

    def commit(self) -> None:
        pass

    def __enter__(self) -> _ConexionFalsa:
        return self

    def __exit__(self, *_: object) -> bool:
        return False


def _puerta(monkeypatch: pytest.MonkeyPatch, config: Config, recuento: tuple[int, int, int, int]) -> Any:
    from normalizacion.core import cola
    from normalizacion.ingesta.workers import verificador

    conn = _ConexionFalsa(recuento)
    monkeypatch.setattr(verificador.psycopg, "connect", lambda *a, **k: conn)
    monkeypatch.setattr(cola, "disco_existe", lambda *a, **k: True)
    return verificador.evaluar_puerta(config, "luna-01:SAT")


class TestPuertaSinCopia:
    """Con `almacen_backend='ninguno'` la puerta NUNCA da verde."""

    def test_todo_hecho_y_aun_asi_roja(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """El caso peligroso: 2 filas, 2 HECHO, 0 pendientes. Con almacén normal esto
        es verde y autoriza a `reclamacion.py` a vaciar la carpeta. Sin copia, el
        índice guarda texto y metadatos — no los bytes del original — así que 'todo
        hecho' significa 'extraído', no 'a salvo'."""
        cfg = _cfg(
            almacen_backend="ninguno",
            despliegue=PerillasDespliegue(perfil="hibrido-ingesta", nodo_id="luna-01"),  # type: ignore[arg-type]
        )
        estado = _puerta(monkeypatch, cfg, (2, 2, 0, 0))
        assert estado.pendientes == 0, "el escenario debe ser el de puerta verde"
        assert estado.seguro_para_desechar is False
        assert estado.motivo_bloqueo == "sin_copia_el_origen_es_la_unica"

    def test_lo_que_se_persiste_tambien_es_rojo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`discos.seguro_para_desechar` es lo que lee el vigilante: si en memoria
        dice rojo y en la tabla queda verde, el cerrojo no sirve de nada."""
        from normalizacion.core import cola
        from normalizacion.ingesta.workers import verificador

        cfg = _cfg(
            almacen_backend="ninguno",
            despliegue=PerillasDespliegue(perfil="hibrido-ingesta", nodo_id="luna-01"),  # type: ignore[arg-type]
        )
        conn = _ConexionFalsa((2, 2, 0, 0))
        monkeypatch.setattr(verificador.psycopg, "connect", lambda *a, **k: conn)
        monkeypatch.setattr(cola, "disco_existe", lambda *a, **k: True)
        verificador.evaluar_puerta(cfg, "luna-01:SAT")
        assert conn.escrituras, "la puerta debe persistir su veredicto"
        assert conn.escrituras[0][1][0] is False

    def test_con_almacen_de_verdad_el_verde_sigue_siendo_posible(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El cerrojo no puede romper el flujo canónico: con almacén real y todo a
        salvo, un nodo maestro sigue pudiendo desechar su disco."""
        cfg = _cfg(almacen_backend="minio")  # local ⇒ archivo maestro
        estado = _puerta(monkeypatch, cfg, (2, 2, 0, 0))
        assert estado.seguro_para_desechar is True
        assert estado.motivo_bloqueo is None


# ------------------------------------------------------------------ el restore que sí trae lo nuevo


class _ClienteFalso:
    """OpenSearch de mentira: sirve los snapshots pactados y anota qué se restaura."""

    def __init__(self, snapshots: list[dict[str, Any]], presentes: tuple[str, ...] = ()) -> None:
        self._snapshots = snapshots
        self._presentes = set(presentes)
        self.restaurados: list[tuple[str, str]] = []
        self.borrados: list[str] = []
        self.indices = self
        self.transport = self

    def perform_request(
        self, metodo: str, ruta: str, params: Any = None, body: Any = None
    ) -> Any:
        if metodo == "GET":
            return {"snapshots": self._snapshots}
        if metodo == "POST" and ruta.endswith("_restore"):
            self.restaurados.append((str(body["indices"]), ruta.split("/")[3]))
        return {}

    def get_alias(self, index: str | None = None) -> dict[str, Any]:
        return dict.fromkeys(self._presentes, {})

    def delete(self, index: str | None = None) -> None:
        self.borrados.append(str(index))
        self._presentes.discard(str(index))

    def put_alias(self, index: str | None = None, name: str | None = None, body: Any = None) -> None:
        return None


def _luna(nodo: str = "vps-01") -> Config:
    """Receptor: `hibrido-servicio` no es archivo maestro, así que restaura."""
    return _cfg(despliegue=PerillasDespliegue(perfil="hibrido-servicio", nodo_id=nodo))  # type: ignore[arg-type]


_SNAPS = [
    {"snapshot": "luna-20260907-100000", "state": "SUCCESS", "indices": ["archivos-luna-000001"]},
    {"snapshot": "luna-20260907-230000", "state": "SUCCESS", "indices": ["archivos-luna-000001"]},
    {"snapshot": "luna-20260907-150000", "state": "SUCCESS", "indices": ["archivos-luna-000001"]},
]


class TestRestauraElMasReciente:
    def test_elige_el_snapshot_mas_reciente_no_el_primero(self) -> None:
        """El bug que costó 22 documentos: recorrer los snapshots en orden y dar el
        índice por restaurado en el PRIMERO que lo trae restauraba siempre el más
        antiguo. Contra un emisor que fotografía su índice cada ciclo, lo nuevo no
        llegaba nunca — y sin error, que es lo peor."""
        from normalizacion.core import replicacion

        cliente = _ClienteFalso(_SNAPS)
        replicacion.restaurar_ajenos(_luna(), cliente)
        assert cliente.restaurados == [("archivos-luna-000001", "luna-20260907-230000")]

    def test_un_indice_ya_presente_se_salta_por_defecto(self) -> None:
        """Un restore sobre un índice abierto falla, y retirarlo es destructivo: no
        puede pasar por sorpresa."""
        from normalizacion.core import replicacion

        cliente = _ClienteFalso(_SNAPS, presentes=("archivos-luna-000001",))
        replicacion.restaurar_ajenos(_luna(), cliente)
        assert cliente.restaurados == []
        assert cliente.borrados == []

    def test_con_refrescar_sustituye_la_copia_vieja(self) -> None:
        """Lo que necesita una replicación periódica: la versión nueva sustituye a la
        vieja, y para eso hay que retirarla antes."""
        from normalizacion.core import replicacion

        cliente = _ClienteFalso(_SNAPS, presentes=("archivos-luna-000001",))
        replicacion.restaurar_ajenos(_luna(), cliente, refrescar=True)
        assert cliente.borrados == ["archivos-luna-000001"]
        assert cliente.restaurados == [("archivos-luna-000001", "luna-20260907-230000")]

    def test_nunca_restaura_el_indice_propio(self) -> None:
        """Restaurar el propio lo sobrescribiría con una copia vieja: el fallo más
        caro y el más silencioso del diseño híbrido."""
        from normalizacion.core import replicacion

        propios = [
            {
                "snapshot": "vps-01-20260907-230000",
                "state": "SUCCESS",
                "indices": ["archivos-vps-01-000001"],
            }
        ]
        cliente = _ClienteFalso(propios)
        replicacion.restaurar_ajenos(_luna("vps-01"), cliente)
        assert cliente.restaurados == []

    def test_ignora_snapshots_fallidos(self) -> None:
        """Un snapshot a medias no es una fuente válida — y como el más reciente gana,
        uno fallido reciente secuestraría el restore."""
        from normalizacion.core import replicacion

        mezcla = [
            {"snapshot": "luna-20260907-100000", "state": "SUCCESS", "indices": ["archivos-luna-000001"]},
            {"snapshot": "luna-20260907-235959", "state": "PARTIAL", "indices": ["archivos-luna-000001"]},
        ]
        cliente = _ClienteFalso(mezcla)
        replicacion.restaurar_ajenos(_luna(), cliente)
        assert cliente.restaurados == [("archivos-luna-000001", "luna-20260907-100000")]
