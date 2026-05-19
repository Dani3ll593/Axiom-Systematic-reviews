"""
axiom_api.py — FastAPI wrapper para el pipeline de LangGraph de Axiom.
Implementa una cola secuencial para proteger el cap de conexiones de
Featherless, emisión SSE en vivo, almacenamiento de estados en disco, y
cancelación cooperativa de runs en curso.
"""

import asyncio
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Literal, Dict, Any

from fastapi import FastAPI, Header, HTTPException, Request, Depends
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
from pydantic import BaseModel

# Importamos el grafo real y la configuración local
from axiom_backend.graph import pipeline
from axiom_backend.config import settings

# ─── Configuración y Constantes ───────────────────────────────────────
RUNS_DIR = Path("data/api_runs")
SSE_HEARTBEAT_S = 15.0

NODE_UI_LABELS = {
    "searcher": "Buscador",
    "screener": "Screener",
    "extractor": "Extractor",
    "rob_assessor": "Risk of Bias",   # Cochrane only (opcional)
    "clusterer": None,                # Interno
    "analyst_7b": "Analista",
    "analyst_32b": "Analista",
    "reconciler": None,               # Interno
    "grade_profiler": "GRADE",        # Cochrane only (opcional)
    "gapfinder": "Gap Finder",
    "writer": "Redactor"
}

# Configuración de Logging
# NOTE: main.py ya configura logging antes de importar este módulo, así que
# este basicConfig es no-op en producción. Lo mantenemos para que `uvicorn
# axiom_api:app` directo (sin main.py) siga funcionando para tests rápidos.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] axiom_api.%(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("axiom_api")


# ─── Estado Global ───────────────────────────────────────────────────
# Cap leído de settings.max_queue_size (default 10, configurable en .env).
_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=settings.max_queue_size)
_current_run: str | None = None
_queue_lock = asyncio.Lock()
# Diccionario para mapear run_id -> listas de colas (subscriptores SSE)
_event_subscribers: Dict[str, list[asyncio.Queue]] = {}

# ─── Cancelación cooperativa ─────────────────────────────────────────
# Set de run_ids marcados para cancelar. El worker chequea esta variable
# entre chunks del astream del grafo. Cuando un run_id está acá, el worker
# rompe el loop después de que termine el chunk actual (cooperativo: no
# interrumpe la llamada HTTP al LLM en curso, deja que termine).
#
# Por qué cooperativo y no asyncio.Task.cancel():
#   - .cancel() dispara CancelledError en el próximo await, lo cual podría
#     ocurrir en medio de una escritura a disco o un emit de SSE → estado
#     inconsistente en meta.json.
#   - El flag chequeado entre chunks da una garantía clara: "el nodo en
#     curso termina, ningún nodo posterior arranca". Es lo que documenta
#     el frontend al usuario.
_cancelled_runs: set[str] = set()


# ─── Ciclo de vida (Lifespan) ────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    worker_task = asyncio.create_task(_worker())
    logger.info("Axiom API Worker iniciado.")
    yield
    worker_task.cancel()
    logger.info("Axiom API Worker detenido.")

app = FastAPI(title="Axiom API", version="1.0", lifespan=lifespan)


# ─── Dependencia de Autenticación ────────────────────────────────────
def require_bearer(authorization: str = Header(None)) -> None:
    """Validación Bearer token. Lee el secreto de settings (que lo carga del .env).

    NOTE: settings.axiom_backend_api_key es Optional[str] — si está vacío
    devolvemos 500 (Server misconfigured) en vez de aceptar cualquier
    request. Mejor fallar visible que aceptar tráfico sin auth.
    """
    expected = settings.axiom_backend_api_key
    if not expected:
        logger.error("AXIOM_BACKEND_API_KEY no configurada en settings/.env.")
        raise HTTPException(500, "Server misconfigured: AXIOM_BACKEND_API_KEY not set")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing Bearer token")
    if authorization[7:] != expected:
        raise HTTPException(401, "Invalid token")


# ─── Modelos Pydantic ────────────────────────────────────────────────
class RunResponse(BaseModel):
    run_id: str
    status: Literal["queued", "running", "done", "error", "cancelled"]
    queue_position: int
    created_at: str

