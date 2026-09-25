"""Réplica: no volver a restaurar un snapshot que trae lo mismo que ya sirve la matriz.

Medido el 24/25-09: Lilith no escribe desde el 11-09 y aun así cada ciclo restauraba otra
vez sus 76,7 GB. Un ciclo tardaba 33-60 min con el disco mecánico de la matriz al 60-80 %,
los ciclos se pisaban y cada uno borraba la restauración a medias del anterior: 2 ciclos
buenos de 64 en 24 h. Los tres últimos snapshots tenían por shard los mismos ficheros y
bytes, y 0 ficheros nuevos.

La otra mitad del contrato importa igual: ante la duda se restaura. Un salto de más deja
la matriz sirviendo datos viejos sin que nada falle, que es peor que recopiar.
"""

from __future__ import annotations

import itertools
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from normalizacion.core import replicacion
from normalizacion.core.config import Config, PerillasDespliegue

REPO = "azazel-snapshots-lilith"
ORIGEN = "archivos-lilith-luna-01-000001"
RANURA = ORIGEN + "-r"

#: Un shard tal y como lo cuenta `_status`: (ficheros, bytes, ficheros nuevos).
QUIETO = {"0": (244, 35_857_067_168, 0)}  # los números reales del 000001 de Lilith

#: Repositorios registrados en la matriz el 25-09. Sólo los `azazel-snapshots*` son lunas.
REGISTRADOS = ("snap-mac01", "snap-vps01old", "azazel-snapshots", REPO)


class _ErrorHttp(Exception):
    """Como las excepciones de opensearch-py: `status_code` es el código HTTP, o "N/A"
    / "TIMEOUT" cuando la petición ni llegó a tener respuesta."""

    def __init__(self, status_code: int | str) -> None:
        super().__init__(status_code)
        self.status_code = status_code


def _matriz(perfil: str = "online") -> Config:
    """La matriz de verdad es `online` (medido el 25-09): su propio archivo maestro, que
    además recibe a las lunas porque su cron llama a `restaurar_ajenos` directamente."""
    return Config(
        _env_file=None,  # type: ignore[call-arg]
        despliegue=PerillasDespliegue(perfil=perfil, nodo_id="vps-01"),  # type: ignore[arg-type]
    )


