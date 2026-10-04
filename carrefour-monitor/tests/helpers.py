from pathlib import Path

from monitor.models import FetchResult

FIXTURES = Path(__file__).parent / "fixtures"
URL_SOBRES = "https://www.carrefour.es/pokemon-30th-aniversario-pack-2-sobres-con-moneda-ingles-6-anos/VC4A-34535245/p"
URL_CAJA = "https://www.carrefour.es/pokemon-caja-elite-30th-aniversario-ingles-juego-de-mesa-6-anos-unboxing/VC4A-34535246/p"
LISTING_URL = "https://www.carrefour.es/juguetes/todos-los-juguetes-pokemon/F-13z0oZ1a42/c"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def ok_result(url: str, html: str, elapsed: float = 0.05) -> FetchResult:
    return FetchResult(ok=True, url=url, final_url=url, status_code=200, text=html, elapsed=elapsed, proxy=None)


class FakeClient:
    """Sustituye a HttpClient: devuelve respuestas guardadas por URL (lista = secuencia de respuestas)."""

    def __init__(self, routes: dict):
        self.routes = {k: (list(v) if isinstance(v, list) else [v]) for k, v in routes.items()}
        self.calls: list[str] = []

    async def fetch(self, url: str) -> FetchResult:
        self.calls.append(url)
        seq = self.routes.get(url)
        if seq is None:
            return FetchResult(ok=False, url=url, error="404 simulado", status_code=404)
        item = seq.pop(0) if len(seq) > 1 else seq[0]
        return item if isinstance(item, FetchResult) else ok_result(url, item)
