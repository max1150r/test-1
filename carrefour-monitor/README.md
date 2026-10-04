# Carrefour Monitor

Monitor de disponibilidad de productos de **Carrefour España** con avisos por **webhook de Discord**.
Python 3.11+, asíncrono, modular y pensado para funcionar 24/7.

* Productos por **URL directa**, por **palabras clave** y por **URL de listado/categoría**.
* Detección de stock por varias señales independientes (JSON-LD, JSON incrustado, texto y botón de compra), con doble confirmación antes de avisar para evitar falsos positivos.
* Avisos: 🟢 stock, 🔔 producto nuevo, 💰 cambio de precio, 🔴 agotado (opcional), ⚠️ errores (con cooldown), 🚀 arranque.
* Estado persistente en SQLite: al reiniciar no repite avisos.
* Proxies opcionales (rotación, cooldown, estadísticas), reintentos con backoff, límite global de peticiones.
* No intenta evitar CAPTCHAs ni retos de seguridad: si aparecen, marca `BLOCKED`, aplica backoff y avisa.

## Qué está verificado y qué no (léelo)

Se inspeccionó la web pública de Carrefour (fichas y listados) y se confirmó que **muestran**: «Agotado temporalmente», botón «Añadir», precio «15,99 €», «Vendido por…», «REF./EAN», imagen `og:image`, enlaces `/<nombre>/VC4A-<id>/p` y paginación por `offset=24`.

**No se pudo verificar** (el código no inventa nada y lo trata como configurable): el HTML crudo exacto (nombres de clases, estructura del JSON incrustado) y la **URL de búsqueda** (`https://www.carrefour.es/?q={query}`, ajustable en `store.search_url`). Por eso la detección usa señales genéricas y todo está aislado en `stores/carrefour/detection.py`.

**Primer paso obligatorio tras instalar**: ejecuta `python main.py --inspect "<una URL de producto tuya>"` y `python main.py --inspect "https://www.carrefour.es/?q=pokemon"`. Si el estado, precio o listado salen mal, la web ha cambiado o carga con JavaScript; usa en su lugar tipo `listing` (URLs de categoría) y/o ajusta `detection.py`. Con `--inspect` se guarda `data/last_inspect.html` para revisarlo.

## Estructura

```
main.py  app.py  config.example.yaml  .env.example  requirements.txt  Dockerfile  docker-compose.yml
monitor/   models, state (SQLite), detector (cambios de estado), scheduler, product_monitor, search_monitor, filters, stats
network/   client (HTTP), proxy_manager
notifications/discord.py
stores/    base.py (StoreAdapter)  carrefour/{adapter.py, detection.py}
utils/     config, logger, text
tests/     pruebas con HTML simulado (sin red)
```

Para añadir otra tienda: crea `stores/<tienda>/adapter.py` con una clase que herede de `StoreAdapter` y úsala en `app.py`.

## Instalación en Windows (paso a paso)

1. **Instalar Python**: entra en https://www.python.org/downloads/ , descarga Python 3.12 e instálalo **marcando «Add python.exe to PATH»** en la primera pantalla.
2. **Copiar el proyecto**: descomprime la carpeta `carrefour-monitor` en, por ejemplo, `C:\carrefour-monitor`.
3. **Abrir una terminal en esa carpeta**: en el Explorador entra en la carpeta, haz clic en la barra de direcciones, escribe `cmd` y pulsa Enter.
4. **Crear el entorno virtual**: `python -m venv .venv` y actívalo: `.venv\Scripts\activate` (verás `(.venv)` al principio de la línea).
5. **Instalar dependencias**: `pip install -r requirements.txt`
6. **Crear `.env`**: `copy .env.example .env` y ábrelo con el Bloc de notas.
7. **Webhook de Discord**: en Discord, ajustes del canal → Integraciones → Webhooks → Nuevo webhook → Copiar URL. Pégala en `.env`: `DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...` (sin comillas). No compartas esa URL.
8. **Crear `config.yaml`**: `copy config.example.yaml config.yaml` y edítalo (productos, búsquedas, intervalos). Respeta los espacios del YAML.
9. **Comprobar**: `python main.py --check-config`, luego `python main.py --test-discord` (debe llegar un mensaje) y `python main.py --once` (una pasada).
10. **Arrancar**: `python main.py`. Para parar: `Ctrl+C` (verás «Monitor detenido correctamente.»).

Cada vez que abras una terminal nueva: `cd C:\carrefour-monitor`, `.venv\Scripts\activate`, `python main.py`.

### Proxies (opcional)
Copia `proxies.example.txt` a `proxies.txt` y pon un proxy por línea (`IP:PUERTO`, `USER:PASS@IP:PUERTO`, `http://...`, `https://...`, `socks5://...`). Si el fichero no existe, se usa tu conexión.

## Configuración clave (`config.yaml`)

| Clave | Para qué |
|---|---|
| `monitor.interval` / `jitter` | segundos entre comprobaciones y variación aleatoria. También `interval:` por producto/búsqueda |
| `monitor.concurrency` | peticiones simultáneas máximas |
| `monitor.max_requests_per_minute` | limitador global (anti-exceso). Al arrancar se muestra la carga estimada |
| `monitor.confirm_stock` | releer la ficha antes de avisar (recomendado) |
| `monitor.min_positive_signals` | `2` exige dos fuentes distintas para dar IN_STOCK (más estricto) |
| `discord.notify_out_of_stock` / `notify_price_changes` | avisos opcionales |
| búsquedas: `include_keywords`, `exclude_keywords`, `min_price`, `max_price`, `query_match`, `notify_existing` | filtros (ignoran mayúsculas y acentos). La primera pasada de una búsqueda solo guarda lo existente |

Intervalos cortos (p. ej. 5 s) con muchos productos generan mucho tráfico y es la causa más probable de bloqueos (`BLOCKED`/429). El límite global espacia las peticiones automáticamente; usa intervalos razonables y proxies propios si los necesitas.

## Ejecución continua

**Windows**: lo más simple es una ventana de terminal abierta. Para que arranque solo: Programador de tareas → Crear tarea → desencadenador «Al iniciar sesión» → acción: programa `C:\carrefour-monitor\.venv\Scripts\python.exe`, argumentos `main.py`, «Iniciar en» `C:\carrefour-monitor`.

**Linux / VPS (systemd)**: `/etc/systemd/system/carrefour-monitor.service`
```
[Unit]
Description=Carrefour Monitor
After=network-online.target
[Service]
WorkingDirectory=/opt/carrefour-monitor
ExecStart=/opt/carrefour-monitor/.venv/bin/python main.py
Restart=always
RestartSec=10
User=monitor
[Install]
WantedBy=multi-user.target
```
`sudo systemctl enable --now carrefour-monitor` · logs: `journalctl -u carrefour-monitor -f` o `logs/monitor.log`.

**Docker**: con `.env` y `config.yaml` creados (y un `proxies.txt`, aunque sea vacío):
`docker compose up -d --build` · `docker compose logs -f` · `docker compose down`. Datos y logs persisten en `./data` y `./logs`; se reinicia solo salvo parada manual.

## Tests

`pip install -r requirements-dev.txt` y `python -m pytest` (o `python -m unittest discover -s tests -t .`). No tocan Carrefour.

## Seguridad

El webhook y las credenciales de proxies se leen de `.env`/`proxies.txt`, se ocultan en logs y nunca se incluyen en los mensajes. La verificación TLS está activa. `.gitignore` excluye `.env`, `config.yaml`, `proxies.txt`, `data/` y `logs/`.