class _OpenSearchFalso:
    """OpenSearch de mentira con lo que la huella necesita: `_status` por snapshot,
    `_recovery` por índice y el uuid de cada índice, que cambia en cada restore."""

    def __init__(self, snapshots: dict[str, dict[str, dict[str, tuple[int, int, int]]]]):
        #: snapshot → índice → shard → (ficheros, bytes, nuevos)
        self.snapshots = snapshots
        self.presentes: set[str] = set()
        self.colgados: set[str] = set()
        self.uuids: dict[str, str] = {}
        self.recuperado_de: dict[str, dict[str, Any]] = {}
        self.restaurados: list[tuple[str, str, str]] = []  # (índice, snapshot, destino)
        self.status_pedidos: list[str] = []
        #: snapshot → cuántas veces más falla su `_status` como falla la red.
        self.status_caido: dict[str, int] = {}
        #: índice → estado de `cluster.health`; sin entrada, verde.
        self.salud: dict[str, str] = {}
        self.borrados: list[str] = []
        self._contador = itertools.count(1)
        self.indices = self
        self.transport = self
        self.cluster = self

    def sirviendo(self, indice: str, *, desde: str | None = None, repo: str = REPO) -> None:
        """Deja `indice` colgado del alias, como tras un ciclo anterior. Con `desde`,
        OpenSearch recuerda de qué snapshot se restauró (hasta que reinicie)."""
        self.presentes.add(indice)
        self.colgados.add(indice)
        self.uuids[indice] = f"uuid-{next(self._contador)}"
        if desde:
            self.recuperado_de[indice] = {
                "repository": repo, "snapshot": desde, "index": ORIGEN,
            }

    # --------------------------------------------------------------- transport
    def perform_request(
        self, metodo: str, ruta: str, params: Any = None, body: Any = None
    ) -> Any:
        partes = ruta.strip("/").split("/")
        if metodo == "GET" and ruta.startswith("/_snapshot/") and ruta.endswith("/_all"):
            return {
                "snapshots": [
                    {"snapshot": s, "state": "SUCCESS", "indices": sorted(idx)}
                    for s, idx in self.snapshots.items()
                ]
            }
        if metodo == "GET" and ruta == "/_snapshot":
            return {r: {"type": "s3"} for r in REGISTRADOS}
        if metodo == "GET" and ruta.endswith("/_status"):
            snap = partes[2]
            self.status_pedidos.append(snap)
            if self.status_caido.get(snap, 0) > 0:
                self.status_caido[snap] -= 1
                raise _ErrorHttp("TIMEOUT")
            if snap not in self.snapshots:
                raise _ErrorHttp(404)  # snapshot_missing: se purgó
            return {
                "snapshots": [
                    {
                        "snapshot": snap,
                        "indices": {
                            i: {"shards": {sid: _shard(*v) for sid, v in shards.items()}}
                            for i, shards in self.snapshots[snap].items()
                        },
                    }
                ]
            }
        if metodo == "GET" and ruta.endswith("/_recovery"):
            vivo = partes[0]
            if vivo not in self.presentes:
                raise KeyError(vivo)
            origen = self.recuperado_de.get(vivo)
            shard = (
                {"id": 0, "primary": True, "type": "SNAPSHOT", "stage": "DONE", "source": origen}
                if origen
                else {"id": 0, "primary": True, "type": "EXISTING_STORE", "stage": "DONE"}
            )
            return {vivo: {"shards": [shard]}}
        if metodo == "POST" and ruta.endswith("/_restore"):
            destino = body["rename_replacement"]
            self.restaurados.append((body["indices"], partes[2], destino))
            self.presentes.add(destino)
            self.uuids[destino] = f"uuid-{next(self._contador)}"
            self.recuperado_de[destino] = {
                "repository": partes[1], "snapshot": partes[2], "index": body["indices"],
            }
            return {"accepted": True}
        raise AssertionError(f"petición no prevista: {metodo} {ruta}")

    # --------------------------------------------------------------- indices
    def get_alias(self, index: str | None = None) -> dict[str, Any]:
        if index and "*" not in index:
            if index not in self.presentes:
                raise KeyError(index)
            candidatos = [index]
        else:
            candidatos = sorted(self.presentes)
        return {i: {"aliases": {"archivos": {}} if i in self.colgados else {}} for i in candidatos}

    def exists(self, index: str | None = None) -> bool:
        return str(index) in self.presentes

    def delete(self, index: str | None = None) -> None:
        self.borrados.append(str(index))
        for d in (self.presentes, self.colgados):
            d.discard(str(index))
        self.uuids.pop(str(index), None)
        self.recuperado_de.pop(str(index), None)

    def get_settings(self, index: str | None = None, name: str | None = None) -> dict[str, Any]:
        return {str(index): {"settings": {"index": {"uuid": self.uuids[str(index)]}}}}

    def update_aliases(self, body: Any = None) -> dict[str, Any]:
        for accion in body["actions"]:
            if "add" in accion:
                self.colgados.add(accion["add"]["index"])
            if "remove" in accion:
                self.colgados.discard(accion["remove"]["index"])
        return {}

    # --------------------------------------------------------------- cluster
    def health(self, index: str | None = None, **kw: Any) -> dict[str, Any]:
        if kw.get("timeout") and not kw.get("request_timeout"):
            # opensearch-py toma entonces `timeout` como el del CLIENTE, y una cadena
            # como "1s" revienta en urllib3 (medido en la matriz el 25-09).
            raise _ErrorHttp("N/A")
        return {"status": self.salud.get(str(index), "green")}


def _shard(ficheros: int, bytes_: int, nuevos: int) -> dict[str, Any]:
    return {
        "stage": "DONE",
        "stats": {
            "incremental": {"file_count": nuevos, "size_in_bytes": nuevos * 1000},
            "total": {"file_count": ficheros, "size_in_bytes": bytes_},
        },
    }


