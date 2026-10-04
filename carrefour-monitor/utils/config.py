"""Carga y validación de la configuración (config.yaml + .env)."""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

try:  # python-dotenv es opcional en tiempo de ejecución
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


class ConfigError(Exception):
    """Error de configuración con mensaje legible para el usuario."""


@dataclass
class DiscordCfg:
    webhook_url: str = ""
    username: str = "Carrefour Monitor"
    avatar_url: str = ""
    send_startup: bool = True
    notify_out_of_stock: bool = False
    notify_price_changes: bool = False
    mention_on_stock: str = ""          # p.ej. "<@&ID_ROL>" o "@everyone"
    error_cooldown: float = 900.0       # s entre alertas de error iguales
    error_threshold: int = 5            # fallos consecutivos antes de avisar
    timeout: float = 10.0


@dataclass
class MonitorCfg:
    interval: float = 10.0
    jitter: float = 2.0
    timeout: float = 15.0
    retries: int = 3
    concurrency: int = 5
    backoff_base: float = 5.0
    backoff_max: float = 300.0
    min_interval: float = 1.0           # suelo duro de seguridad (no es un límite "bonito")
    max_requests_per_minute: int = 120  # limitador global
    confirm_stock: bool = True          # re-comprobar antes de avisar de stock
    stock_renotify_cooldown: float = 60.0
    min_positive_signals: int = 1
    enrich_new_products: bool = True
    max_enrich_per_cycle: int = 5


@dataclass
class NetworkCfg:
    proxies_file: str = "proxies.txt"
    proxy_strategy: str = "round_robin"  # round_robin | random
    proxy_cooldown: float = 120.0
    proxy_max_failures: int = 3
    proxy_fallback_direct: bool = False  # ¿usar tu IP si no quedan proxies?
    retry_backoff_base: float = 1.0
    retry_backoff_max: float = 30.0
    http2: bool = False
    accept_language: str = "es-ES,es;q=0.9"


@dataclass
class StoreCfg:
    base_url: str = "https://www.carrefour.es"
    search_url: str = "https://www.carrefour.es/?q={query}"
    page_size: int = 24
    offset_param: str = "offset"


@dataclass
class LoggingCfg:
    level: str = "INFO"
    file: str = "logs/monitor.log"
    max_bytes: int = 5_000_000
    backup_count: int = 5
    console: bool = True


@dataclass
class HealthCfg:
    summary_interval: float = 900.0     # 0 = desactivado
    to_discord: bool = False


@dataclass
class StateCfg:
    db_path: str = "data/state.db"


@dataclass
class TargetCfg:
    name: str
    kind: str                           # url | search | listing
    value: str
    interval: float | None = None
    enabled: bool = True
    include_keywords: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)
    include_mode: str = "any"           # any | all
    query_match: str = "all"            # all | any | none
    min_price: float | None = None
    max_price: float | None = None
    max_pages: int = 1
    notify_existing: bool = False

    @property
    def id(self) -> str:
        return hashlib.sha1(f"{self.kind}|{self.value}".encode()).hexdigest()[:12]


@dataclass
class AppConfig:
    discord: DiscordCfg
    monitor: MonitorCfg
    network: NetworkCfg
    store: StoreCfg
    logging: LoggingCfg
    health: HealthCfg
    state: StateCfg
    targets: list[TargetCfg]
    base_dir: Path

    def resolve(self, path: str) -> Path:
        p = Path(path)
        return p if p.is_absolute() else self.base_dir / p


_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def _to_bool(v: Any, where: str) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.strip().lower() in ("true", "yes", "si", "sí", "1", "on"):
        return True
    if isinstance(v, str) and v.strip().lower() in ("false", "no", "0", "off", ""):
        return False
    if isinstance(v, (int, float)) and v in (0, 1):
        return bool(v)
    raise ConfigError(f"{where}: se esperaba true/false y se recibió {v!r}")


def _build(cls, data: dict | None, section: str):
    data = data or {}
    if not isinstance(data, dict):
        raise ConfigError(f"La sección '{section}' debe ser un bloque clave: valor")
    known = {f.name: f for f in fields(cls)}
    unknown = set(data) - set(known)
    if unknown:
        raise ConfigError(
            f"Claves desconocidas en '{section}': {', '.join(sorted(unknown))}. "
            f"Permitidas: {', '.join(sorted(known))}"
        )
    inst = cls()
    for key, val in data.items():
        default = getattr(inst, key)
        where = f"{section}.{key}"
        try:
            if isinstance(default, bool):
                val = _to_bool(val, where)
            elif isinstance(default, int):
                val = int(val)
            elif isinstance(default, float):
                val = float(val)
            else:
                val = "" if val is None else str(val)
        except (TypeError, ValueError):
            raise ConfigError(f"{where}: valor no válido {val!r}") from None
        setattr(inst, key, val)
    return inst


_KIND_ALIASES = {"url": "url", "search": "search", "keyword": "search", "keywords": "search",
                 "busqueda": "search", "búsqueda": "search", "listing": "listing", "listado": "listing",
                 "category": "listing"}
_TARGET_KEYS = {"name", "type", "kind", "value", "url", "query", "interval", "enabled",
                "include_keywords", "exclude_keywords", "include_mode", "query_match",
                "min_price", "max_price", "max_pages", "notify_existing"}


def _str_list(v: Any, where: str) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    if isinstance(v, list):
        return [str(x) for x in v]
    raise ConfigError(f"{where}: debe ser una lista de textos")


