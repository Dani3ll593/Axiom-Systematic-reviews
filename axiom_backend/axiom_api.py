"""
axiom_api.py — FastAPI wrapper para el pipeline de LangGraph de Axiom.
Implementa una cola secuencial para proteger el cap de conexiones de
Featherless, emisión SSE en vivo, y almacenamiento de estados en disco.
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
from src.graph import pipeline
from src.config import settings

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
    status: Literal["queued", "running", "done", "error"]
    queue_position: int
    created_at: str

class StatusResponse(BaseModel):
    run_id: str
    status: Literal["queued", "running", "done", "error"]
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
    
    try:
        # Escuchamos los chunks (modo tuplas con "updates" y "values")
        async for mode, chunk in pipeline.astream(initial_state, stream_mode=["updates", "values"]):
            if mode == "updates":
                for node_name, update in chunk.items():
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
                    # (ELIMINADO: el log genérico de "Agente finalizó su tarea")
                    
            elif mode == "values":
                final_state = chunk  # Snapshot completo del estado hasta este punto

        if final_state:
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
    if node_name == "screener":
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
    if node_name == "writer":
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
                       cochrane_mode=cochrane_raw)

    _queue.put_nowait(run_id)
    
    pos = _queue_position(run_id)
    logger.info(f"POST /pipeline/start -> run_id={run_id} cochrane={cochrane_raw} queue_position={pos}")
    
    return RunResponse(
        run_id=run_id,
        status="queued",
        queue_position=pos,
        created_at=now
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
    """Endpoint para servir el PDF del borrador APA 7 generado por writer.py."""
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