class _Control:
    """La tabla `control` en memoria, detrás de un `psycopg.connect` de mentira."""

    def __init__(self) -> None:
        self.filas: dict[str, str] = {}
        self.backfills: list[dict[str, Any]] = []
        self.pausas: list[float] = []

    def connect(self, *_: Any, **__: Any) -> _Control:
        return self

    def __enter__(self) -> _Control:
        return self

    def __exit__(self, *_: object) -> bool:
        return False

    def commit(self) -> None:
        pass

    def execute(self, sql: str, params: Any = ()) -> Any:
        filas = self.filas
        consulta = " ".join(sql.split())

        class _Cursor:
            rowcount = 0

            def fetchone(self) -> Any:
                valor = filas.get(params[0])
                return (valor,) if valor is not None else None

            def fetchall(self) -> list[tuple[str, str]]:
                return [(k, v) for k, v in filas.items() if k.startswith(params[0])]

        if consulta.startswith("INSERT INTO control") and "DO NOTHING" in consulta:
            filas.setdefault(params[0], params[1])
        elif consulta.startswith("INSERT INTO control"):
            filas[params[0]] = params[1]
        elif consulta.startswith("DELETE FROM control"):
            _Cursor.rowcount = int(filas.pop(params[0], None) is not None)
        return _Cursor()

    def huella(self, repo: str = REPO, indice: str = ORIGEN) -> dict[str, Any] | None:
        valor = self.filas.get(f"replica_huella:{repo}:{indice}")
        return json.loads(valor) if valor else None


@pytest.fixture
def control(monkeypatch: pytest.MonkeyPatch) -> _Control:
    from normalizacion.entidades import backfill

    tabla = _Control()
    monkeypatch.setattr(replicacion.psycopg, "connect", tabla.connect)

    def _lanzar(*_: Any, **kw: Any) -> dict[str, Any]:
        tabla.backfills.append(kw)
        return {"lanzado": True}

    monkeypatch.setattr(backfill, "lanzar_en_fondo", _lanzar)
    # La pausa antes del reintento se anota, no se duerme.
    monkeypatch.setattr(replicacion.time, "sleep", tabla.pausas.append)
    return tabla


def _health_caducado(*_: Any, **__: Any) -> dict[str, Any]:
    """`cluster.health` que no llega a verde: el 408 que storage vio 14 veces en 24 h."""
    raise TimeoutError("408 health timed_out")


def _ciclo(cliente: _OpenSearchFalso, repo: str = REPO) -> replicacion.ResumenReplica:
    """Lo que ejecuta el cron de la luna en la matriz, tal cual."""
    return replicacion.restaurar_ajenos(_matriz(), cliente, refrescar=True, repositorio=repo)


# ------------------------------------------------------------------ lo idéntico no se recopia