def _build_target(raw: dict, default_kind: str | None, idx: int, section: str) -> TargetCfg:
    if not isinstance(raw, dict):
        raise ConfigError(f"{section}[{idx}] debe ser un bloque clave: valor")
    unknown = set(raw) - _TARGET_KEYS
    if unknown:
        raise ConfigError(f"{section}[{idx}]: claves desconocidas {sorted(unknown)}. Permitidas: {sorted(_TARGET_KEYS)}")
    kind_raw = raw.get("type", raw.get("kind", default_kind))
    if kind_raw is None and "query" in raw:
        kind_raw = "search"
    kind = _KIND_ALIASES.get(str(kind_raw or "").strip().lower())
    if not kind:
        raise ConfigError(f"{section}[{idx}]: 'type' debe ser url, search o listing (recibido {kind_raw!r})")
    value = raw.get("value") or raw.get("query") or raw.get("url")
    if not value or not str(value).strip():
        raise ConfigError(f"{section}[{idx}]: falta 'value' (URL o palabras clave)")
    value = str(value).strip()
    if kind in ("url", "listing") and not re.match(r"^https?://", value, re.I):
        raise ConfigError(f"{section}[{idx}]: '{value}' no es una URL http(s) válida")
    where = f"{section}[{idx}]"
    t = TargetCfg(name=str(raw.get("name") or value), kind=kind, value=value)
    try:
        if raw.get("interval") is not None:
            t.interval = float(raw["interval"])
        t.min_price = None if raw.get("min_price") is None else float(raw["min_price"])
        t.max_price = None if raw.get("max_price") is None else float(raw["max_price"])
        t.max_pages = int(raw.get("max_pages", 1))
    except (TypeError, ValueError):
        raise ConfigError(f"{where}: interval/min_price/max_price/max_pages deben ser números") from None
    if t.max_pages < 1:
        raise ConfigError(f"{where}: max_pages debe ser >= 1")
    t.enabled = _to_bool(raw.get("enabled", True), f"{where}.enabled")
    t.notify_existing = _to_bool(raw.get("notify_existing", False), f"{where}.notify_existing")
    t.include_keywords = _str_list(raw.get("include_keywords"), f"{where}.include_keywords")
    t.exclude_keywords = _str_list(raw.get("exclude_keywords"), f"{where}.exclude_keywords")
    t.include_mode = str(raw.get("include_mode", "any")).lower()
    t.query_match = str(raw.get("query_match", "all")).lower()
    if t.include_mode not in ("any", "all"):
        raise ConfigError(f"{where}.include_mode debe ser 'any' o 'all'")
    if t.query_match not in ("all", "any", "none"):
        raise ConfigError(f"{where}.query_match debe ser 'all', 'any' o 'none'")
    return t


def load_config(path: str | Path = "config.yaml", env_file: str | Path | None = None) -> AppConfig:
    path = Path(path)
    if not path.exists():
        raise ConfigError(
            f"No se encuentra '{path}'. Copia config.example.yaml a config.yaml y edítalo."
        )
    base_dir = path.resolve().parent
    if load_dotenv is not None:
        load_dotenv(env_file or base_dir / ".env", override=False)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"config.yaml no es un YAML válido: {exc}") from None
    if not isinstance(raw, dict):
        raise ConfigError("config.yaml debe contener un bloque raíz clave: valor")
    raw = _expand(raw)

    allowed = {"discord", "monitor", "network", "store", "logging", "health", "state", "products", "searches"}
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(f"Secciones desconocidas en config.yaml: {sorted(unknown)}. Permitidas: {sorted(allowed)}")

    discord = _build(DiscordCfg, raw.get("discord"), "discord")
    if not discord.webhook_url:
        discord.webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    monitor = _build(MonitorCfg, raw.get("monitor"), "monitor")
    network = _build(NetworkCfg, raw.get("network"), "network")
    store = _build(StoreCfg, raw.get("store"), "store")
    logging_cfg = _build(LoggingCfg, raw.get("logging"), "logging")
    health = _build(HealthCfg, raw.get("health"), "health")
    state = _build(StateCfg, raw.get("state"), "state")

    if monitor.interval <= 0 or monitor.timeout <= 0:
        raise ConfigError("monitor.interval y monitor.timeout deben ser > 0")
    if monitor.concurrency < 1:
        raise ConfigError("monitor.concurrency debe ser >= 1")
    if monitor.retries < 0 or monitor.jitter < 0:
        raise ConfigError("monitor.retries y monitor.jitter no pueden ser negativos")
    if network.proxy_strategy not in ("round_robin", "random"):
        raise ConfigError("network.proxy_strategy debe ser 'round_robin' o 'random'")
    if "{query}" not in store.search_url:
        raise ConfigError("store.search_url debe contener el marcador {query}")
    if discord.webhook_url and not re.match(r"^https://([\w-]+\.)?discord(app)?\.com/api/webhooks/", discord.webhook_url):
        raise ConfigError("discord.webhook_url no parece un webhook de Discord válido")

    targets: list[TargetCfg] = []
    for i, item in enumerate(raw.get("products") or []):
        targets.append(_build_target(item, None, i, "products"))
    for i, item in enumerate(raw.get("searches") or []):
        targets.append(_build_target(item, "search", i, "searches"))
    seen: set[str] = set()
    unique: list[TargetCfg] = []
    for t in targets:
        if t.id in seen:
            continue  # misma URL/consulta duplicada
        seen.add(t.id)
        unique.append(t)
    unique = [t for t in unique if t.enabled]

    return AppConfig(discord, monitor, network, store, logging_cfg, health, state, unique, base_dir)
