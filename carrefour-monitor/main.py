"""Carrefour Monitor - punto de entrada.

Uso:
    python main.py                      # monitoriza de forma continua
    python main.py --once               # una sola pasada y salir (para probar)
    python main.py --check-config       # valida config.yaml y sale
    python main.py --test-discord       # envía un mensaje de prueba al webhook
    python main.py --inspect URL        # descarga una URL y muestra lo que el parser entiende (sin avisos)
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

if sys.version_info < (3, 11):
    raise SystemExit("Se necesita Python 3.11 o superior (https://www.python.org/downloads/).")

from utils.config import ConfigError, load_config  # noqa: E402


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Monitor de stock de Carrefour España con avisos por Discord")
    p.add_argument("-c", "--config", default="config.yaml", help="ruta del fichero de configuración")
    p.add_argument("--once", action="store_true", help="hacer una sola pasada y salir")
    p.add_argument("--check-config", action="store_true", help="validar la configuración y salir")
    p.add_argument("--test-discord", action="store_true", help="enviar un mensaje de prueba a Discord")
    p.add_argument("--inspect", metavar="URL", help="analizar una URL (ficha o listado) y mostrar el resultado")
    return p.parse_args(argv)


async def test_discord(cfg) -> int:
    from app import Application
    from notifications import discord as dc

    app = Application(cfg)
    try:
        await app.notifier.start()
        if not app.notifier.enabled:
            print("No hay webhook configurado. Define DISCORD_WEBHOOK_URL en .env")
            return 1
        ok = await app.notifier.send_now(dc.build_text_payload(
            cfg.discord, "✅ Prueba de Carrefour Monitor", "Si ves este mensaje, el webhook funciona."))
        print("Mensaje enviado correctamente." if ok else "No se pudo enviar el mensaje (mira el log).")
        return 0 if ok else 1
    finally:
        await app.notifier.stop()
        await app.client.aclose()
        app.store.close()


async def inspect(cfg, url: str) -> int:
    from app import Application
    from stores.carrefour import detection as det

    app = Application(cfg)
    try:
        res = await app.client.fetch(url)
        print(f"HTTP {res.status_code} | ok={res.ok} | bloqueado={res.blocked} | {res.elapsed*1000:.0f} ms | error={res.error}")
        if not res.text:
            return 1
        print(f"URL final: {res.final_url}  | bytes: {len(res.text)}")
        if det.product_id_from_url(res.final_url or url):
            a = det.analyze_product_page(res.text, res.final_url or url, cfg.monitor.min_positive_signals)
            print(f"[FICHA] estado={a.status.value} ({a.reason})")
            print(f"  nombre={a.name!r}\n  precio={a.price} (anterior: {a.list_price})\n  vendedor={a.seller!r}")
            print(f"  sku={a.sku} ref={a.ref} ean={a.ean}\n  imagen={a.image}")
            print("  señales:", [str(s) for s in a.signals] or "ninguna")
        else:
            items = app.adapter.parse_listing(res.text, res.final_url or url)
            print(f"[LISTADO] {len(items)} productos reconocidos")
            for it in items[:30]:
                print(f"  - {it.status.value:<12} {it.price!s:<7} {it.sku}  {it.name}")
            if not items:
                print("  (ninguno: la página puede cargar los resultados con JavaScript o requerir otra URL de búsqueda)")
        out = Path("data") / "last_inspect.html"
        out.parent.mkdir(exist_ok=True)
        out.write_text(res.text, encoding="utf-8")
        print(f"HTML guardado en {out} para que puedas revisarlo / ajustar stores/carrefour/detection.py")
        return 0
    finally:
        await app.client.aclose()
        app.store.close()


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"[CONFIG] {exc}")
        return 2

    from app import Application, configure_logging

    configure_logging(cfg)
    if args.check_config:
        urls = sum(1 for t in cfg.targets if t.kind == "url")
        print(f"Configuración válida: {urls} productos URL, {len(cfg.targets) - urls} búsquedas/listados, "
              f"webhook {'configurado' if cfg.discord.webhook_url else 'NO configurado'}.")
        return 0
    try:
        if args.test_discord:
            return asyncio.run(test_discord(cfg))
        if args.inspect:
            return asyncio.run(inspect(cfg, args.inspect))
        app = Application(cfg)
        if args.once:
            asyncio.run(app.run_once())
        else:
            asyncio.run(app.run())
        return 0
    except ConfigError as exc:
        print(f"[CONFIG] {exc}")
        return 2
    except KeyboardInterrupt:
        # Ctrl+C: el finally de Application.run() ya cerró todo y mostró el mensaje final.
        return 0


if __name__ == "__main__":
    sys.exit(main())