class TestSinCambiosNoRestaura:
    def test_snapshot_identico_al_ultimo_restaurado_no_llama_a_restore(
        self, control: _Control
    ) -> None:
        """El caso de Lilith: el ciclo anterior restauró S1; S2 trae los mismos ficheros y
        bytes y no subió nada. Antes se recopiaban los 35,9 GB de este índice otra vez."""
        cliente = _OpenSearchFalso({"lilith-luna-01-20260925-210002": {ORIGEN: QUIETO}})
        assert _ciclo(cliente).indices == [ORIGEN]

        cliente.snapshots["lilith-luna-01-20260925-220001"] = {ORIGEN: QUIETO}
        r = _ciclo(cliente)

        assert len(cliente.restaurados) == 1, "el segundo ciclo no debe restaurar nada"
        assert r.indices == []
        assert r.sin_cambios == [ORIGEN]
        assert cliente.colgados == {ORIGEN}, "la copia que sirve no se toca"

    def test_el_salto_es_un_exito_y_sella_su_luna(self, control: _Control) -> None:
        """Si el salto no sellara, el lag de una luna quieta crecería sin parar y la
        alerta saltaría justo cuando la copia está al día."""
        cliente = _OpenSearchFalso({"lilith-luna-01-20260925-210002": {ORIGEN: QUIETO}})
        _ciclo(cliente)
        control.filas.clear()  # sin el sello del primer ciclo; la huella sale de _recovery
        cliente.snapshots["lilith-luna-01-20260925-220001"] = {ORIGEN: QUIETO}

        r = _ciclo(cliente)

        assert r.ok is True and r.motivo is None
        sello = json.loads(control.filas[f"replica_ultimo_restore:{REPO}"])
        assert sello["sin_cambios"] == [ORIGEN]

    def test_un_salto_no_toca_el_backfill_de_entidades(self, control: _Control) -> None:
        """Un restore borra el cursor del backfill: la próxima pasada barre el índice
        entero. Sin docs nuevos no hay nada que resolver y el cursor se conserva.

        (Relanzarlo desde aquí no costaba nada: llamado desde el `python -c` del cron,
        el hilo del backfill muere con el proceso. Medido el 25-09.)"""
        from normalizacion.entidades.backfill import _CURSOR_CLAVE

        cliente = _OpenSearchFalso({"lilith-luna-01-20260925-210002": {ORIGEN: QUIETO}})
        _ciclo(cliente)
        lanzados = len(control.backfills)
        control.filas[_CURSOR_CLAVE] = json.dumps({"despues_de": ["a1b2"]})
        cliente.snapshots["lilith-luna-01-20260925-220001"] = {ORIGEN: QUIETO}

        _ciclo(cliente)

        assert _CURSOR_CLAVE in control.filas, "el salto no debe invalidar el cursor"
        assert len(control.backfills) == lanzados

    def test_sin_huella_guardada_la_deduce_de_recovery(self, control: _Control) -> None:
        """El primer ciclo tras desplegar no tiene huella guardada. OpenSearch sí sabe de
        qué snapshot restauró lo que sirve: sin esto, ese ciclo recopiaría los 76,7 GB una
        vez más con el disco saturado. Y la anota para cuando OpenSearch reinicie."""
        cliente = _OpenSearchFalso(
            {
                "lilith-luna-01-20260925-210002": {ORIGEN: QUIETO},
                "lilith-luna-01-20260925-220001": {ORIGEN: QUIETO},
            }
        )
        cliente.sirviendo(ORIGEN, desde="lilith-luna-01-20260925-210002")

        r = _ciclo(cliente)

        assert cliente.restaurados == []
        assert r.sin_cambios == [ORIGEN]
        huella = control.huella()
        assert huella is not None
        assert huella["destino"] == ORIGEN
        assert huella["uuid"] == cliente.uuids[ORIGEN]

    def test_copia_amarilla_se_salta(self, control: _Control) -> None:
        """Amarillo es una réplica sin asignar, no datos que falten: no justifica recopiar
        decenas de GB."""
        cliente = _OpenSearchFalso({"lilith-luna-01-20260925-210002": {ORIGEN: QUIETO}})
        _ciclo(cliente)
        cliente.salud[ORIGEN] = "yellow"
        cliente.snapshots["lilith-luna-01-20260925-220001"] = {ORIGEN: QUIETO}

        r = _ciclo(cliente)

        assert len(cliente.restaurados) == 1
        assert r.sin_cambios == [ORIGEN]

    def test_el_salto_retira_el_huerfano_de_la_ranura_libre(self, control: _Control) -> None:
        """Un ciclo interrumpido deja la ranura libre ocupada y fuera del alias. Antes cada
        ciclo la borraba al preparar su restore; con el salto nadie lo haría, y ese restore
        a medias seguiría gastando E/S (el 25-09, 10,4 GB idénticos en `…-000003-r`)."""
        cliente = _OpenSearchFalso({"lilith-luna-01-20260925-210002": {ORIGEN: QUIETO}})
        _ciclo(cliente)
        cliente.presentes.add(RANURA)  # existe, pero no cuelga del alias
        cliente.snapshots["lilith-luna-01-20260925-220001"] = {ORIGEN: QUIETO}

        r = _ciclo(cliente)

        assert r.sin_cambios == [ORIGEN]
        assert RANURA in cliente.borrados
        assert RANURA not in cliente.presentes
        assert cliente.colgados == {ORIGEN}, "la copia que sirve no se toca"

    def test_un_status_pasajero_se_reintenta_en_vez_de_recopiar(
        self, control: _Control
    ) -> None:
        """Un timeout o un parpadeo de MinIO en `_status` dejaba la huella del candidato
        ilegible y se restauraban los 35,9 GB del índice. Un reintento basta."""
        cliente = _OpenSearchFalso({"lilith-luna-01-20260925-210002": {ORIGEN: QUIETO}})
        _ciclo(cliente)
        cliente.snapshots["lilith-luna-01-20260925-220001"] = {ORIGEN: QUIETO}
        cliente.status_caido["lilith-luna-01-20260925-220001"] = 1

        r = _ciclo(cliente)

        assert len(cliente.restaurados) == 1
        assert r.sin_cambios == [ORIGEN]
        assert control.pausas, "el reintento va tras una pausa"

    def test_status_se_pide_una_vez_por_snapshot_no_por_indice(
        self, control: _Control
    ) -> None:
        """Lilith trae 3 índices en cada snapshot: una sola lectura de metadatos basta."""
        otro = "archivos-lilith-luna-01-000002"
        cliente = _OpenSearchFalso(
            {"lilith-luna-01-20260925-220001": {ORIGEN: QUIETO, otro: QUIETO}}
        )
        _ciclo(cliente)
        assert cliente.status_pedidos.count("lilith-luna-01-20260925-220001") == 1