class StatusResponse(BaseModel):
    run_id: str
    status: Literal["queued", "running", "done", "error", "cancelled"]
    queue_position: int
    current_node: str | None
    last_event_id: int
    created_at: str
    started_at: str | None
    ended_at: str | None


# ─── Utilidades ──────────────────────────────────────────────────────
async def _update_meta(run_id: str, **kwargs):
    meta_path = RUNS_DIR / run_id / "meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    else:
        meta = {}
    meta.update(kwargs)
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

def _queue_position(run_id: str) -> int:
    """0 = currently running, 1 = next, 2+ = waiting, -1 = not in queue/done."""
    if _current_run == run_id:
        return 0
    pending = list(_queue._queue)
    try:
        return pending.index(run_id) + 1
    except ValueError:
        return -1

async def _next_event_id(run_id: str) -> int:
    events_file = RUNS_DIR / run_id / "events.jsonl"
    if not events_file.exists():
        return 1
    with events_file.open("r", encoding="utf-8") as f:
        count = sum(1 for _ in f)
    return count + 1


# ─── Emisor de Eventos SSE ───────────────────────────────────────────
async def _emit_event(run_id: str, type: str, node: str | None = None,
                      payload: dict | None = None, level: str = "info",
                      message: str | None = None):
    run_dir = RUNS_DIR / run_id
    events_file = run_dir / "events.jsonl"

    event_id = await _next_event_id(run_id)

    event = {
        "type": type,
        "event_id": event_id,
        "node": node,
        "message": message,
        "payload": payload or {},
        "timestamp": time.time(),
        "level": level,
    }

    # Escribir a disco (Durabilidad)
    with events_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")

    # Actualizar last_event_id en meta
    await _update_meta(run_id, last_event_id=event_id)

    # Notificar a subscriptores SSE en vivo (Memoria)
    for q in _event_subscribers.get(run_id, []):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass

def _format_sse(event: dict) -> str:
    return (
        f"event: {event['type']}\n"
        f"id: {event['event_id']}\n"
        f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
    )

class SSELogHandler(logging.Handler):
    """Captura los logs internos de los agentes y los envía al frontend."""
    def __init__(self, run_id: str):
        super().__init__()
        self.run_id = run_id

    def emit(self, record: logging.LogRecord):
        # Ignorar mensajes de debug para no saturar la UI
        if record.levelno < logging.INFO:
            return

        msg = record.getMessage()
        level = "error" if record.levelno >= logging.ERROR else ("warn" if record.levelno == logging.WARNING else "info")

        # Enviar el evento SSE de forma asíncrona
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_emit_event(self.run_id, type="log", message=msg, level=level))
        except Exception:
            pass


# ─── Ejecutor del Pipeline (Worker) ──────────────────────────────────
async def _worker():
    """Toma jobs de la cola secuencialmente."""
    global _current_run
    while True:
        run_id = await _queue.get()
        async with _queue_lock:
            _current_run = run_id

        logger.info(f"Worker iniciando run_id: {run_id}")
        try:
            await _execute_run(run_id)
        except Exception as e:
            logger.exception(f"Run {run_id} falló de forma crítica: {e}")
            await _emit_event(run_id, type="finished", payload={"error": str(e)}, level="error")
            await _update_meta(run_id, status="error", error=str(e), ended_at=datetime.now(timezone.utc).isoformat())
        finally:
            async with _queue_lock:
                _current_run = None
            # Limpiar la entrada de cancelados — el run terminó (sea por
            # cancelación o por completarse). Si quedara, un futuro run con
            # el mismo run_id (improbable pero posible con UUIDs cortos)
            # arrancaría ya cancelado.
            _cancelled_runs.discard(run_id)
            _queue.task_done()


