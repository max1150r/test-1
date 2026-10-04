"""Base común de los monitores: seguimiento de rachas de error y envío de eventos."""
from __future__ import annotations

from monitor.detector import ChangeDetector
from monitor.models import CheckResult, Event, EventKind, Status
from monitor.stats import Stats
from notifications.discord import DiscordNotifier
from utils.config import DiscordCfg, MonitorCfg, TargetCfg
from utils.logger import log


class BaseMonitor:
    kind = "base"

    def __init__(self, target: TargetCfg, interval: float, adapter, client, detector: ChangeDetector,
                 notifier: DiscordNotifier, stats: Stats, monitor_cfg: MonitorCfg, discord_cfg: DiscordCfg):
        self.target = target
        self.interval = interval
        self.adapter = adapter
        self.client = client
        self.detector = detector
        self.notifier = notifier
        self.stats = stats
        self.cfg = monitor_cfg
        self.discord_cfg = discord_cfg
        self.problem_streak = 0
        self.unknown_streak = 0

    @property
    def name(self) -> str:
        return self.target.name

    async def check(self) -> CheckResult:  # pragma: no cover - interfaz
        raise NotImplementedError

    def dispatch(self, events: list[Event]) -> None:
        for ev in events:
            self.notifier.notify_event(ev)

    def track(self, status: Status, detail: str = "") -> None:
        """Cuenta rachas y avisa por Discord (con cooldown) si un objetivo falla de forma sostenida."""
        thr = max(1, self.discord_cfg.error_threshold)
        if status in (Status.ERROR, Status.BLOCKED):
            self.problem_streak += 1
            if self.problem_streak >= thr:
                kind = "Bloqueo" if status == Status.BLOCKED else "Errores repetidos"
                self.notifier.notify_error(
                    key=f"{self.target.id}:{status.value}", title=kind, target=self.name,
                    details=f"{self.problem_streak} comprobaciones seguidas con problemas.\nÚltimo error: {detail}",
                )
            return
        if self.problem_streak >= thr:
            log.info("Recuperado: %s vuelve a responder", self.name)
        self.problem_streak = 0
        if status == Status.UNKNOWN:
            self.unknown_streak += 1
            if self.unknown_streak == max(10, thr * 2):
                self.notifier.notify_error(
                    key=f"{self.target.id}:UNKNOWN", title="Página no reconocida", target=self.name,
                    details=("La página responde pero no se pueden leer señales de stock. "
                             "Es posible que Carrefour haya cambiado su web: revisa stores/carrefour/detection.py "
                             f"o ejecuta `python main.py --inspect <URL>`.\nMotivo: {detail}"),
                )
        else:
            self.unknown_streak = 0