# ------------------------------------------------------------------ ante la duda, se restaura


class TestAnteLaDudaRestaura:
    def _tras_restaurar(self, siguiente: dict[str, tuple[int, int, int]]) -> _OpenSearchFalso:
        cliente = _OpenSearchFalso({"lilith-luna-01-20260925-210002": {ORIGEN: QUIETO}})
        _ciclo(cliente)
        cliente.snapshots["lilith-luna-01-20260925-220001"] = {ORIGEN: siguiente}
        _ciclo(cliente)
        return cliente

    def test_un_shard_cambiado_restaura(self, control: _Control) -> None:
        cliente = self._tras_restaurar({"0": (251, 35_857_990_001, 7)})
        assert [s for _, s, _ in cliente.restaurados] == [
            "lilith-luna-01-20260925-210002",
            "lilith-luna-01-20260925-220001",
        ]
        assert cliente.colgados == {RANURA}

    def test_totales_iguales_pero_con_ficheros_nuevos_restaura(
        self, control: _Control
    ) -> None:
        """Un candidato que subió ficheros cambió respecto a su anterior: si sus totales
        coinciden con lo que sirve, es casualidad, no identidad."""
        cliente = self._tras_restaurar({"0": (244, 35_857_067_168, 1)})
        assert len(cliente.restaurados) == 2

    def test_un_restore_fallido_no_deja_la_huella_adelantada(self, control: _Control) -> None:
        """S2 cambió y su restore falló; S3 no sube nada nuevo respecto a S2. Comparar S3
        con su anterior diría «igual»; comparado con lo que SIRVE, no lo es."""
        cliente = _OpenSearchFalso({"lilith-luna-01-20260925-210002": {ORIGEN: QUIETO}})
        _ciclo(cliente)
        cambiado = {"0": (251, 35_857_990_001, 7)}
        cliente.snapshots["lilith-luna-01-20260925-220001"] = {ORIGEN: cambiado}
        salud = cliente.health
        cliente.health = _health_caducado  # type: ignore[method-assign]
        assert _ciclo(cliente).ok is False
        cliente.health = salud  # type: ignore[method-assign]

        cliente.snapshots["lilith-luna-01-20260925-224001"] = {
            ORIGEN: {"0": (251, 35_857_990_001, 0)}
        }
        _ciclo(cliente)

        assert cliente.restaurados[-1][1] == "lilith-luna-01-20260925-224001"

    def test_sin_huella_previa_restaura(self, control: _Control) -> None:
        """Copia sirviendo, ninguna huella guardada y OpenSearch sin recuerdo de dónde
        salió (reinició: EXISTING_STORE). No hay forma de saberlo: se restaura."""
        cliente = _OpenSearchFalso({"lilith-luna-01-20260925-220001": {ORIGEN: QUIETO}})
        cliente.sirviendo(ORIGEN)

        r = _ciclo(cliente)

        assert cliente.restaurados == [(ORIGEN, "lilith-luna-01-20260925-220001", RANURA)]
        assert r.sin_cambios == []

    def test_huella_de_otro_indice_fisico_no_vale(self, control: _Control) -> None:
        """Alguien sustituyó la copia (restore a mano, ciclo del código anterior) con el
        mismo nombre: la huella guardada describe un índice que ya no existe."""
        cliente = _OpenSearchFalso({"lilith-luna-01-20260925-210002": {ORIGEN: QUIETO}})
        _ciclo(cliente)
        cliente.uuids[ORIGEN] = "uuid-de-otro-restore"
        cliente.recuperado_de.pop(ORIGEN)
        cliente.snapshots["lilith-luna-01-20260925-220001"] = {ORIGEN: QUIETO}

        _ciclo(cliente)

        assert len(cliente.restaurados) == 2

    def test_recovery_de_otro_repositorio_no_vale(self, control: _Control) -> None:
        cliente = _OpenSearchFalso({"lilith-luna-01-20260925-220001": {ORIGEN: QUIETO}})
        cliente.snapshots["lilith-luna-01-20260925-210002"] = {ORIGEN: QUIETO}
        cliente.sirviendo(ORIGEN, desde="lilith-luna-01-20260925-210002", repo="otro-repo")

        _ciclo(cliente)

        assert len(cliente.restaurados) == 1

    def test_status_ilegible_restaura(self, control: _Control) -> None:
        """El snapshot del que salió la copia ya se purgó: `_status` da 404 y no hay con
        qué comparar. Un 404 no va a cambiar en 3 s: no se reintenta."""
        cliente = _OpenSearchFalso({"lilith-luna-01-20260925-220001": {ORIGEN: QUIETO}})
        cliente.sirviendo(ORIGEN, desde="lilith-luna-01-20260925-150000")  # purgado

        _ciclo(cliente)

        assert len(cliente.restaurados) == 1
        assert cliente.status_pedidos.count("lilith-luna-01-20260925-150000") == 1

    def test_status_caido_dos_veces_restaura(self, control: _Control) -> None:
        """El reintento es uno: si la red sigue caída, no se sabe y se restaura."""
        cliente = _OpenSearchFalso({"lilith-luna-01-20260925-210002": {ORIGEN: QUIETO}})
        _ciclo(cliente)
        cliente.snapshots["lilith-luna-01-20260925-220001"] = {ORIGEN: QUIETO}
        cliente.status_caido["lilith-luna-01-20260925-220001"] = 2

        _ciclo(cliente)

        assert len(cliente.restaurados) == 2
        assert cliente.status_pedidos.count("lilith-luna-01-20260925-220001") == 2

    def test_copia_roja_se_repone_aunque_no_cambie(self, control: _Control) -> None:
        """Antes del salto, cada ciclo sustituía la copia: una ROJA (shard perdido tras un
        reinicio sucio o con el disco lleno; en la matriz van con 0 réplicas) quedaba
        repuesta al ciclo siguiente. Saltarla la dejaría roja mientras la luna no cambie,
        y el alias devolvería resultados parciales sin marcar fallo."""
        cliente = _OpenSearchFalso({"lilith-luna-01-20260925-210002": {ORIGEN: QUIETO}})
        _ciclo(cliente)
        cliente.salud[ORIGEN] = "red"
        cliente.snapshots["lilith-luna-01-20260925-220001"] = {ORIGEN: QUIETO}

        r = _ciclo(cliente)

        assert cliente.restaurados[-1] == (ORIGEN, "lilith-luna-01-20260925-220001", RANURA)
        assert cliente.colgados == {RANURA}
        assert r.sin_cambios == []

    def test_dos_ranuras_en_el_alias_restaura(self, control: _Control) -> None:
        """Las dos ranuras colgadas duplican cada resultado de búsqueda. Saltar lo dejaría
        así para siempre; el restore lo deshace."""
        cliente = _OpenSearchFalso({"lilith-luna-01-20260925-210002": {ORIGEN: QUIETO}})
        _ciclo(cliente)
        cliente.sirviendo(RANURA)
        cliente.snapshots["lilith-luna-01-20260925-220001"] = {ORIGEN: QUIETO}

        _ciclo(cliente)

        assert len(cliente.restaurados) == 2
        assert len(cliente.colgados) == 1


