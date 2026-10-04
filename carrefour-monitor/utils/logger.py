"""Logging con niveles propios, rotación de ficheros y ocultación de secretos."""
from __future__ import annotations

import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

CHECK, OOS, PROXY, STOCK, BLOCKED = 21, 22, 23, 24, 26
for _n, _name in ((CHECK, "CHECK"), (OOS, "OOS"), (PROXY, "PROXY"), (STOCK, "STOCK"), (BLOCKED, "BLOCKED")):
    logging.addLevelName(_n, _name)

_secrets: set[str] = set()
_WEBHOOK_RE = re.compile(r"https?://(?:[\w-]+\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w\-]+", re.I)
_CREDS_RE = re.compile(r"(\b[a-z][a-z0-9+.\-]*://)([^/\s:@]+):([^@\s/]+)@", re.I)


def register_secret(value: str | None) -> None:
    if value and len(value) >= 6:
        _secrets.add(value)


def redact(text: str) -> str:
    text = _WEBHOOK_RE.sub("https://discord.com/api/webhooks/***", text)
    text = _CREDS_RE.sub(r"\1***:***@", text)
    for s in _secrets:
        text = text.replace(s, "***")
    return text


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


class Log:
    """Fachada fina sobre ``logging`` con los niveles CHECK/OOS/STOCK/PROXY/BLOCKED."""

    def __init__(self) -> None:
        self._l = logging.getLogger("carrefour_monitor")

    def debug(self, msg, *a): self._l.debug(msg, *a)
    def info(self, msg, *a): self._l.info(msg, *a)
    def warning(self, msg, *a): self._l.warning(msg, *a)
    def error(self, msg, *a): self._l.error(msg, *a)
    def exception(self, msg, *a): self._l.exception(msg, *a)
    def check(self, msg, *a): self._l.log(CHECK, msg, *a)
    def oos(self, msg, *a): self._l.log(OOS, msg, *a)
    def stock(self, msg, *a): self._l.log(STOCK, msg, *a)
    def proxy(self, msg, *a): self._l.log(PROXY, msg, *a)
    def blocked(self, msg, *a): self._l.log(BLOCKED, msg, *a)


log = Log()


def setup_logging(level: str = "INFO", file: str | None = "logs/monitor.log",
                  max_bytes: int = 5_000_000, backup_count: int = 5, console: bool = True) -> None:
    logger = logging.getLogger("carrefour_monitor")
    logger.handlers.clear()
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False
    if console:
        try:
            sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(RedactingFormatter("[%(asctime)s] [%(levelname)s] %(message)s", "%H:%M:%S"))
        logger.addHandler(h)
    if file:
        Path(file).parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8")
        fh.setFormatter(RedactingFormatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"))
        logger.addHandler(fh)
    # Las librerías HTTP pueden volcar URLs completas: las silenciamos.
    for noisy in ("httpx", "httpcore", "hpack"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