async def _execute_run(run_id: str):
    run_dir = RUNS_DIR / run_id
    initial_state = json.loads((run_dir / "initial_state.json").read_text(encoding="utf-8"))

    await _update_meta(run_id, status="running", started_at=datetime.now(timezone.utc).isoformat())
    await _emit_event(run_id, type="agent_start", node=None, payload={
        "sr_id": initial_state.get("sr_id"),
        "cochrane_mode": bool(initial_state.get("cochrane_mode", False)),
    })

    # --- 1. Conectar el interceptor de logs a los agentes ---
    sse_handler = SSELogHandler(run_id)
    src_logger = logging.getLogger("src") # Captura src.agents y src.tools
    src_logger.addHandler(sse_handler)

    started = time.time()
    final_state: dict | None = None
    cancelled = False
    last_node: str | None = None

    try:
        # Escuchamos los chunks (modo tuplas con "updates" y "values")
        async for mode, chunk in pipeline.astream(initial_state, stream_mode=["updates", "values"]):
            # ─── Check de cancelación cooperativa ───────────────────
            # Lo chequeamos al inicio de cada iteración: si el endpoint
            # /cancel marcó este run_id, salimos del loop antes de procesar
            # el chunk. Cualquier nodo que estaba corriendo cuando vino el
            # cancel ya terminó (porque astream solo yieldea cuando un nodo
            # completa) — el siguiente NO arranca.
            if run_id in _cancelled_runs:
                cancelled = True
                logger.info(f"Run {run_id} cancelado cooperativamente (último nodo: {last_node})")
                break

            if mode == "updates":
                for node_name, update in chunk.items():
                    last_node = node_name
                    await _update_meta(run_id, current_node=node_name)

                    # Derivar stats del nodo para la UI
                    stats = _derive_stats(node_name, update)

                    await _emit_event(
                        run_id,
                        type="agent_done",
                        node=node_name,
                        payload={
                            "ui_label": NODE_UI_LABELS.get(node_name),
                            "is_internal": node_name in {"clusterer", "reconciler"},
                            "stats": stats,
                        },
                        level="success"
                    )

            elif mode == "values":
                final_state = chunk  # Snapshot completo del estado hasta este punto

        # ─── Cierre: cancelado vs. completado ───────────────────────
        if cancelled:
            # Persistir lo que tengamos hasta ahora (puede servir para auditoría).
            if final_state:
                (run_dir / "final_state.json").write_text(
                    json.dumps(final_state, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            await _update_meta(
                run_id,
                status="cancelled",
                ended_at=datetime.now(timezone.utc).isoformat(),
                last_node=last_node,
            )
            await _emit_event(
                run_id,
                type="cancelled",
                payload={"run_id": run_id, "last_node": last_node,
                         "duration_s": round(time.time() - started, 1)},
                level="warn",
            )
            # Emitimos también `finished` para que los consumidores SSE
            # genéricos (que cierran al ver finished/error) no se queden
            # colgados — usamos level=warn para distinguirlo de éxito.
            await _emit_event(
                run_id,
                type="finished",
                payload={"cancelled": True, "last_node": last_node},
                level="warn",
            )
            logger.info(f"Run {run_id} marcado como cancelado tras {round(time.time() - started, 1)}s")

        elif final_state:
            (run_dir / "final_state.json").write_text(json.dumps(final_state, ensure_ascii=False, indent=2), encoding="utf-8")
            await _update_meta(run_id, status="done", ended_at=datetime.now(timezone.utc).isoformat())

            await _emit_event(run_id, type="finished", payload={
                "sr_id": final_state.get("sr_id", run_id),
                "duration_s": round(time.time() - started, 1),
            }, level="success")
            logger.info(f"Run {run_id} finalizado exitosamente en {round(time.time() - started, 1)}s")

    finally:
        # --- 2. Desconectar el interceptor al terminar (Evita memoria zombie) ---
        src_logger.removeHandler(sse_handler)


def _derive_stats(node_name: str, update: dict) -> dict:
    if node_name == "searcher":
        return {"papers_found": len(update.get("papers_found", []))}
    # El grafo emite dos nodos screener (cascada 7B → 32B). Ambos escriben
    # al mismo state field, así que cada uno reporta los conteos acumulados.
    # El UI los colapsa en una sola fila ("screener" en NODE_TO_UI) y mergea
    # los stats; cuando llega screener_32b, los conteos finales sobrescriben
    # los del 7B. Si solo corrió el 7B (sin dudosos para escalar), también
    # quedan reflejados los conteos correctamente.
    if node_name in ("screener", "screener_7b", "screener_32b"):
        return {
            "included":  len(update.get("screened_papers", [])),
            "excluded":  len(update.get("papers_excluded", [])),
        }
    if node_name == "extractor":
        return {"extractions": len(update.get("extractions", []))}
    if node_name == "rob_assessor":
        # Cochrane mode only. Si no corrió, el update es {} y devuelve 0.
        return {"papers_assessed": len(update.get("rob_assessments", []))}
    if node_name == "clusterer":
        clusters = update.get("clusters", [])
        return {"n_clusters": len(clusters), "sizes": [len(c) for c in clusters]}
    if node_name in ("analyst_7b", "analyst_32b"):
        key = "synthesis_7b" if node_name == "analyst_7b" else "synthesis_32b"
        return {"clusters_analyzed": len(update.get(key, []))}
    if node_name == "reconciler":
        return {"consensus_clusters": len(update.get("consensus_clusters", []))}
    if node_name == "grade_profiler":
        # Cuenta cuántos clusters efectivamente recibieron GRADE (vs "not_assessed").
        clusters = update.get("consensus_clusters", [])
        n_graded = sum(
            1 for c in clusters
            if c.get("grade_final_certainty") not in (None, "not_assessed")
        )
        return {"clusters_graded": n_graded, "clusters_total": len(clusters)}
    if node_name == "gapfinder":
        return {"gaps": len(update.get("research_gaps", []))}
    # El writer ahora son 6 sub-nodos secuenciales que escriben distintas
    # keys del state. La UI los colapsa en una sola fila y muestra "Reporte
    # ensamblado" cuando termina el assembler. Para los nodos intermedios
    # devolvemos stats incrementales pero el frontend los ignora (no hay
    # texto i18n específico, solo el "Reporte ensamblado" final).
    if node_name in (
        "writer", "writer_synthesis", "writer_discussion",
        "writer_limitations", "writer_tables", "writer_references",
        "writer_assembler",
    ):
        return {
            "report_length": len(update.get("executive_report_md") or ""),
            "apa7_length":   len(update.get("apa7_literature_review") or ""),
        }
    return {}


# ─── Adaptador: AxiomState interno → contrato del frontend ───────────
def _adapt_final_state_for_ui(final_state: dict) -> dict:
    """Traduce el AxiomState interno al shape documentado en README §Final state.

    El frontend (Results screen) consume `report_md`, `apa_draft`, `gaps`,
    `restricted_papers`, `stats`, `kappa` — nombres que NO coinciden 1:1 con
    los del state interno (`executive_report_md`, `apa7_literature_review`,
    `research_gaps`, etc.). Mantenemos esta capa acá para que el frontend siga
    siendo un thin client y no tenga que conocer los nombres internos del grafo.

    `restricted_papers` y `stats` no son campos del state — los derivamos:
      • restricted = screened_papers filtrados por is_open=False
      • stats      = conteos a partir de papers_found/screened/excluded

    Passthroughs (`sr_id`, `executive_report_pdf_path`, `apa7_pdf_path`) los
    necesita el frontend para construir los download links de los PDFs.

    Cochrane: si el run corrió en modo Cochrane, exponemos `rob_assessments`
    y la certeza GRADE por cluster (ya está embebida en consensus_clusters).
    """
    screened = final_state.get("screened_papers") or []
    restricted = [
        {
            "paper_id":          p.get("paper_id"),
            "title":             p.get("title"),
            "doi":               p.get("doi"),
            "journal":           p.get("journal") or p.get("source"),
            "oa_url":            p.get("oa_url"),
            "access_confidence": p.get("access_confidence"),
        }
        for p in screened if not p.get("is_open")
    ]
    return {
        # Campos del contrato (README §Final state)
        "report_md":         final_state.get("executive_report_md") or "",
        "apa_draft":         final_state.get("apa7_literature_review") or "",
        "gaps":              final_state.get("research_gaps") or [],
        "restricted_papers": restricted,
        "stats": {
            "found":      len(final_state.get("papers_found") or []),
            "included":   len(screened),
            "excluded":   len(final_state.get("papers_excluded") or []),
            "restricted": len(restricted),
        },
        # Aún ningún nodo lo escribe en el state — pasa por si lo agregamos luego.
        "kappa": final_state.get("kappa"),
        # Passthroughs que el frontend lee directo
        "sr_id":                     final_state.get("sr_id"),
        "executive_report_pdf_path": final_state.get("executive_report_pdf_path"),
        "apa7_pdf_path":             final_state.get("apa7_pdf_path"),
        # Cochrane-only — vacío si no se corrió el modo. El frontend
        # los oculta condicionalmente según cochrane_mode del request original.
        "cochrane_mode":   bool(final_state.get("cochrane_mode", False)),
        "rob_assessments": final_state.get("rob_assessments") or [],
        # consensus_clusters tiene los campos grade_* embebidos cuando hubo
        # Cochrane; lo exponemos para que el frontend renderee la SoF table.
        "consensus_clusters": final_state.get("consensus_clusters") or [],
    }


# ─── Endpoints de la API ─────────────────────────────────────────────

@app.post("/pipeline/start", response_model=RunResponse)
async def submit_run(request: Request, _: None = Depends(require_bearer)):
    try:
        initial_state = await request.json()
    except Exception:
        raise HTTPException(422, "Invalid JSON body")

    # Validaciones básicas
    if not isinstance(initial_state, dict):
        raise HTTPException(422, "initial_state must be a JSON object")
    if not initial_state.get("question"):
        raise HTTPException(422, "Missing required field: question")
    if not initial_state.get("prisma_criteria"):
        raise HTTPException(422, "Missing required field: prisma_criteria")

    # cochrane_mode es opcional. Default False = modo PRISMA puro (fast).
    # Si está presente, validamos que sea bool. El kill-switch global
    # (settings.cochrane_mode_enabled) lo aplica el grafo, no acá — así el
    # log del run preserva la INTENCIÓN del usuario aunque el server lo salte.
    cochrane_raw = initial_state.get("cochrane_mode", False)
    if not isinstance(cochrane_raw, bool):
        raise HTTPException(422, "cochrane_mode must be a boolean")
    initial_state["cochrane_mode"] = cochrane_raw

    # output_language es opcional. Default "auto" → backend autodetect.
    # Si viene presente, debe ser uno de {"auto", "English", "Spanish"} (los
    # idiomas soportados hoy; ampliable cuando agreguemos PT/FR). Frontend
    # nunca debería mandar otra cosa, pero validamos defensivamente para no
    # propagar valores raros al state del grafo.
    lang_raw = initial_state.get("output_language", "auto")
    if not isinstance(lang_raw, str):
        raise HTTPException(422, "output_language must be a string")
    lang_normalized = lang_raw.strip() or "auto"
    if lang_normalized not in ("auto", "English", "Spanish"):
        raise HTTPException(
            422,
            f"output_language must be one of 'auto', 'English', 'Spanish' "
            f"(got {lang_raw!r})",
        )
    initial_state["output_language"] = lang_normalized

    body_bytes = await request.body()
    if len(body_bytes) > 100_000:
        raise HTTPException(413, "Payload too large")

    if _queue.full():
        raise HTTPException(503, "Queue is full. Try again later.")

    run_id = uuid.uuid4().hex[:8]
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc).isoformat()

    (run_dir / "initial_state.json").write_text(json.dumps(initial_state, ensure_ascii=False), encoding="utf-8")
    (run_dir / "events.jsonl").touch()
    await _update_meta(run_id, status="queued", created_at=now, last_event_id=0,
                       cochrane_mode=cochrane_raw, output_language=lang_normalized)

    _queue.put_nowait(run_id)

    pos = _queue_position(run_id)
    logger.info(
        f"POST /pipeline/start -> run_id={run_id} "
        f"cochrane={cochrane_raw} lang={lang_normalized} queue_position={pos}"
    )

    return RunResponse(
        run_id=run_id,
        status="queued",
        queue_position=pos,
        created_at=now
    )


@app.post("/pipeline/{run_id}/cancel")
async def cancel_run(run_id: str, _: None = Depends(require_bearer)):
    """Cancelación cooperativa de un run en curso o encolado.

    Comportamiento:
      • Si el run no existe                       → 404
      • Si el run ya terminó (done/error/cancelled) → 409
      • Si el run está corriendo o encolado       → 202 {status: "cancelling"}

    Cómo funciona internamente:
      1. Agrega run_id a `_cancelled_runs`.
      2. Si el run está encolado: lo dejamos pasar; cuando el worker lo
         saque, en la primera iteración del astream el flag lo aborta antes
         de procesar nada y se marca como cancelled.
      3. Si el run está corriendo: el worker chequea el flag entre chunks
         del astream. El nodo en curso termina su llamada actual al LLM
         (no se puede interrumpir HTTP mid-flight), el siguiente NO arranca.

    La latencia entre POST /cancel y el evento SSE `cancelled` depende del
    tiempo que tarde el nodo en curso en completar — típicamente segundos
    para searcher/screener, hasta ~3min para nodos LLM pesados como
    rob_assessor o grade_profiler. El frontend muestra "Cancelando…"
    durante este intervalo.
    """
    run_dir = RUNS_DIR / run_id
    if not run_dir.exists():
        raise HTTPException(404, "run_id not found")

    meta_path = run_dir / "meta.json"
    if not meta_path.exists():
        raise HTTPException(404, "run metadata missing")

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    current_status = meta.get("status")

    # Idempotencia: si ya está cancelado, devolvemos 200 con el mismo body
    # que el endpoint emite normalmente — el cliente puede reintentar.
    if current_status == "cancelled":
        return JSONResponse(
            status_code=200,
            content={"status": "cancelled", "run_id": run_id},
        )

    # Estados terminales: no se puede cancelar lo que ya terminó.
    if current_status in ("done", "error"):
        raise HTTPException(409, f"Run already in terminal state: {current_status}")

    _cancelled_runs.add(run_id)
    logger.info(f"POST /pipeline/{run_id}/cancel -> queued cooperative cancel (status was {current_status})")

    # Para runs encolados (aún no arrancaron) emitimos un evento ahora —
    # el worker lo verá igualmente cuando lo saque de la cola, pero un
    # cliente conectado al SSE estream debería ver la respuesta inmediata.
    if current_status == "queued":
        await _emit_event(
            run_id,
            type="log",
            message="Cancelación recibida antes de iniciar el run.",
            level="warn",
        )

    return JSONResponse(
        status_code=202,
        content={"status": "cancelling", "run_id": run_id},
    )


@app.get("/pipeline/stream/{run_id}")
async def stream_events(run_id: str, since_event_id: int = 0, _: None = Depends(require_bearer)):
    run_dir = RUNS_DIR / run_id
    if not run_dir.exists():
        raise HTTPException(404, "Run ID not found")

    async def event_generator():
        # 1. Replay histórico desde disco
        events_file = run_dir / "events.jsonl"
        run_finished = False

        if events_file.exists():
            for line in events_file.read_text(encoding="utf-8").splitlines():
                if not line.strip(): continue
                event = json.loads(line)
                if event["event_id"] > since_event_id:
                    yield _format_sse(event)
                if event["type"] in ("finished", "error"):
                    run_finished = True

        if run_finished:
            return  # Cortamos la conexión si ya terminó

        # 2. Subscripción a nuevos eventos (en memoria)
        sub_queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        _event_subscribers.setdefault(run_id, []).append(sub_queue)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(sub_queue.get(), timeout=SSE_HEARTBEAT_S)
                    yield _format_sse(event)
                    if event["type"] in ("finished", "error"):
                        return
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            if sub_queue in _event_subscribers.get(run_id, []):
                _event_subscribers[run_id].remove(sub_queue)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/pipeline/result/{run_id}")
async def fetch_result(run_id: str, _: None = Depends(require_bearer)):
    run_dir = RUNS_DIR / run_id
    if not run_dir.exists():
        raise HTTPException(404, "Run ID not found")

    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))

    if meta.get("status") == "error":
        raise HTTPException(500, detail={"status": "error", "error": meta.get("error")})

    if meta.get("status") == "cancelled":
        # Cancelled runs no tienen final_state completo — devolvemos 409
        # con info de hasta qué nodo llegamos para que el cliente decida.
        return JSONResponse(
            status_code=409,
            content={"status": "cancelled", "last_node": meta.get("last_node")},
        )

    if meta.get("status") != "done":
        return JSONResponse(status_code=202, content={"status": meta.get("status")})

    final_state_path = run_dir / "final_state.json"
    if not final_state_path.exists():
        raise HTTPException(404, "Final state not found despite status done")

    final_state = json.loads(final_state_path.read_text(encoding="utf-8"))
    return _adapt_final_state_for_ui(final_state)


