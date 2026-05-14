"""
utils/pipeline_runner.py
────────────────────────
Drives the pipeline progress stream that the Streamlit UI consumes.

Two execution modes:
  • REAL — POST /pipeline/start to the backend, then iterate the SSE
            stream from /pipeline/stream/{run_id}. The frontend is a
            thin client; all model orchestration lives in the backend.
  • MOCK — replay a deterministic event sequence so the UI demo works
            offline (used when AXIOM_MOCK=1 or backend secrets missing).
"""

from __future__ import annotations
import time
import random
from dataclasses import dataclass
from typing import Any, Iterator, Literal

import httpx

from utils.api_client import (
    is_mock_mode,
    start_pipeline,
    stream_pipeline_events,
    fetch_final_state,
)


EventType = Literal[
    "log", "agent_start", "agent_progress", "agent_done",
    "stat", "kappa", "finished", "final_state", "run_started",
]


@dataclass
class PipelineEvent:
    type: EventType
    agent: str | None = None
    progress: int | None = None
    message: str | None = None
    level: Literal["info", "success", "warn", "error"] = "info"
    payload: dict | None = None


# ─── Mock script — mirrors the React demo ───────────────────────────
_MOCK_SCRIPT: list[tuple] = [
    ("agent_start", "searcher", None, None, None),
    ("log", None, None, "Iniciando búsqueda en arXiv HTTPS...", "info"),
    ("agent_progress", "searcher", 15, None, None),
    ("log", None, None, "arXiv: 47 resultados encontrados", "success"),
    ("stat", None, None, None, {"found": 47}),
    ("agent_progress", "searcher", 35, None, None),
    ("log", None, None, "PubMed: consultando entrez API...", "info"),
    ("stat", None, None, None, {"found": 121}),
    ("agent_progress", "searcher", 55, None, None),
    ("log", None, None, "OpenAlex: paginando 3/5 queries...", "info"),
    ("stat", None, None, None, {"found": 218}),
    ("agent_progress", "searcher", 75, None, None),
    ("log", None, None, "Scielo: 4238ms p50 — timeout extendido a 20s", "warn"),
    ("stat", None, None, None, {"found": 274}),
    ("agent_progress", "searcher", 90, None, None),
    ("log", None, None, "Deduplicación por DOI + fuzzy match...", "info"),
    ("stat", None, None, None, {"found": 312, "restricted": 14}),
    ("agent_done", "searcher", 100, "Buscador completado: 312 papers · 14 restringidos", "success"),

    ("agent_start", "screener", None, None, None),
    ("log", None, None, "Screener iniciado — Qwen 7B temperatura 0 (Pase 1)...", "info"),
    ("agent_progress", "screener", 25, None, None),
    ("log", None, None, "Pase 1 completo: 203 descartados (obvios)", "info"),
    ("stat", None, None, None, {"excluded": 203}),
    ("agent_progress", "screener", 55, None, None),
    ("log", None, None, "QwQ-32B Pase 2: 22 papers dudosos en revisión...", "info"),
    ("agent_progress", "screener", 80, None, None),
    ("kappa", None, None, None, {"value": 0.81}),
    ("log", None, None, "Inter-rater κ = 0.81 — umbral aceptado (>0.70)", "success"),
    ("agent_done", "screener", 100, "Screener completado: 87 incluidos · 225 excluidos", "success"),
    ("stat", None, None, None, {"included": 87, "excluded": 225}),

    ("agent_start", "extractor", None, None, None),
    ("log", None, None, "Extractor: PyMuPDF parsing (path primario)...", "info"),
    ("agent_progress", "extractor", 30, None, None),
    ("log", None, None, "PDF parsing: 60ms/paper avg — 28/87 completos", "info"),
    ("agent_progress", "extractor", 65, None, None),
    ("log", None, None, "outlines: JSON schema restringido — 0% malformado", "success"),
    ("agent_progress", "extractor", 85, None, None),
    ("log", None, None, "Confidence scores asignados — avg 0.79", "info"),
    ("agent_done", "extractor", 100, "Extractor: 87 papers extraídos con fragmento_fuente", "success"),

    ("agent_start", "analyst", None, None, None),
    ("log", None, None, "Analista: BGE-M3 embeddings (multilingüe)...", "info"),
    ("agent_progress", "analyst", 40, None, None),
    ("log", None, None, "ChromaDB: 312 vectores indexados", "info"),
    ("agent_progress", "analyst", 75, None, None),
    ("log", None, None, "QwQ-32B <think>: detección de contradicciones...", "info"),
    ("agent_done", "analyst", 100, "Mapa consenso/controversia generado: 3 clústeres", "success"),

    ("agent_start", "gap_finder", None, None, None),
    ("log", None, None, "Gap Finder: 5 categorías — verificación secundaria...", "info"),
    ("agent_progress", "gap_finder", 50, None, None),
    ("log", None, None, "OpenAlex secondary check: 2 gaps rechazados (ya cubiertos)", "warn"),
    ("agent_done", "gap_finder", 100, "Gap Finder: 4 vacíos confirmados reportables", "success"),

    ("agent_start", "writer", None, None, None),
    ("log", None, None, "Redactor QwQ-32B: iniciando síntesis narrativa...", "info"),
    ("agent_progress", "writer", 40, None, None),
    ("log", None, None, "Generando reporte ejecutivo en Markdown...", "info"),
    ("agent_progress", "writer", 75, None, None),
    ("log", None, None, "Borrador APA 7 — referencias formateadas", "info"),
    ("agent_done", "writer", 100, "Pipeline completado exitosamente.", "success"),

    ("finished", None, None, None, None),
]


