"""
main.py — Entrypoint del servidor Axiom.

Lanza el FastAPI app definido en axiom_backend/axiom_api.py usando uvicorn.
Funciona idénticamente en VM (`python main.py`) o en Cloud Run (que setea $PORT).

Por qué un main.py separado en vez de `uvicorn axiom_backend.axiom_api:app`:
  - Configura logging ANTES de importar la app, para capturar mensajes de
    inicialización (carga de prompts, conexión a Featherless, etc.).
  - Hace explícita la ligadura host/puerto y el reload flag.
  - Hace pre-flight checks (env vars críticas) con un mensaje claro.
  - Es un script ejecutable; no requiere recordar el path al app.

Uso
---
Desarrollo local:
    python main.py

VM en Google Cloud:
    nohup python main.py > axiom.log 2>&1 &

Cloud Run (vía Dockerfile):
    CMD ["python", "main.py"]
    # Cloud Run inyecta $PORT automáticamente.
"""

from __future__ import annotations

import logging
import os
import sys


def _setup_logging() -> None:
    """Configura logging ANTES de cargar la app.

    Llama a basicConfig solo si nadie lo configuró previamente (ej. tests).
    """
    if logging.getLogger().handlers:
        return
    logging.basicConfig(
        level=os.environ.get("AXIOM_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


def _preflight_check() -> None:
    """Valida que las env vars críticas estén presentes ANTES de lanzar uvicorn.

    Lee desde `settings` (no de `os.environ`), porque `pydantic-settings` ya
    carga el `.env` al instanciar Settings. Si leyéramos `os.environ` directo,
    fallaría cuando el .env existe pero las vars no se exportaron al shell —
    que es el caso usual al hacer `python main.py` sin `source .env`.

    Sale con código 1 y mensaje claro si falta algo crítico. Esto evita
    el caso donde el server arranca, acepta requests, y todos fallan con
    errores genéricos hasta que alguien revisa los logs.
    """
    log = logging.getLogger("axiom.preflight")

    # Import diferido: settings instancia Settings() al cargarse, lo cual
    # ejecuta la carga del .env. Lo queremos DESPUÉS de _setup_logging.
    try:
        from axiom_backend.config import settings
    except Exception as e:
        log.error("No se pudo cargar axiom_backend.config: %s", e)
        log.error("Pre-flight check FAILED. Aborting startup.")
        sys.exit(1)

    # Mapeo: nombre humano → (valor leído, mensaje de ayuda)
    required = {
        "FEATHERLESS_API_KEY": (
            settings.featherless_api_key,
            "Llamadas LLM fallarán con 401 sin esto.",
        ),
        "CONTACT_EMAIL": (
            settings.contact_email,
            "Crossref/OpenAlex requieren un email en User-Agent.",
        ),
        "AXIOM_BACKEND_API_KEY": (
            settings.axiom_backend_api_key,
            "Endpoints protegidos rechazarán todas las requests sin esto.",
        ),
    }

    missing = [name for name, (value, _) in required.items() if not value]
    if missing:
        for name in missing:
            log.error("Env var requerida ausente: %s — %s", name, required[name][1])
        log.error("Pre-flight check FAILED. Aborting startup.")
        sys.exit(1)

    log.info("Pre-flight check OK. Lanzando uvicorn...")


def main() -> None:
    _setup_logging()
    log = logging.getLogger("axiom.main")

    _preflight_check()

    # Import diferido: queremos que logging esté configurado antes de que
    # llm_router, prompts/__init__.py, y los agentes loguien al cargarse.
    import uvicorn

    # Cloud Run inyecta $PORT (típicamente 8080). En VM/dev cae al default 8080.
    port = int(os.environ.get("PORT", "8080"))
    host = os.environ.get("HOST", "0.0.0.0")

    # Reload solo en desarrollo local. En VM/Cloud Run NUNCA — reinicia
    # el worker y mata las runs en cola.
    reload = os.environ.get("AXIOM_RELOAD", "").lower() in ("1", "true", "yes")

    log.info(
        "Iniciando uvicorn en %s:%d (reload=%s)",
        host, port, reload,
    )

    uvicorn.run(
        "axiom_backend.axiom_api:app",
        host=host,
        port=port,
        reload=reload,
        # Un solo worker. axiom_api mantiene una cola asyncio in-process —
        # múltiples workers tendrían colas independientes y romperían la
        # serialización que protege Featherless (4 conexiones).
        workers=1,
        # Cloud Run y la mayoría de proxies inyectan X-Forwarded-* headers.
        proxy_headers=True,
        forwarded_allow_ips="*",
        # SSE necesita keep-alive prolongado. Por defecto uvicorn cierra
        # conexiones inactivas a los 5s — eso rompería el stream durante
        # pausas de razonamiento de DeepSeek-R1.
        timeout_keep_alive=300,
        # No emitir el log de "Uvicorn running on..." en formato propio —
        # ya tenemos nuestro logger configurado y queremos formato consistente.
        log_config=None,
    )


if __name__ == "__main__":
    main()
