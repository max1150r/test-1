"""Modelos de datos compartidos."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum

from utils.text import canonical_url


class Status(str, Enum):
    UNKNOWN = "UNKNOWN"
    OUT_OF_STOCK = "OUT_OF_STOCK"
    IN_STOCK = "IN_STOCK"
    ERROR = "ERROR"
    BLOCKED = "BLOCKED"

    @property
    def is_stock_status(self) -> bool:
        return self in (Status.IN_STOCK, Status.OUT_OF_STOCK)


class EventKind(str, Enum):
    STOCK_AVAILABLE = "STOCK_AVAILABLE"
    STOCK_LOST = "STOCK_LOST"
    NEW_PRODUCT = "NEW_PRODUCT"
    PRICE_CHANGE = "PRICE_CHANGE"


@dataclass
class ProductInfo:
    store: str
    url: str
    status: Status = Status.UNKNOWN
    sku: str | None = None
    name: str | None = None
    price: float | None = None
    list_price: float | None = None      # precio tachado / anterior mostrado en la web
    image: str | None = None
    seller: str | None = None
    detected_at: float = field(default_factory=time.time)
    reason: str = ""                     # explicación breve (señales usadas o error)
    signals: list[str] = field(default_factory=list)
    response_time: float | None = None
    proxy: str | None = None
    http_status: int | None = None

    @property
    def key(self) -> str:
        return f"{self.store}:{self.sku}" if self.sku else f"{self.store}:{canonical_url(self.url)}"

    @property
    def display_name(self) -> str:
        return self.name or self.sku or self.url


@dataclass
class Event:
    kind: EventKind
    info: ProductInfo
    target_name: str = ""
    source: str = "url"                  # url | search | listing
    query: str | None = None
    previous_status: Status | None = None
    previous_price: float | None = None
    new_price: float | None = None


@dataclass
class FetchResult:
    ok: bool
    url: str
    final_url: str | None = None
    status_code: int | None = None
    text: str = ""
    elapsed: float = 0.0
    proxy: str | None = None
    error: str | None = None
    blocked: bool = False
    attempts: int = 1


@dataclass
class SearchPage:
    """Resultado de pedir una página de resultados/listado."""
    ok: bool
    items: list[ProductInfo] = field(default_factory=list)
    blocked: bool = False
    error: str | None = None
    elapsed: float = 0.0
    proxy: str | None = None
    http_status: int | None = None
    url: str = ""


@dataclass
class CheckResult:
    ok: bool
    status: Status = Status.UNKNOWN
    error: str | None = None
    blocked: bool = False
