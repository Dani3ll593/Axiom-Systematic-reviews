from langgraph.graph import StateGraph, START, END
from typing import Literal

from axiom_backend.state import AxiomState
from axiom_backend.config import settings

from axiom_backend.agents.searcher import run_searcher
from axiom_backend.agents.screener import screener_7b_node, screener_32b_node
from axiom_backend.agents.extractor import run_extractor
from axiom_backend.tools.clusterer import clusterer_node
from axiom_backend.agents.analyst_7b import analyst_7b_node
from axiom_backend.agents.analyst_32b import analyst_32b_node
from axiom_backend.tools.reconciler import reconciler_node
from axiom_backend.agents.gap_finder import run_gap_finder
from axiom_backend.agents.writer import (
    writer_synthesis_node,
    writer_discussion_node,
    writer_limitations_node,
    writer_tables_node,
    writer_references_node,
    writer_assembler_node,
)

# Cochrane nodes (solo se invocan si state["cochrane_mode"] AND settings.cochrane_mode_enabled)
from axiom_backend.agents.rob_assessor import run_rob_assessor
from axiom_backend.agents.grade_profiler import run_grade_profiler


# ==============================================================================
# LÓGICA DE CONDICIONES (Ruteo Dinámico)
# ==============================================================================
def check_screening_results(state: AxiomState) -> Literal["extractor", "writer_synthesis"]:
    """Si el screener rechaza TODOS los papers, saltamos directo al writer para informar.

    El destino "writer_synthesis" es el primer nodo de la cadena del writer
    bifásico (synthesis → tables → references → assembler). Saltarse el
    extractor implica saltarse también análisis/cochrane/gap_finder, así que
    el writer recibe state vacío en clusters/gaps — el prompt y los nodos
    Python lo manejan emitiendo prosa breve "no incluidos relevantes".
    """
    if not state.get("screened_papers"):
        return "writer_synthesis"
    return "extractor"


def _should_run_cochrane(state: AxiomState) -> bool:
    """True si el usuario pidió Cochrane Y el kill-switch global lo permite.

    El kill-switch (settings.cochrane_mode_enabled) le da al sysadmin una
    forma de desactivar Cochrane sin tocar código — útil si Featherless está
    rate-limiteado o los modelos de reasoning están lentos.
    """
    return bool(state.get("cochrane_mode", False)) and settings.cochrane_mode_enabled


def route_after_extractor(state: AxiomState) -> Literal["rob_assessor", "clusterer"]:
    """Modo Cochrane → rob_assessor antes del clusterer. Modo fast → directo al clusterer."""
    return "rob_assessor" if _should_run_cochrane(state) else "clusterer"


def route_after_reconciler(state: AxiomState) -> Literal["grade_profiler", "gapfinder"]:
    """Modo Cochrane → grade_profiler antes del gap finder. Modo fast → directo al gap finder."""
    return "grade_profiler" if _should_run_cochrane(state) else "gapfinder"


# ==============================================================================
# CONSTRUCCIÓN DEL GRAFO
# ==============================================================================
def build_axiom_graph():
    builder = StateGraph(AxiomState)

    # 1. Agregar Nodos
    builder.add_node("searcher", run_searcher)
    builder.add_node("screener_7b", screener_7b_node)
    builder.add_node("screener_32b", screener_32b_node)
    builder.add_node("extractor", run_extractor)
    builder.add_node("rob_assessor", run_rob_assessor)        # Cochrane only
    builder.add_node("clusterer", clusterer_node)
    builder.add_node("analyst_7b", analyst_7b_node)
    builder.add_node("analyst_32b", analyst_32b_node)
    builder.add_node("reconciler", reconciler_node)
    builder.add_node("grade_profiler", run_grade_profiler)    # Cochrane only
    builder.add_node("gapfinder", run_gap_finder)
    builder.add_node("writer_synthesis",   writer_synthesis_node)
    builder.add_node("writer_discussion",  writer_discussion_node)    
    builder.add_node("writer_limitations", writer_limitations_node)   
    builder.add_node("writer_tables",      writer_tables_node)
    builder.add_node("writer_references",  writer_references_node)
    builder.add_node("writer_assembler",   writer_assembler_node)

    # 2. Definir Aristas (Flujo)
    builder.add_edge(START, "searcher")
    builder.add_edge("searcher", "screener_7b")
    # Cascada de screening: el 32B procesa solo los papers que el 7B marcó
    # como uncertain/low confidence (vía `papers_to_escalate` en el state).
    # Si no hay nada que escalar, screener_32b retorna {} sin overhead.
    builder.add_edge("screener_7b", "screener_32b")

    # Condicional post-screening (Salta al final si no hay papers).
    # Se evalúa después de screener_32b porque `screened_papers` acumula
    # contribuciones de ambos nodos vía operator.add.
    builder.add_conditional_edges("screener_32b", check_screening_results)

    # Condicional post-extractor: si modo Cochrane, evaluar Risk of Bias antes
    # del clusterer. Si no, ir directo. El clusterer no depende de rob_assessments,
    # así que rob_assessor puede ejecutarse en serie sin afectar otros nodos.
    builder.add_conditional_edges("extractor", route_after_extractor)
    builder.add_edge("rob_assessor", "clusterer")

    # Fan-out: El clusterer alimenta en paralelo a ambos analistas
    builder.add_edge("clusterer", "analyst_7b")
    builder.add_edge("clusterer", "analyst_32b")

    # FAN-IN: El reconciliador necesita que AMBOS analistas terminen
    builder.add_edge("analyst_7b", "reconciler")
    builder.add_edge("analyst_32b", "reconciler")

    # Condicional post-reconciler: si modo Cochrane, aplicar GRADE a cada
    # consensus cluster antes del gap finder. Si no, ir directo.
    builder.add_conditional_edges("reconciler", route_after_reconciler)
    builder.add_edge("grade_profiler", "gapfinder")

    builder.add_edge("gapfinder", "writer_synthesis")
    # Cadena del writer (Paso B completo): synthesis (LLM) → tables (Python) →
    # references (Python) → assembler (Python). Cada nodo escribe su key
    # intermedia en el state; el assembler concatena los 3 y produce 1 PDF.
    builder.add_edge("writer_synthesis",   "writer_discussion")
    builder.add_edge("writer_discussion",  "writer_limitations")
    builder.add_edge("writer_limitations", "writer_tables")
    builder.add_edge("writer_tables",      "writer_references")
    builder.add_edge("writer_references",  "writer_assembler")
    builder.add_edge("writer_assembler",   END)

    return builder.compile()

# Exportamos el pipeline compilado para que axiom_api.py y otros lo importen.
# Sin esta línea, `from axiom_backend.graph import pipeline` falla.
pipeline = build_axiom_graph()