@app.get("/pipeline/{run_id}/status", response_model=StatusResponse)
async def fetch_status(run_id: str, _: None = Depends(require_bearer)):
    run_dir = RUNS_DIR / run_id
    if not run_dir.exists():
        raise HTTPException(404, "Run ID not found")

    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))

    return StatusResponse(
        run_id=run_id,
        status=meta.get("status", "queued"),
        queue_position=_queue_position(run_id),
        current_node=meta.get("current_node"),
        last_event_id=meta.get("last_event_id", 0),
        created_at=meta.get("created_at", ""),
        started_at=meta.get("started_at"),
        ended_at=meta.get("ended_at")
    )


@app.get("/pipeline/{run_id}/report.pdf")
async def fetch_report_pdf(run_id: str, _: None = Depends(require_bearer)):
    """Addendum: Endpoint para servir el PDF generado."""
    run_dir = RUNS_DIR / run_id
    if not run_dir.exists():
        raise HTTPException(404, "run_id not found")

    meta_path = run_dir / "meta.json"
    if not meta_path.exists():
        raise HTTPException(404, "run metadata missing")

    meta = json.loads(meta_path.read_text(encoding="utf-8"))

    if meta.get("status") != "done":
        return JSONResponse(status_code=202, content={"status": meta.get("status", "unknown")})

    final_state_path = run_dir / "final_state.json"
    if not final_state_path.exists():
        raise HTTPException(404, "final_state not found")

    final_state = json.loads(final_state_path.read_text(encoding="utf-8"))
    pdf_path_str = final_state.get("executive_report_pdf_path")

    if not pdf_path_str:
        raise HTTPException(404, "pdf_not_available")

    pdf_path = Path(pdf_path_str)
    if not pdf_path.exists():
        raise HTTPException(404, "pdf_file_missing_on_disk")

    sr_id = final_state.get("sr_id", run_id)
    return FileResponse(
        path=str(pdf_path),
        media_type="application/pdf",
        filename=f"Axiom_Report_{sr_id}.pdf",
    )