def _mock_events() -> Iterator[PipelineEvent]:
    for t, agent, prog, msg, payload in _MOCK_SCRIPT:
        time.sleep(0.7 + random.random() * 0.6)
        level = "info"
        if t == "log" and isinstance(payload, str):
            level = payload
            payload = None
        elif t == "log" and len(_MOCK_SCRIPT[0]) >= 5:
            # Some tuples encode level via the 5th positional slot directly
            pass
        # The script uses the 5th positional slot as either a level (for "log"
        # rows) or a structured payload (for "stat" / "kappa" rows). Detect:
        if t == "agent_done" and isinstance(payload, str):
            level = payload
            payload = None
        yield PipelineEvent(
            type=t, agent=agent, progress=prog,
            message=msg or "", level=level, payload=payload if isinstance(payload, dict) else None,
        )


def _real_events(state_payload: dict[str, Any]) -> Iterator[PipelineEvent]:
    """Drive the real backend over HTTP + SSE.

    1. POST /pipeline/start → run_id
    2. GET  /pipeline/stream/{run_id} (SSE) → live events
    3. On 'finished', GET /pipeline/result/{run_id} → final state, yielded
       as a synthetic PipelineEvent(type='final_state').
    """
    try:
        yield PipelineEvent(type="log", message="Conectando con Axiom backend (MI300X)...", level="info")
        run_id = start_pipeline(state_payload)
        # Surface the run_id to the UI layer so it can stash it in session_state
        # (needed for the PDF download endpoint after the pipeline finishes).
        yield PipelineEvent(type="run_started", payload={"run_id": run_id})
        yield PipelineEvent(type="log", message=f"Run iniciado · id={run_id[:8]}…", level="success")

        finished = False
        for raw in stream_pipeline_events(run_id):
            ev = PipelineEvent(
                type=raw.get("type"),
                # FIX: Aceptamos 'node' o 'agent' para que coincida con el backend
                agent=raw.get("agent") or raw.get("node"), 
                message=raw.get("message", ""),
                level=raw.get("level", "info"),
                payload=raw.get("payload"),
            )
            yield ev
            if ev.type == "finished":
                finished = True
                break

        if finished:
            try:
                final = fetch_final_state(run_id)
                yield PipelineEvent(type="final_state", payload=final)
            except httpx.HTTPError as e:
                yield PipelineEvent(
                    type="log",
                    level="error",
                    message=f"No se pudo recuperar el estado final: {e}",
                )

    except httpx.HTTPError as e:
        yield PipelineEvent(
            type="log", level="error",
            message=f"Error de conexión con backend: {e}",
        )
    except RuntimeError as e:
        yield PipelineEvent(type="log", level="error", message=str(e))


def run_pipeline_events(state_payload: dict[str, Any], run_id: str = None) -> Iterator[PipelineEvent]:
    if is_mock_mode():
        yield from _mock_events()
        return

    # Si no tenemos run_id, lo pedimos al backend
    if not run_id:
        try:
            run_id = start_pipeline(state_payload)
            yield PipelineEvent(type="run_started", payload={"run_id": run_id})
        except Exception as e:
            yield PipelineEvent(type="log", level="error", message=f"Error al iniciar: {e}")
            return

    # Conectamos al stream (esto siempre reproduce desde el inicio del archivo en el backend)
    try:
        finished = False
        for raw in stream_pipeline_events(run_id):
            ev = PipelineEvent(
                type=raw.get("type", "log"),
                agent=raw.get("node") or raw.get("agent"),
                message=raw.get("message", ""),
                level=raw.get("level", "info"),
                payload=raw.get("payload"),
            )
            yield ev
            if ev.type == "finished":
                finished = True
                break

        if finished:
            final = fetch_final_state(run_id)
            yield PipelineEvent(type="final_state", payload=final)

    except Exception as e:
        yield PipelineEvent(type="log", level="error", message=f"Conexión perdida: {e}")
