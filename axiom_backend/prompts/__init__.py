"""
Prompt loader for Axiom agents.

All prompts live as `.md` or `.json` files alongside this module so they can
be edited without touching Python code. The loader reads them once at import
time and caches the strings in module-level constants. JSON files are loaded
into Python dicts.

Usage from agent modules:

    from src.prompts import (
        SEARCHER_PROMPT,
        SCREENER_PROMPT,
        SCREENER_FEWSHOT,
        EXTRACTION_PROMPT,
        EXTRACTOR_SCHEMA,
        ROB_ASSESSOR_PROMPT,
        GRADE_PROFILER_PROMPT,
        PRISMA_CRITERIA_TEMPLATE,
    )

The loader fails loudly at import time if any expected file is missing.
This is deliberate: a missing prompt should never reach runtime.
"""

from __future__ import annotations

import json
from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent


def _read_text(filename: str) -> str:
    path = _PROMPTS_DIR / filename
    if not path.exists():
        raise FileNotFoundError(
            f"Prompt file not found: {path}. "
            f"Every prompt declared in src/prompts/__init__.py must exist on disk."
        )
    return path.read_text(encoding="utf-8").strip()


def _read_json(filename: str) -> dict:
    path = _PROMPTS_DIR / filename
    if not path.exists():
        raise FileNotFoundError(f"JSON prompt asset not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


# Agente 1 — Searcher
SEARCHER_PROMPT: str = _read_text("searcher_prompt.md")

# Agente 2 — Screener (two-stage cascade: 7B first reviewer + 32B adjudicator)
# Cada modelo tiene su propio prompt y few-shot, alineados a su rol.
SCREENER_PROMPT_7B:   str = _read_text("screener_prompt_7b.md")
SCREENER_FEWSHOT_7B:  str = _read_text("screener_fewshot_7b.md")
SCREENER_PROMPT_32B:  str = _read_text("screener_prompt_32b.md")
SCREENER_FEWSHOT_32B: str = _read_text("screener_fewshot_32b.md")

# Agente 3 — Extractor
# EXTRACTION_PROMPT is a template with a {schema} placeholder. The agent
# renders it at call time with PaperExtraction.model_json_schema().
# EXTRACTOR_SCHEMA is the JSON reference copy (NOT the source of truth —
# the source of truth is the Pydantic class in src/agents/extractor.py).
EXTRACTION_PROMPT: str  = _read_text("extraction_prompt.md")
EXTRACTOR_SCHEMA:  dict = _read_json("extractor_schema.json")

# Agentes 4a / 4b — Analysts (dual)
ANALYST_PROMPT_7B:  str = _read_text("analyst_prompt_v3.md")
ANALYST_PROMPT_32B: str = _read_text("analyst_prompt_r1.md")

# Agente 5 — Gap Finder
GAPFINDER_PROMPT: str = _read_text("gapfinder_prompt.md")

# Agente 6 — Writer (bifásico: synthesis → tables → references → assembler)
# WRITER_SYNTHESIS_PROMPT alimenta el nodo writer_synthesis (única llamada LLM
# del writer; los otros 3 nodos son Python puro). Reemplaza al WRITER_PROMPT
# monolítico, que queda DEPRECATED y NO se carga: la lógica de tablas y
# references list ahora vive en Python (writer_tables_node, writer_references_node).
WRITER_SYNTHESIS_PROMPT:   str = _read_text("writer_synthesis_prompt.md")
WRITER_DISCUSSION_PROMPT:  str = _read_text("writer_discussion_prompt.md")
WRITER_LIMITATIONS_PROMPT: str = _read_text("writer_limitations_prompt.md")
WRITER_APA7_RULES:         str = _read_text("writer_apa7_rules.md")

# Agentes Cochrane (solo se cargan; los nodos del grafo deciden si correr)
ROB_ASSESSOR_PROMPT:   str = _read_text("rob_assessor_prompt.md")
GRADE_PROFILER_PROMPT: str = _read_text("grade_profiler_prompt.md")

# UI helper — default PRISMA criteria offered to the user in Streamlit
PRISMA_CRITERIA_TEMPLATE: dict = _read_json("prisma_criteria_template.json")


__all__ = [
    "SEARCHER_PROMPT",
    "SCREENER_PROMPT_7B",
    "SCREENER_FEWSHOT_7B",
    "SCREENER_PROMPT_32B",
    "SCREENER_FEWSHOT_32B",
    "EXTRACTION_PROMPT",
    "EXTRACTOR_SCHEMA",
    "ANALYST_PROMPT_7B",
    "ANALYST_PROMPT_32B",
    "GAPFINDER_PROMPT",
    "WRITER_SYNTHESIS_PROMPT",
    "WRITER_DISCUSSION_PROMPT",    
    "WRITER_LIMITATIONS_PROMPT",   
    "WRITER_APA7_RULES",
    "ROB_ASSESSOR_PROMPT",
    "GRADE_PROFILER_PROMPT",
    "PRISMA_CRITERIA_TEMPLATE",
]