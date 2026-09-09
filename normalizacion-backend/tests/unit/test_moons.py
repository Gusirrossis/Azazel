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


# ------------------------------------------------------------------ el destino no puede reactivar la copia


class TestDestinoNoReactivaLaCopia:
    """Elegir carpeta de destino conmuta el almacén a backend `local`. En una luna eso
    sería una puerta trasera: reactiva la copia del corpus entero Y devuelve la puerta
    al verde, con `reclamacion.py` autorizado a vaciar el origen."""

    def test_con_almacen_nulo_el_destino_se_ignora(self, tmp_path: Path) -> None:
        from normalizacion.ingesta.pipeline import config_con_destino

        efectiva = config_con_destino(_cfg(almacen_backend="ninguno"), str(tmp_path))
        assert efectiva.almacen_backend == "ninguno"
        assert not (tmp_path / "almacen").exists(), "ni siquiera debe plantar las carpetas"

    def test_con_almacen_de_verdad_el_destino_sigue_funcionando(self, tmp_path: Path) -> None:
        """El cerrojo no puede romper el flujo canónico: quien sí guarda copia sigue
        pudiendo elegir dónde."""
        from normalizacion.ingesta.pipeline import config_con_destino

        efectiva = config_con_destino(_cfg(almacen_backend="minio"), str(tmp_path))
        assert efectiva.almacen_backend == "local"
        assert (tmp_path / "almacen").is_dir()

    def test_el_panel_no_anuncia_un_almacen_que_no_existe(self) -> None:
        """`destinos` es la pantalla donde el operador comprueba dónde quedó su dato.
        Decir 'minio://…/almacen' cuando no se guarda nada le haría creer que el
        origen ya es prescindible — que es exactamente la decisión peligrosa."""
        from normalizacion.ingesta.pipeline import destinos

        d = destinos(_cfg(almacen_backend="ninguno"))
        assert "minio://" not in d["originales_hot"]
        assert "sin copia" in d["originales_hot"]
        assert d["frio_reversible"] == d["originales_hot"]
        # El índice y la cola SÍ existen y deben seguir anunciándose.
        assert "alias" in d["indice_metadatos"]


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

    def __init__(
        self,
        snapshots: list[dict[str, Any]],
        presentes: tuple[str, ...] = (),
        alias: str = "archivos",
    ) -> None:
        self._snapshots = snapshots
        self._presentes = set(presentes)
        self._alias = alias
        #: Qué índices cuelgan AHORA del alias. El blue/green depende de esto: decide
        #: en qué ranura se restaura y cuál se retira, así que no puede ser un detalle.
        self._colgados = set(presentes)
        self.restaurados: list[tuple[str, str]] = []
        self.destinos: list[str] = []
        self.borrados: list[str] = []
        self.esperas: list[tuple[str, Any, Any]] = []
        self.swaps: list[Any] = []
        self.indices = self
        self.transport = self
        self.cluster = self

    def perform_request(
        self, metodo: str, ruta: str, params: Any = None, body: Any = None
    ) -> Any:
        if metodo == "GET":
            return {"snapshots": self._snapshots}
        if metodo == "POST" and ruta.endswith("_restore"):
            self.restaurados.append((str(body["indices"]), ruta.split("/")[3]))
            # El destino real: con blue/green el restore RENOMBRA a la otra ranura.
            destino = (body or {}).get("rename_replacement") or str(body["indices"])
            self.destinos.append(destino)
            self._presentes.add(destino)  # existe, pero AÚN NO cuelga del alias
        return {}

    def get_alias(self, index: str | None = None) -> dict[str, Any]:
        if index and "*" not in index:
            if index not in self._presentes:
                raise KeyError(index)  # como OpenSearch: 404 si no existe
            candidatos = [index]
        else:
            candidatos = sorted(self._presentes)
        return {
            i: {"aliases": {self._alias: {}} if i in self._colgados else {}} for i in candidatos
        }

    def exists(self, index: str | None = None) -> bool:
        return str(index) in self._presentes

    def delete(self, index: str | None = None) -> None:
        self.borrados.append(str(index))
        self._presentes.discard(str(index))
        self._colgados.discard(str(index))

    def put_alias(self, index: str | None = None, name: str | None = None, body: Any = None) -> None:
        self._colgados.add(str(index))

    def health(self, index: str | None = None, **kw: Any) -> dict[str, Any]:
        """`cluster.health` — anota que se esperó, y a qué."""
        self.esperas.append((str(index), kw.get("wait_for_status"), kw.get("request_timeout")))
        return {"status": "green"}

    def update_aliases(self, body: Any = None) -> dict[str, Any]:
        """El swap atómico: en UNA operación entra el nuevo y sale el viejo."""
        self.swaps.append(body)
        for accion in (body or {}).get("actions", []):
            if "add" in accion:
                self._colgados.add(accion["add"]["index"])
            if "remove" in accion:
                self._colgados.discard(accion["remove"]["index"])
        return {}


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
        vieja. Ya NO borrando la vieja primero — ver `TestBlueGreen`."""
        from normalizacion.core import replicacion

        cliente = _ClienteFalso(_SNAPS, presentes=("archivos-luna-000001",))
        replicacion.restaurar_ajenos(_luna(), cliente, refrescar=True)
        assert cliente.restaurados == [("archivos-luna-000001", "luna-20260907-230000")]
        assert cliente.destinos == ["archivos-luna-000001-r"]
        assert "archivos-luna-000001" in cliente.borrados


class TestBlueGreen:
    """El refresco borraba el índice VIVO y restauraba encima. Medido en la matriz: con
    24,4 GB el shard tarda ~12 min en recuperarse y el ciclo corre cada 20, así que la
    copia estaba a medias más de la mitad del tiempo. Y fallaba en silencio — una
    búsqueda sobre el alias devolvía menos resultados marcando `failed: 0`."""

    def _refrescar(self, presentes: tuple[str, ...] = ("archivos-luna-000001",)):
        from normalizacion.core import replicacion

        cliente = _ClienteFalso(_SNAPS, presentes=presentes)
        replicacion.restaurar_ajenos(_luna(), cliente, refrescar=True)
        return cliente

    def test_restaura_en_la_otra_ranura_no_encima(self) -> None:
        cliente = self._refrescar()
        assert cliente.destinos == ["archivos-luna-000001-r"]

    def test_espera_a_verde_antes_de_tocar_el_alias(self) -> None:
        """`wait_for_completion` vuelve cuando el shard está ASIGNADO, no recuperado.
        Sin esta espera el swap metería en el alias el índice a medias."""
        cliente = self._refrescar()
        assert cliente.esperas, "no se esperó a que el índice quedara verde"
        indice, estado, req_timeout = cliente.esperas[0]
        assert indice == "archivos-luna-000001-r"
        assert estado == "green"
        # El timeout del CLIENTE va aparte del de servidor: sin él, esperar 60 min se
        # corta a los 30 s con un ConnectionTimeout que parece un fallo y no lo es.
        assert req_timeout and req_timeout > 60

    def test_el_cambio_de_alias_es_UNA_operacion(self) -> None:
        """Entrar el nuevo y sacar el viejo en llamadas separadas deja un instante con
        los dos, o con ninguno. Tiene que ser atómico."""
        cliente = self._refrescar()
        assert len(cliente.swaps) == 1
        acciones = cliente.swaps[0]["actions"]
        assert {"add", "remove"} == {k for a in acciones for k in a}
        añadido = next(a["add"]["index"] for a in acciones if "add" in a)
        quitado = next(a["remove"]["index"] for a in acciones if "remove" in a)
        assert (añadido, quitado) == ("archivos-luna-000001-r", "archivos-luna-000001")

    def test_el_viejo_NO_se_borra_antes_del_swap(self) -> None:
        """LA invariante. Si se borra antes, hay una ventana sin índice servible — que
        es exactamente el fallo que esto viene a curar."""
        cliente = self._refrescar()
        assert cliente.swaps, "no hubo swap"
        # El fake sólo registra borrados de índices que existían; el viejo tiene que
        # seguir colgado del alias hasta que el swap lo retira.
        assert "archivos-luna-000001" in cliente.borrados
        assert cliente.destinos == ["archivos-luna-000001-r"]
        # y el que queda sirviendo es el nuevo
        colgados = {
            i for i, v in cliente.get_alias(index="archivos-*").items() if v["aliases"]
        }
        assert colgados == {"archivos-luna-000001-r"}

    def test_dos_indices_del_emisor_NO_se_pisan(self) -> None:
        """La regresión que costó una copia entera. El emisor ROTA sus índices (ISM):
        la luna de Lilith pasó a tener `…-000001` (431.715 docs) y `…-000002`
        (159.999). Con las ranuras derivadas del NÚMERO final, restaurar el primero
        escribía sobre el segundo y viceversa — la matriz acabó con una sola copia, y
        suelta del alias. Las ranuras tienen que salir del nombre COMPLETO."""
        from normalizacion.core import replicacion

        snaps = [
            {
                "snapshot": "luna-20260909-170004",
                "state": "SUCCESS",
                "indices": ["archivos-luna-000001", "archivos-luna-000002"],
            }
        ]
        cliente = _ClienteFalso(snaps, presentes=("archivos-luna-000001", "archivos-luna-000002"))
        replicacion.restaurar_ajenos(_luna(), cliente, refrescar=True)

        # Cada origen a SU ranura, y ninguna coincide con el otro origen.
        assert sorted(cliente.destinos) == [
            "archivos-luna-000001-r",
            "archivos-luna-000002-r",
        ]
        assert "archivos-luna-000002" not in cliente.destinos
        assert "archivos-luna-000001" not in cliente.destinos
        # y los dos acaban sirviendo: no se perdió ninguna copia
        colgados = {i for i, v in cliente.get_alias(index="archivos-*").items() if v["aliases"]}
        assert colgados == {"archivos-luna-000001-r", "archivos-luna-000002-r"}

    def test_alterna_de_vuelta_en_el_siguiente_ciclo(self) -> None:
        """Dos ranuras fijas y no un nombre nuevo cada vez: así el juego de índices que
        puede existir está acotado y no quedan residuos de ciclos viejos."""
        cliente = self._refrescar(presentes=("archivos-luna-000001-r",))
        assert cliente.destinos == ["archivos-luna-000001"]

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


class TestHuerfanos:
    """Un ciclo roto deja índices sueltos. Restaurar sobre uno de ellos falla con
    `an open index with same name already exists` y mata el ciclo entero — pasó en
    producción y dejó la copia de una luna a medias."""

    def test_no_restaura_sobre_un_huerfano_suelto(self) -> None:
        from normalizacion.core import replicacion

        snaps = [
            {
                "snapshot": "luna-20260909-170004",
                "state": "SUCCESS",
                "indices": ["archivos-luna-000001"],
            }
        ]
        # El huérfano EXISTE pero no cuelga del alias: es el estado que rompía.
        cliente = _ClienteFalso(snaps)
        cliente._presentes.add("archivos-luna-000001")
        replicacion.restaurar_ajenos(_luna(), cliente, refrescar=True)
        assert cliente.destinos == ["archivos-luna-000001-r"], (
            "con el nombre propio ocupado hay que ir a la ranura, no restaurar encima"
        )

    def test_si_el_nombre_esta_libre_se_usa(self) -> None:
        from normalizacion.core import replicacion

        snaps = [
            {
                "snapshot": "luna-20260909-170004",
                "state": "SUCCESS",
                "indices": ["archivos-luna-000001"],
            }
        ]
        cliente = _ClienteFalso(snaps)
        replicacion.restaurar_ajenos(_luna(), cliente, refrescar=True)
        assert cliente.destinos == ["archivos-luna-000001"]
