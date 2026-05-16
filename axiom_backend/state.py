"""
El Estado Global de Axiom.
Cada agente recibe esto como input y devuelve un dict con los campos que modificó.
LangGraph se encarga de hacer el merge (reemplazar o acumular).
"""
from typing import TypedDict, Annotated
import operator

class AxiomState(TypedDict, total=False):
    # ─── Inputs de la Ejecución ───
    sr_id: str                      # Identificador de la Revisión Sistemática
    domain: str                     # Dominio de investigación
    question: str                   # Pregunta de investigación (Leída por Searcher)
    prisma_criteria: dict           # Criterios de Inclusión/Exclusión

    # Toggle del modo Cochrane (Risk of Bias + GRADE). Viene del frontend en
    # POST /pipeline/start. Si está ausente o False, el grafo SALTA los nodos
    # rob_assessor y grade_profiler (modo fast: solo PRISMA).
    cochrane_mode: bool

    # ─── Acumulables (Reducers) ───
    # operator.add permite que múltiples agentes sumen listas concurrentemente sin pisarse
    errors: Annotated[list[dict], operator.add] 
    
    # Output del Agente 1 (Searcher) -> Input del Agente 2 (Screener)
    papers_found: Annotated[list[dict], operator.add] 
    
    # Outputs del Agente 2 (Screener)
    screened_papers: Annotated[list[dict], operator.add] # Include / Uncertain
    papers_excluded: Annotated[list[dict], operator.add] # Exclude (para auditoría PRISMA)

    # Key intermedia screener_7b → screener_32b. Lista de papers donde el 7B
    # disparó la regla de escalación (confidence=low o decision=uncertain),
    # cada uno con su veredicto 7B adjunto en `_7b_decision` para que
    # screener_32b pueda hacer fallback si el 32B falla. Escritura atómica
    # (solo screener_7b la produce, solo screener_32b la consume) — NO usa
    # operator.add a propósito.
    papers_to_escalate: list[dict]
    
    # Output del Agente 3 (Extractor)
    extractions: Annotated[list[dict], operator.add]

    # ─── Cochrane Agent: Risk of Bias Assessor (solo modo Cochrane) ───
    # Lookup por paper_id. El grade_profiler downstream lo joinea con
    # extractions por paper_id, no por índice.
    rob_assessments: Annotated[list[dict], operator.add]

    # Output del Clusterer (Escritura atómica, antes de los analistas)
    clusters: list[list[dict]]  # Escritura atómica del clusterer
    
    # ─── Agentes Analistas (4a y 4b) en Paralelo ───
    synthesis_7b: Annotated[list[dict], operator.add]
    synthesis_32b: Annotated[list[dict], operator.add]
    
    # ─── Agente 4r: Reconciliador Determinista ───
    consensus_clusters: list[dict] # Escritura atómica
                                   # (En modo Cochrane el grade_profiler lo
                                   # re-escribe enriquecido con campos grade_*)
    
    # ─── Agente 5: Gap Finder ───
    research_gaps: list[dict]
    
    # ─── Agente 6: Writer (bifásico: synthesis → tables → references → assembler) ───
    # Keys intermedias entre nodos del writer. Escritura atómica (cada nodo
    # produce una sola key, los siguientes la consumen).
    writer_synthesis_md:  str   # writer_synthesis (LLM) → writer_assembler
    writer_tables_md:     str   # writer_tables (Python) → writer_assembler
    writer_references_md: str   # writer_references (Python) → writer_assembler

    # Output final: 1 solo reporte, producido por writer_assembler concatenando
    # los 3 md anteriores y renderizando un PDF unificado.
    executive_report_md: str
    executive_report_pdf_path: str | None

    # DEPRECATED — conservados temporalmente para compatibilidad con consumidores
    # downstream (axiom_api.py /apa7.pdf endpoint, etc). El refactor genera 1
    # solo reporte; estos campos quedan en None tras un run exitoso.
    apa7_literature_review: str
    apa7_pdf_path: str | None