# ------------------------------------------------------------------ la huella, al restaurar


class TestGuardaLaHuella:
    def test_tras_restaurar_con_exito_guarda_la_huella(self, control: _Control) -> None:
        cliente = _OpenSearchFalso({"lilith-luna-01-20260925-220001": {ORIGEN: QUIETO}})

        _ciclo(cliente)

        huella = control.huella()
        assert huella is not None
        assert huella["snapshot"] == "lilith-luna-01-20260925-220001"
        assert huella["destino"] == ORIGEN
        # Atada al índice FÍSICO: si otro lo sustituye con el mismo nombre, no vale.
        assert huella["uuid"] == cliente.uuids[ORIGEN]
        assert huella["shards"] == {"0": [244, 35_857_067_168]}

    def test_un_restore_fallido_no_guarda_huella(self, control: _Control) -> None:
        cliente = _OpenSearchFalso({"lilith-luna-01-20260925-220001": {ORIGEN: QUIETO}})
        cliente.health = _health_caducado  # type: ignore[method-assign]

        _ciclo(cliente)

        assert control.huella() is None


# ------------------------------------------------------------------ un sello por luna


def _sello(horas: float) -> str:
    ts = datetime.now(UTC) - timedelta(hours=horas)
    return json.dumps({"indices": [], "ts": ts.isoformat()})


