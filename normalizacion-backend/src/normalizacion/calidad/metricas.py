"""Métricas de calidad de extracción. Funciones puras: entran dos textos, sale un número.

Nada aquí toca la BD, el índice ni el disco — así se pueden probar en un test unitario
y, sobre todo, así se puede confiar en que el número de hoy es comparable con el de la
semana que viene.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any


def _plegar(texto: str) -> str:
    """Minúsculas sin acentos. El OCR destroza acentos constantemente y contarlos como
    error hincharía el CER sin que cambie nada de lo que importa: `RAMIREZ` y `Ramírez`
    resuelven a la misma persona."""
    sin = unicodedata.normalize("NFKD", texto.lower())
    return "".join(c for c in sin if not unicodedata.combining(c))


def _normalizar_espacios(texto: str) -> str:
    """Colapsa espacios y saltos. El OCR reparte los saltos de línea de forma distinta
    en cada corrida y esa diferencia no es un error de lectura."""
    return re.sub(r"\s+", " ", texto).strip()


def distancia_levenshtein(a: str, b: str) -> int:
    """Distancia de edición. Implementación por filas: para dos páginas de texto la
    matriz completa serían millones de celdas y solo hacen falta dos filas."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    previa = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        actual = [i]
        for j, cb in enumerate(b, start=1):
            actual.append(
                min(
                    previa[j] + 1,           # borrado
                    actual[j - 1] + 1,       # inserción
                    previa[j - 1] + (ca != cb),  # sustitución
                )
            )
        previa = actual
    return previa[-1]


def cer(verdad: str, obtenido: str) -> float:
    """Character Error Rate en 0-1. 0 = perfecto. Puede pasar de 1 si el OCR alucina
    mucho más texto del que hay, y eso es información: significa ruido, no lectura."""
    v = _normalizar_espacios(_plegar(verdad))
    o = _normalizar_espacios(_plegar(obtenido))
    if not v:
        return 0.0 if not o else 1.0
    return round(distancia_levenshtein(v, o) / len(v), 4)


def wer(verdad: str, obtenido: str) -> float:
    """Word Error Rate en 0-1, sobre palabras en vez de caracteres."""
    v = _normalizar_espacios(_plegar(verdad)).split()
    o = _normalizar_espacios(_plegar(obtenido)).split()
    if not v:
        return 0.0 if not o else 1.0
    # Levenshtein sobre listas de palabras: se reusa el algoritmo mapeando cada
    # palabra única a un carácter privado del BMP.
    vocabulario: dict[str, str] = {}

    def clave(p: str) -> str:
        if p not in vocabulario:
            vocabulario[p] = chr(0xE000 + len(vocabulario))
        return vocabulario[p]

    return round(
        distancia_levenshtein("".join(clave(p) for p in v), "".join(clave(p) for p in o)) / len(v),
        4,
    )


@dataclass
class ResultadoAnclas:
    """Recall y precisión de anclas: LA métrica que decide."""

    esperadas: int = 0
    encontradas: int = 0
    correctas: int = 0
    faltantes: list[str] = field(default_factory=list)
    inventadas: list[str] = field(default_factory=list)

    @property
    def recall(self) -> float:
        """De las anclas que HAY, ¿cuántas se leyeron? Lo que se pierde aquí es
        información que el sistema nunca tendrá."""
        return round(self.correctas / self.esperadas, 4) if self.esperadas else 1.0

    @property
    def precision(self) -> float:
        """De las que se leyeron, ¿cuántas eran reales? Una CURP inventada crea una
        persona que no existe — cuesta más limpiarla que no haberla creado."""
        return round(self.correctas / self.encontradas, 4) if self.encontradas else 1.0

    def como_dict(self) -> dict[str, Any]:
        return {
            "esperadas": self.esperadas,
            "encontradas": self.encontradas,
            "correctas": self.correctas,
            "recall": self.recall,
            "precision": self.precision,
            "faltantes": self.faltantes[:10],
            "inventadas": self.inventadas[:10],
        }


def evaluar_anclas(esperadas: list[str], obtenidas: list[str]) -> ResultadoAnclas:
    """Compara las anclas anotadas a mano con las que salieron del texto extraído."""
    esp = {a.strip().upper() for a in esperadas if a.strip()}
    obt = {a.strip().upper() for a in obtenidas if a.strip()}
    correctas = esp & obt
    return ResultadoAnclas(
        esperadas=len(esp),
        encontradas=len(obt),
        correctas=len(correctas),
        faltantes=sorted(esp - obt),
        inventadas=sorted(obt - esp),
    )


@dataclass
class Medicion:
    """Lo medido para UN documento del conjunto dorado."""

    documento: str
    cer: float
    wer: float
    anclas: ResultadoAnclas
    confianza: float | None
    ms: int
    chars: int
    flags: list[str] = field(default_factory=list)

    def como_dict(self) -> dict[str, Any]:
        return {
            "documento": self.documento,
            "cer": self.cer,
            "wer": self.wer,
            "anclas": self.anclas.como_dict(),
            "confianza": self.confianza,
            "ms": self.ms,
            "chars": self.chars,
            "flags": self.flags,
        }


def agregar(mediciones: list[Medicion]) -> dict[str, Any]:
    """Resumen del conjunto completo.

    El recall y la precisión se agregan sumando aciertos sobre el total, NO promediando
    los porcentajes por documento: un documento con una sola CURP no debe pesar lo mismo
    que un padrón con doscientas.
    """
    if not mediciones:
        return {"documentos": 0}

    esperadas = sum(m.anclas.esperadas for m in mediciones)
    encontradas = sum(m.anclas.encontradas for m in mediciones)
    correctas = sum(m.anclas.correctas for m in mediciones)
    confianzas = [m.confianza for m in mediciones if m.confianza is not None]

    return {
        "documentos": len(mediciones),
        "cer_medio": round(sum(m.cer for m in mediciones) / len(mediciones), 4),
        "wer_medio": round(sum(m.wer for m in mediciones) / len(mediciones), 4),
        "anclas_esperadas": esperadas,
        "anclas_correctas": correctas,
        "recall_anclas": round(correctas / esperadas, 4) if esperadas else 1.0,
        "precision_anclas": round(correctas / encontradas, 4) if encontradas else 1.0,
        "confianza_media": round(sum(confianzas) / len(confianzas), 1) if confianzas else None,
        "ms_medio": round(sum(m.ms for m in mediciones) / len(mediciones)),
        "ms_total": sum(m.ms for m in mediciones),
        # Los peores: por dónde empezar a mirar cuando el número global no gusta.
        "peores": [
            m.documento
            for m in sorted(mediciones, key=lambda x: x.anclas.recall)[:5]
            if m.anclas.esperadas
        ],
    }


def comparar(antes: dict[str, Any], despues: dict[str, Any]) -> dict[str, Any]:
    """Delta entre dos corridas. Lo que se pega en el PR para justificar el cambio."""
    claves = (
        "cer_medio", "wer_medio", "recall_anclas", "precision_anclas",
        "confianza_media", "ms_medio",
    )
    delta: dict[str, Any] = {}
    for k in claves:
        a, d = antes.get(k), despues.get(k)
        if isinstance(a, (int, float)) and isinstance(d, (int, float)):
            delta[k] = {"antes": a, "despues": d, "delta": round(d - a, 4)}
    return delta