@app.get("/pipeline/{run_id}/apa7.pdf")
async def fetch_apa7_pdf_endpoint(run_id: str, _: None = Depends(require_bearer)):
    """Endpoint para servir el PDF del borrador APA 7.

    DEPRECATED: tras la refactorización del writer a un solo PDF unificado
    (`executive_report_pdf_path`), `apa7_pdf_path` ya no se setea — este
    endpoint siempre devuelve 404 en runs nuevos. Lo mantenemos para no
    romper clientes que aún lo llaman.
    """
    run_dir = RUNS_DIR / run_id
    if not run_dir.exists():
        raise HTTPException(404, "run_id not found")

    meta_path = run_dir / "meta.json"
    if not meta_path.exists():
        raise HTTPException(404, "run metadata missing")

    meta = json.loads(meta_path.read_text(encoding="utf-8"))

    if meta.get("status") != "done":
        return JSONResponse(status_code=202, content={"status": meta.get("status", "unknown")})

    final_state_path = run_dir / "final_state.json"
    if not final_state_path.exists():
        raise HTTPException(404, "final_state not found")

    final_state = json.loads(final_state_path.read_text(encoding="utf-8"))

    # ⚠️ Clave exacta que emite writer.py
    pdf_path_str = final_state.get("apa7_pdf_path")

    if not pdf_path_str:
        raise HTTPException(404, "pdf_not_available")

    pdf_path = Path(pdf_path_str)
    if not pdf_path.exists():
        raise HTTPException(404, "pdf_file_missing_on_disk")

    sr_id = final_state.get("sr_id", run_id)
    return FileResponse(
        path=str(pdf_path),
        media_type="application/pdf",
        filename=f"Axiom_APA7_{sr_id}.pdf",
    )


@app.get("/healthz")
async def healthz():
    """Health check endpoint. No auth required.

    Devuelve estado del proceso (workers, queue) y configuración LLM declarada.
    NO pinga Featherless porque /healthz lo llama el orquestador (Cloud Run,
    systemd, load balancer) cada pocos segundos — un ping externo aquí
    multiplicaría el rate-limit consumption sin valor.

    Para verificar conectividad LLM real, usar `smoke_test.sh` o el
    endpoint /pipeline/start con una query trivial.
    """
    return {
        "status": "ok",
        "queue_depth": _queue.qsize(),
        "queue_max":   settings.max_queue_size,
        "cancelling_runs": len(_cancelled_runs),
        "llm_provider": "featherless",
        "llm_base_url": settings.featherless_base_url,
        "models": {
            "7b":     settings.model_7b_name,
            "32b":    settings.model_32b_name,
            "writer": settings.model_writer_name,
            "light":  settings.model_light_reasoning_name,
        },
        "cochrane_mode_enabled": settings.cochrane_mode_enabled,
    }