class TestSelloPorLuna:
    def test_cada_luna_sella_su_propia_clave(self, control: _Control) -> None:
        cliente = _OpenSearchFalso({"lilith-luna-01-20260925-220001": {ORIGEN: QUIETO}})
        _ciclo(cliente)
        assert f"replica_ultimo_restore:{REPO}" in control.filas
        # la de siempre se sigue escribiendo: la leen los informes y el código anterior
        assert "replica_ultimo_restore" in control.filas

    @pytest.mark.parametrize("perfil", ["online", "hibrido-servicio"])
    def test_el_lag_es_el_de_la_luna_mas_atrasada(self, control: _Control, perfil: str) -> None:
        """Medido el 24-09: storage reescribía el sello único cada ~20 min mientras Lilith
        fallaba 62 de 64 ciclos. Y en la matriz, que es `online`, el lag ni siquiera miraba
        el restore: el exportador publicaba -1 el 25-09 con las dos lunas replicando."""
        control.filas["replica_ultimo_restore"] = _sello(0.1)  # storage acaba de sellar
        control.filas["replica_ultimo_restore:azazel-snapshots"] = _sello(0.1)
        control.filas[f"replica_ultimo_restore:{REPO}"] = _sello(5)  # Lilith, hace 5 h

        lag = replicacion.lag_segundos(_matriz(perfil))

        assert lag is not None
        assert lag == pytest.approx(5 * 3600, abs=60)

    def test_un_emisor_sigue_midiendo_su_snapshot(self, control: _Control) -> None:
        """Una luna mide su propio snapshot. En su Postgres no hay sellos de restore."""
        control.filas["replica_ultimo_snapshot"] = _sello(1)

        assert replicacion.lag_segundos(_matriz("online")) == pytest.approx(3600, abs=60)

    def test_una_luna_olvidada_no_desaparece_del_lag(self, control: _Control) -> None:
        """Los dos cron se reanudan a mano tras el reproceso. Si se reanuda storage y a
        Lilith se le olvida, Lilith no tiene sello propio: sin sembrarlo, el lag era sólo
        el de storage y no avisaba nadie."""
        control.filas["replica_ultimo_restore"] = _sello(5)  # el último ciclo del código viejo
        cliente = _OpenSearchFalso({"vps-storage-01-20260925-221503": {ORIGEN: QUIETO}})

        assert _ciclo(cliente, repo="azazel-snapshots").ok

        assert replicacion.lag_segundos(_matriz()) == pytest.approx(5 * 3600, abs=60)
        assert set(replicacion.lag_por_luna(_matriz())) == {"azazel-snapshots", REPO}
        # Las copias viejas registradas no son lunas: sembrarlas alertaría para siempre.
        assert "replica_ultimo_restore:snap-mac01" not in control.filas
        assert "replica_ultimo_restore:snap-vps01old" not in control.filas

    def test_sin_sello_anterior_la_luna_olvidada_empieza_a_contar(
        self, control: _Control
    ) -> None:
        """Instalación nueva: no hay sello único que copiar. La luna que falta empieza a
        envejecer desde ya, y avisa a las 2 h si no sella."""
        cliente = _OpenSearchFalso({"vps-storage-01-20260925-221503": {ORIGEN: QUIETO}})

        _ciclo(cliente, repo="azazel-snapshots")

        assert REPO in replicacion.lag_por_luna(_matriz())

    def test_la_siembra_no_pisa_el_sello_de_otra_luna(self, control: _Control) -> None:
        control.filas["replica_ultimo_restore"] = _sello(5)
        propio = _sello(0.5)
        control.filas[f"replica_ultimo_restore:{REPO}"] = propio
        cliente = _OpenSearchFalso({"vps-storage-01-20260925-221503": {ORIGEN: QUIETO}})

        _ciclo(cliente, repo="azazel-snapshots")

        assert control.filas[f"replica_ultimo_restore:{REPO}"] == propio

    @pytest.mark.parametrize("perfil", ["online", "hibrido-servicio"])
    def test_sin_sellos_por_luna_lee_el_de_antes(self, control: _Control, perfil: str) -> None:
        """El primer día tras desplegar ninguna luna ha sellado aún su clave: el lag no
        puede quedarse en «nunca»."""
        control.filas["replica_ultimo_restore"] = _sello(1)

        lag = replicacion.lag_segundos(_matriz(perfil))

        assert lag == pytest.approx(3600, abs=60)
