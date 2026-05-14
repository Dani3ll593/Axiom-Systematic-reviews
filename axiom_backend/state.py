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
    
    # ─── Agente 6: Writer ───
    executive_report_md: str
    apa7_literature_review: str
    executive_report_pdf_path: str | None
    apa7_pdf_path: str | None