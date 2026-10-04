"""Filtros de resultados de búsqueda (insensibles a mayúsculas y acentos)."""
from __future__ import annotations

from dataclasses import dataclass, field

from utils.text import normalize


@dataclass
class SearchFilter:
    query: str = ""
    include_keywords: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)
    include_mode: str = "any"      # any | all
    query_match: str = "all"       # all | any | none
    min_price: float | None = None
    max_price: float | None = None

    def evaluate(self, name: str | None, price: float | None) -> tuple[bool, str]:
        """Devuelve (acepta, motivo_si_rechaza)."""
        text = normalize(name or "")
        if not text:
            return False, "sin nombre"
        tokens = [t for t in normalize(self.query).split() if t]
        if tokens and self.query_match != "none":
            hits = [t in text for t in tokens]
            if self.query_match == "all" and not all(hits):
                return False, "no contiene todas las palabras de la búsqueda"
            if self.query_match == "any" and not any(hits):
                return False, "no contiene ninguna palabra de la búsqueda"
        inc = [normalize(k) for k in self.include_keywords if k.strip()]
        if inc:
            found = [k in text for k in inc]
            ok = all(found) if self.include_mode == "all" else any(found)
            if not ok:
                return False, "no cumple include_keywords"
        for k in self.exclude_keywords:
            nk = normalize(k)
            if nk and nk in text:
                return False, f"excluido por '{k}'"
        if price is not None:
            if self.min_price is not None and price < self.min_price:
                return False, "precio por debajo del mínimo"
            if self.max_price is not None and price > self.max_price:
                return False, "precio por encima del máximo"
        return True, ""
