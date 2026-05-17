"""Agent 6 — Writer."""

import asyncio
import json
import logging
import re
from collections import Counter, defaultdict
from pathlib import Path

from pydantic import BaseModel, ValidationError

from axiom_backend.state import AxiomState
from axiom_backend.config import settings
from axiom_backend.tools.llm_router import LLM_32B, extract_json_from_response, featherless_credit, COST_32B
from axiom_backend.prompts import (
    WRITER_SYNTHESIS_PROMPT,
    WRITER_DISCUSSION_PROMPT,
    WRITER_LIMITATIONS_PROMPT,
    WRITER_APA7_RULES,
)

logger = logging.getLogger(__name__)

# --- Tunables ---
TIMEOUT_S = 800.0
MAX_TOKENS = 8000

# Directorio donde se guardan los PDFs generados. axiom_api.py los lee desde aquí.
REPORTS_DIR = Path("data/results")


# --- Esquema de salida del nodo synthesis ---
# El LLM produce SOLO el markdown de prosa (synthesis_md). Tablas y references
# list ya no vienen del LLM — viven en writer_tables_node y
# writer_references_node (Python puro).
class SynthesisOutput(BaseModel):
    synthesis_md: str

class DiscussionOutput(BaseModel):
    discussion_md: str

class LimitationsOutput(BaseModel):
    limitations_md: str


# ============================================================================
# Helpers — Detección de idioma de la pregunta de investigación
# ============================================================================
# Stopwords muy frecuentes y específicas de cada idioma. La heurística no
# pretende ser un detector general — solo distingue ES vs EN, que es lo único
# que el corpus actual produce. Si en el futuro entran PT/FR, ampliar aquí o
# cambiar a `langdetect`.
_ES_MARKERS = {
    "el", "la", "los", "las", "de", "del", "en", "para", "por", "con", "sin",
    "que", "qué", "cuál", "cuáles", "cómo", "cuándo", "dónde", "es", "son",
    "y", "o", "u", "un", "una", "unos", "unas", "sobre", "entre",
    "efectividad", "eficacia", "comparado", "respecto",
}
_EN_MARKERS = {
    "the", "of", "in", "for", "with", "without", "and", "or", "an", "a",
    "what", "which", "how", "when", "where", "is", "are", "to", "from",
    "compared", "between", "among", "effectiveness", "efficacy",
}


def _detect_language(question: str) -> str:
    """Devuelve 'Spanish' o 'English' según el idioma de la pregunta.

    Heurística simple:
    1. Si hay caracteres acentuados típicos del español (á é í ó ú ñ ¿ ¡), es ES.
    2. Si no, cuenta tokens contra listas de stopwords ES vs EN; gana mayoría.
    3. Empate o sin señal → English (default conservador, igual al comportamiento
       actual del prompt antes de este cambio).
    """
    if not question or not question.strip():
        return "English"

    # 1. Acentos hispanos: señal fuerte
    if re.search(r"[áéíóúñÁÉÍÓÚÑ¿¡]", question):
        return "Spanish"

    # 2. Conteo de stopwords
    tokens = re.findall(r"\b[a-záéíóúñ]+\b", question.lower())
    es_hits = sum(1 for t in tokens if t in _ES_MARKERS)
    en_hits = sum(1 for t in tokens if t in _EN_MARKERS)

    if es_hits > en_hits:
        return "Spanish"
    return "English"


# ============================================================================
# Helpers — Tabla de referencias APA 7 (short form)
# ============================================================================
def _last_name(author: str) -> str:
    """Heurística para extraer el apellido en formatos mixtos.

    Maneja:
      - "Last, First M"  → "Last"            (PubMed-style)
      - "First Last"     → "Last"            (OpenAlex/Crossref)
      - "First Middle Last" → "Last"
      - cadena vacía     → ""
    """
    author = (author or "").strip()
    if not author:
        return ""
    if "," in author:
        return author.split(",", 1)[0].strip()
    parts = author.split()
    return parts[-1] if parts else ""


def _normalize_year(year) -> str:
    """Year viene como int (2024), str ("2024"), "n.d." o "" — devolvemos siempre str."""
    if year is None:
        return "n.d."
    if isinstance(year, int):
        return str(year)
    s = str(year).strip()
    return s if s else "n.d."


def _short_citation(authors: list[str], year: str) -> str:
    """Genera la cita short-form APA 7 sin sufijo de desambiguación."""
    if not authors:
        return f"Anónimo, {year}"
    last_names = [_last_name(a) for a in authors if _last_name(a)]
    if not last_names:
        return f"Anónimo, {year}"
    if len(last_names) == 1:
        return f"{last_names[0]}, {year}"
    if len(last_names) == 2:
        return f"{last_names[0]} & {last_names[1]}, {year}"
    return f"{last_names[0]} et al., {year}"


def _build_references_table(papers: list[dict]) -> dict[str, str]:
    """{paper_id: 'Smith et al., 2023a'} con desambiguación a/b/c.

    Aplica la regla APA 7: si dos o más papers comparten (autores-cortos, año),
    se sufijan letras alfabéticamente para que cada cita sea única.
    """
    base: dict[str, tuple[str, str]] = {}  # pid -> (author_str, year_str)
    for p in papers:
        pid = p.get("paper_id")
        if not pid:
            continue
        authors = p.get("authors") or []
        year = _normalize_year(p.get("year"))
        # Citación base sin año todavía sufijado
        if not authors:
            author_str = "Anónimo"
        elif len(authors) == 1:
            author_str = _last_name(authors[0]) or "Anónimo"
        elif len(authors) == 2:
            author_str = f"{_last_name(authors[0])} & {_last_name(authors[1])}"
        else:
            author_str = f"{_last_name(authors[0])} et al."
        base[pid] = (author_str, year)

    # Agrupar por (autor_str, year) para desambiguar
    groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for pid, key in base.items():
        groups[key].append(pid)

    table: dict[str, str] = {}
    for (author_str, year), pids in groups.items():
        if len(pids) == 1:
            table[pids[0]] = f"{author_str}, {year}"
        else:
            # Orden estable por paper_id para que las letras sean reproducibles
            for i, pid in enumerate(sorted(pids)):
                # a..z; si pasamos de 26 (improbable), seguimos con aa, ab...
                if i < 26:
                    suffix = chr(ord("a") + i)
                else:
                    suffix = chr(ord("a") + (i // 26) - 1) + chr(ord("a") + (i % 26))
                table[pid] = f"{author_str}, {year}{suffix}"
    return table


# ============================================================================
# Helpers — Restricted papers + PRISMA flow
# ============================================================================
def _build_restricted_list(screened_papers: list[dict]) -> list[dict]:
    """Solo los papers relevantes que NO son open access — para la sección
    'RESTRICTED ACCESS ARTICLES' del reporte ejecutivo."""
    restricted = []
    for p in screened_papers:
        if p.get("is_open"):
            continue
        restricted.append({
            "paper_id":         p.get("paper_id"),
            "title":            p.get("title"),
            "doi":              p.get("doi"),
            "source":           p.get("source"),
            "access_confidence": p.get("access_confidence"),
        })
    return restricted


def _build_prisma_flow(
    papers_found: list[dict],
    screened_papers: list[dict],
    papers_excluded: list[dict],
) -> dict:
    """Conteos para el PRISMA 2020 flow diagram."""
    excluded_by_reason = Counter()
    for p in papers_excluded:
        reason = (p.get("screening") or {}).get("reason") or "unspecified"
        excluded_by_reason[reason] += 1
    return {
        "found":              len(papers_found),
        "included":           len(screened_papers),
        "excluded_total":     len(papers_excluded),
        "excluded_by_reason": dict(excluded_by_reason),
    }


# ============================================================================
# Helpers — Llamada a QwQ-32B con un reintento (espejo del patrón en gap_finder)
# ============================================================================
async def _call_qwq_with_retry(
    system_prompt: str,
    user_msg: str,
    output_class: type[BaseModel] = SynthesisOutput,
    node_label: str = "writer_synthesis",
) -> BaseModel:
    """Llama a un nodo writer (synthesis | discussion | limitations) y, si la
    respuesta no parsea o no valida, reintenta una vez con temperatura distinta.

    Generalizado tras el refactor 3-nodos: cada nodo de prosa pasa su propio
    `output_class` (SynthesisOutput / DiscussionOutput / LimitationsOutput) y
    `node_label` (para los logs). El resto del comportamiento es idéntico al
    helper original — incluyendo el fallback `content or reasoning` para R1.
    """
    last_err: Exception | None = None
    for attempt, temperature in ((1, 0.3), (2, 0.55)):
        try:
            async with featherless_credit(cost=COST_32B):
                response = await asyncio.wait_for(
                    LLM_32B.chat.completions.create(
                        model=settings.model_32b_name,
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user",   "content": user_msg},
                        ],
                        temperature=temperature,
                        max_tokens=MAX_TOKENS,
                    ),
                    timeout=TIMEOUT_S,
                )
            msg = response.choices[0].message
            raw_text = msg.content or getattr(msg, "reasoning", None) or ""
            parsed_json = extract_json_from_response(raw_text)
            return output_class(**parsed_json)
        except (json.JSONDecodeError, ValidationError, ValueError) as e:
            last_err = e
            if attempt == 1:
                logger.warning(
                    "writer_synthesis: intento %d falló (%s); reintentando con temperature=0.55.",
                    attempt, type(e).__name__,
                )
            continue
    raise last_err  # type: ignore[misc]


# ============================================================================
# Helpers — Generación de PDFs (reporte ejecutivo + APA 7)
# ============================================================================
# Dos perfiles de styling: el reporte ejecutivo es markdown rico (headers,
# tablas, bullets); el APA 7 es prosa académica con sangría y referencias.
# Comparten base CSS pero divergen en algunos detalles.

# Base común: tipografía y márgenes A4
_CSS_BASE = """
@page { size: A4; margin: 22mm 25mm; }
body {
    font-family: 'DejaVu Sans', 'Helvetica Neue', 'Helvetica', 'Arial', sans-serif;
    color: #2a2a2a;
    line-height: 1.6;
    font-size: 11pt;
}
h1 {
    color: #1E3A8A;
    border-bottom: 2px solid #1E3A8A;
    padding-bottom: 6px;
    font-size: 20pt;
    margin-top: 0;
}
h2 {
    color: #2563EB;
    margin-top: 22px;
    font-size: 14pt;
    border-bottom: 1px solid #cbd5e0;
    padding-bottom: 4px;
}
h3 { color: #2563EB; font-size: 12pt; margin-top: 16px; }
.axiom-meta {
    background: #f8fafc;
    border-left: 3px solid #1E3A8A;
    padding: 10px 14px;
    margin: 16px 0 22px 0;
    font-size: 10pt;
    color: #475569;
}
.axiom-footer {
    margin-top: 28px; padding-top: 10px; border-top: 1px solid #cbd5e0;
    font-size: 8.5pt; color: #94a3b8; text-align: center;
}
"""

# Reporte ejecutivo: markdown rico con tablas, código, blockquotes
_CSS_EXECUTIVE = _CSS_BASE + """
p { margin: 8px 0; text-align: justify; }
ul, ol { margin: 8px 0 8px 18px; }
li { margin: 4px 0; }
table { border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 10pt; }
th, td { border: 1px solid #cbd5e0; padding: 6px 10px; text-align: left; }
th { background: #f1f5f9; font-weight: 600; }
code {
    background: #f1f5f9; padding: 1px 5px; border-radius: 3px;
    font-family: 'Courier New', monospace; font-size: 9.5pt;
}
blockquote {
    border-left: 3px solid #2563EB; padding: 4px 14px;
    margin: 10px 0; color: #475569; font-style: italic;
}
"""

# APA 7: prosa académica, primera línea sangrada en párrafos, sin justify forzado
# (los reviewers APA prefieren left-aligned), referencias hanging indent
_CSS_APA7 = _CSS_BASE + """
body { font-family: 'DejaVu Serif', 'Times New Roman', 'Times', serif; }
p {
    margin: 0 0 6px 0;
    text-indent: 1.27cm;     /* 0.5 in — sangría APA 7 estándar */
    text-align: left;
}
p:first-of-type, h1 + p, h2 + p, h3 + p { text-indent: 0; }
h2 { text-align: center; text-transform: none; border: none; }
h3 { font-style: italic; border: none; }
ul, ol { margin: 6px 0 10px 24px; }
/* Referencias en hanging indent (al final del documento) */
.references p {
    text-indent: -1.27cm;
    padding-left: 1.27cm;
    margin-bottom: 8px;
}
em { font-style: italic; }
"""


def _render_pdf(
    markdown_text: str,
    output_path: Path,
    title: str,
    css: str,
    meta_block: str = "",
) -> Path | None:
    """Convierte Markdown a PDF en `output_path` con el CSS dado.

    LAZY IMPORT: WeasyPrint y markdown se cargan acá adentro para que el
    módulo siga importable aunque las libs nativas (libpango, libcairo) no
    estén en el sistema. Si fallan, devolvemos None y seguimos.
    """
    try:
        import markdown as md_lib
        from weasyprint import HTML
    except (ImportError, OSError) as e:
        logger.warning(
            "writer: WeasyPrint/markdown no disponibles (%s: %s). "
            "El PDF '%s' NO se generará.",
            type(e).__name__, e, output_path.name,
        )
        return None

    try:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)

        html_body = md_lib.markdown(
            markdown_text,
            extensions=["tables", "fenced_code", "nl2br"],
        )

        full_html = f"""<!DOCTYPE html>
<html>
  <head>
    <meta charset="utf-8"/>
    <title>{title}</title>
    <style>{css}</style>
  </head>
  <body>
    <h1>{title}</h1>
    {meta_block}
    {html_body}
    <div class="axiom-footer">
      Generated by Axiom · AMD MI300X · vLLM + LangGraph + QwQ-32B
    </div>
  </body>
</html>"""

        HTML(string=full_html).write_pdf(str(output_path))
        logger.info(
            "writer: PDF generado en %s (%.1f KB)",
            output_path, output_path.stat().st_size / 1024,
        )
        return output_path

    except Exception as e:
        logger.warning(
            "writer: falló la generación del PDF '%s' (%s: %s).",
            output_path.name, type(e).__name__, e,
        )
        return None


def _escape_html(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _generate_executive_pdf(markdown_text: str, sr_id: str, question: str) -> Path | None:
    """PDF del executive_report_md."""
    meta_block = (
        f'<div class="axiom-meta">'
        f'<strong>Run ID:</strong> {sr_id}<br/>'
        f'<strong>Research question:</strong> <em>{_escape_html(question)}</em>'
        f'</div>'
    )
    return _render_pdf(
        markdown_text=markdown_text,
        output_path=REPORTS_DIR / f"Axiom_Report_{sr_id}.pdf",
        title="Axiom — Systematic Review Report",
        css=_CSS_EXECUTIVE,
        meta_block=meta_block,
    )


def _generate_apa7_pdf(apa_text: str, sr_id: str, question: str) -> Path | None:
    """PDF del apa7_literature_review."""
    meta_block = (
        f'<div class="axiom-meta">'
        f'<strong>Run ID:</strong> {sr_id}<br/>'
        f'<strong>Research question:</strong> <em>{_escape_html(question)}</em>'
        f'</div>'
    )
    return _render_pdf(
        markdown_text=apa_text,
        output_path=REPORTS_DIR / f"Axiom_APA7_{sr_id}.pdf",
        title="Literature Review (APA 7)",
        css=_CSS_APA7,
        meta_block=meta_block,
    )


# ============================================================================
# LangGraph Nodes — Writer (bifásico)
# ============================================================================
# Arquitectura: writer_synthesis (LLM) → writer_tables (Python) →
# writer_references (Python) → writer_assembler (Python). Cada nodo escribe
# su propia key intermedia en el state; el assembler concatena los 3 y genera
# UN solo PDF unificado.
#
# Paso A (este archivo): solo writer_synthesis_node implementado.
# Paso B: writer_tables_node, writer_references_node, writer_assembler_node.
# ============================================================================

def _build_writer_payload(state: AxiomState) -> tuple[dict, dict, str]:
    """Construye el payload que comparten los 3 nodos de prosa.

    Devuelve:
      - payload         : dict con question, consensus_findings, gaps, references_table
      - references_table: {paper_id: short-form citation} (también útil downstream)
      - output_language : 'English' o 'Spanish' detectado de la pregunta

    Centralizado para que los 3 nodos LLM compartan exactamente el mismo input
    (mismo references_table, misma estructura, mismo idioma).
    """
    screened_papers = state.get("screened_papers", [])
    consensus       = state.get("consensus_clusters", [])
    gaps            = state.get("research_gaps", [])
    question        = state.get("question", "Pregunta no definida")

    references_table = _build_references_table(screened_papers)

    consensus_findings = [
        {
            "claim":                c.get("core_claim"),
            "agreement_percentage": c.get("agreement_percentage"),
            "is_heterogeneous":     c.get("heterogeneity_detected"),
            "supporting_papers":    c.get("supporting_papers", []),
            "contradicting_papers": c.get("contradicting_papers", []),
            "neutral_papers":       c.get("neutral_papers", []),
            "contradictions_found": c.get("contradiction_quotes", {}),
        }
        for c in consensus
    ]

    payload = {
        "research_question":  question,
        "consensus_findings": consensus_findings,
        "verified_gaps":      gaps,
        "references_table":   references_table,
    }

    return payload, references_table, _detect_language(question)


async def writer_synthesis_node(state: AxiomState) -> dict:
    """Nodo 1/6 del writer: genera Executive Summary + Synthesis of Findings.

    Output del state:
      - writer_synthesis_md: markdown con secciones Executive Summary y
        Comprehensive Synthesis of Findings (NO Discussion, NO Limitations,
        NO Future Research — esos están en writer_discussion / writer_limitations).
    """
    payload, _, output_language = _build_writer_payload(state)

    logger.info("writer_synthesis: idioma detectado = %s", output_language)
    system_prompt = (
        WRITER_SYNTHESIS_PROMPT
        .replace("{apa7_rules_text}", WRITER_APA7_RULES)
        .replace("{output_language}", output_language)
    )
    user_msg = f"SYNTHESIS PAYLOAD:\n{json.dumps(payload, ensure_ascii=False)}"

    logger.info(
        "writer_synthesis: %d incluidos, %d gaps, %d clusters",
        len(state.get("screened_papers", [])),
        len(state.get("research_gaps", [])),
        len(state.get("consensus_clusters", [])),
    )

    try:
        validated = await _call_qwq_with_retry(
            system_prompt, user_msg, SynthesisOutput, "writer_synthesis",
        )
        logger.info(
            "writer_synthesis: prosa generada (%d chars)",
            len(validated.synthesis_md),
        )
        return {"writer_synthesis_md": validated.synthesis_md}
    except ValidationError as e:
        logger.error("writer_synthesis: validation_error tras reintento: %s", e)
        return {
            "writer_synthesis_md": "",
            "errors": [{"node": "writer_synthesis", "error": f"validation_error: {e}"}],
        }
    except Exception as e:
        logger.exception("writer_synthesis: fallo tras reintento")
        return {
            "writer_synthesis_md": "",
            "errors": [{"node": "writer_synthesis", "error": str(e)}],
        }

async def writer_discussion_node(state: AxiomState) -> dict:
    """Nodo 2/6 del writer: genera In-Depth Discussion (solo).

    Recibe el mismo payload que synthesis pero un prompt distinto enfocado en
    INTERPRETAR la evidencia (no re-describirla). Output: writer_discussion_md.
    """
    payload, _, output_language = _build_writer_payload(state)

    system_prompt = (
        WRITER_DISCUSSION_PROMPT
        .replace("{apa7_rules_text}", WRITER_APA7_RULES)
        .replace("{output_language}", output_language)
    )
    user_msg = f"DISCUSSION PAYLOAD:\n{json.dumps(payload, ensure_ascii=False)}"

    try:
        validated = await _call_qwq_with_retry(
            system_prompt, user_msg, DiscussionOutput, "writer_discussion",
        )
        logger.info(
            "writer_discussion: prosa generada (%d chars)",
            len(validated.discussion_md),
        )
        return {"writer_discussion_md": validated.discussion_md}
    except ValidationError as e:
        logger.error("writer_discussion: validation_error tras reintento: %s", e)
        return {
            "writer_discussion_md": "",
            "errors": [{"node": "writer_discussion", "error": f"validation_error: {e}"}],
        }
    except Exception as e:
        logger.exception("writer_discussion: fallo tras reintento")
        return {
            "writer_discussion_md": "",
            "errors": [{"node": "writer_discussion", "error": str(e)}],
        }


async def writer_limitations_node(state: AxiomState) -> dict:
    """Nodo 3/6 del writer: genera Limitations + Future Research Directions.

    Mismo payload que los demás; prompt enfocado en boundaries de la evidencia
    y recomendaciones derivadas de los verified_gaps. Output: writer_limitations_md.
    """
    payload, _, output_language = _build_writer_payload(state)

    system_prompt = (
        WRITER_LIMITATIONS_PROMPT
        .replace("{apa7_rules_text}", WRITER_APA7_RULES)
        .replace("{output_language}", output_language)
    )
    user_msg = f"LIMITATIONS PAYLOAD:\n{json.dumps(payload, ensure_ascii=False)}"

    try:
        validated = await _call_qwq_with_retry(
            system_prompt, user_msg, LimitationsOutput, "writer_limitations",
        )
        logger.info(
            "writer_limitations: prosa generada (%d chars)",
            len(validated.limitations_md),
        )
        return {"writer_limitations_md": validated.limitations_md}
    except ValidationError as e:
        logger.error("writer_limitations: validation_error tras reintento: %s", e)
        return {
            "writer_limitations_md": "",
            "errors": [{"node": "writer_limitations", "error": f"validation_error: {e}"}],
        }
    except Exception as e:
        logger.exception("writer_limitations: fallo tras reintento")
        return {
            "writer_limitations_md": "",
            "errors": [{"node": "writer_limitations", "error": str(e)}],
        }


# ────────────────────────────────────────────────────────────────────────────
# Paso B — Helpers Python puros para tables y references
# ────────────────────────────────────────────────────────────────────────────
def _format_authors_apa7_long(authors: list[str]) -> str:
    """Lista de autores en formato APA 7 'long' para la lista de referencias.

    APA 7 reglas:
      - 1 autor:            "Smith, A."
      - 2 autores:          "Smith, A., & Lee, B."
      - 3-20 autores:       todos listados con comas, "&" antes del último
      - >20 autores:        primeros 19, ", ..., ", luego el último
      - Sin autores:        "Anónimo"

    Para cada nombre asumimos "First Last" o "Last, First". Si solo viene
    un token, lo usamos tal cual.
    """
    if not authors:
        return "Anónimo"

    def fmt_one(name: str) -> str:
        # Si ya viene "Last, First" lo respetamos
        if "," in name:
            return name.strip()
        parts = [p for p in name.strip().split() if p]
        if not parts:
            return name
        if len(parts) == 1:
            return parts[0]
        last = parts[-1]
        initials = " ".join(f"{p[0]}." for p in parts[:-1] if p)
        return f"{last}, {initials}"

    formatted = [fmt_one(a) for a in authors if a and a.strip()]
    if not formatted:
        return "Anónimo"

    if len(formatted) == 1:
        return formatted[0]
    if len(formatted) == 2:
        return f"{formatted[0]}, & {formatted[1]}"
    if len(formatted) <= 20:
        return ", ".join(formatted[:-1]) + f", & {formatted[-1]}"
    # >20: APA 7 lista los primeros 19, "...", luego el último
    return ", ".join(formatted[:19]) + ", ... " + formatted[-1]


def _build_apa7_full_entry(paper: dict) -> str:
    """Una entrada completa APA 7 a partir de los campos del paper.

    Estructura: `Authors. (Year). Title. *Journal/Source*. https://doi.org/DOI`

    Cualquier campo faltante se omite con elegancia — no inventamos info.
    """
    authors = _format_authors_apa7_long(paper.get("authors") or [])
    year = _normalize_year(paper.get("year"))
    title = (paper.get("title") or "Sin título").strip().rstrip(".")
    journal = (paper.get("journal") or paper.get("venue") or "").strip()
    
    # ¡Agregamos la extracción de las variables que faltaban!
    journal = (paper.get("journal") or paper.get("venue") or "").strip()
    volume = str(paper.get("volume") or "").strip()
    issue = str(paper.get("issue") or "").strip()
    pages = str(paper.get("pages") or "").strip()
    doi = (paper.get("doi") or "").strip()

    parts = [f"{authors} ({year}). {title}."]

    if journal:
        journal_part = f"<em>{journal}</em>"
        if volume:
            journal_part += f", <em>{volume}</em>"
            if issue:
                journal_part += f"({issue})"
        if pages:
            journal_part += f", {pages}"
        
        parts.append(journal_part + ".")

    if doi:
        # Normalizar a https://doi.org/...
        if doi.startswith("http"):
            parts.append(doi)
        else:
            parts.append(f"https://doi.org/{doi.lstrip('/')}")
            
    return " ".join(parts)


def _md_table(header: list[str], rows: list[list[str]]) -> str:
    """Genera una tabla GFM Markdown a partir de header y rows. Maneja celdas
    con `|` o `\\n` escapándolas para no romper el render.
    """
    def clean(c) -> str:
        s = str(c) if c is not None else ""
        return s.replace("\n", " ").replace("|", "\\|").strip()

    head = "| " + " | ".join(clean(c) for c in header) + " |"
    sep  = "| " + " | ".join("---" for _ in header) + " |"
    body = "\n".join("| " + " | ".join(clean(c) for c in row) + " |" for row in rows)
    return f"{head}\n{sep}\n{body}" if body else f"{head}\n{sep}"


# ────────────────────────────────────────────────────────────────────────────
# Nodo 2/4 — writer_tables (Python puro, sin LLM)
# ────────────────────────────────────────────────────────────────────────────

# ────────────────────────────────────────────────────────────────────────────
# i18n — labels traducibles para tablas y secciones de references
# ────────────────────────────────────────────────────────────────────────────
# Mantener sincronizado con _detect_language(). Si en el futuro se agregan
# idiomas (PT, FR…), agregar la entrada acá y en el detector.


_TABLE_LABELS = {
    "English": {
        "prisma_flow_title":     "PRISMA Flow",
        "stage":                 "Stage",
        "n":                     "n",
        "records_identified":    "Records identified",
        "records_screened":      "Records screened",
        "records_included":      "Records included",
        "records_excluded":      "Records excluded",
        "restricted_papers":     "Restricted access papers",
        "exclusion_reasons":     "Exclusion reasons",
        "reason":                "Reason",
        "cluster_summary_title": "Cluster Summary",
        "cluster":               "Cluster",
        "core_claim":            "Core claim",
        "n_papers":              "n papers",
        "agreement_pct":         "Agreement (%)",
        "consensus_level":       "Consensus level",
        "heterogeneity":         "Heterogeneity",
        "grade":                 "GRADE",
        "yes":                   "Yes",
        "no":                    "No",
        "not_assessed":          "not assessed",
        "papers_by_cluster":     "Papers by Cluster",
        "citation":              "Citation",
        "position":              "Position",
        "year":                  "Year",
        "study_design":          "Study design",
        "supports":              "Supports",
        "contradicts":           "Contradicts",
        "neutral":               "Neutral",
        "no_clusters":           "_No clusters formed._",
        "no_papers_in_clusters": "_No papers in any cluster._",
    },
    "Spanish": {
        "prisma_flow_title":     "Flujo PRISMA",
        "stage":                 "Etapa",
        "n":                     "n",
        "records_identified":    "Registros identificados",
        "records_screened":      "Registros cribados",
        "records_included":      "Registros incluidos",
        "records_excluded":      "Registros excluidos",
        "restricted_papers":     "Artículos de acceso restringido",
        "exclusion_reasons":     "Razones de exclusión",
        "reason":                "Razón",
        "cluster_summary_title": "Resumen de clusters",
        "cluster":               "Cluster",
        "core_claim":            "Afirmación principal",
        "n_papers":              "n artículos",
        "agreement_pct":         "Acuerdo (%)",
        "consensus_level":       "Nivel de consenso",
        "heterogeneity":         "Heterogeneidad",
        "grade":                 "GRADE",
        "yes":                   "Sí",
        "no":                    "No",
        "not_assessed":          "no evaluado",
        "papers_by_cluster":     "Artículos por cluster",
        "citation":              "Cita",
        "position":              "Posición",
        "year":                  "Año",
        "study_design":          "Diseño del estudio",
        "supports":              "Respalda",
        "contradicts":           "Contradice",
        "neutral":               "Neutral",
        "no_clusters":           "_No se formaron clusters._",
        "no_papers_in_clusters": "_No hay artículos en ningún cluster._",
    },
}

_REASON_TRANSLATIONS = {
    "English": {
        "wrong_population": "Wrong population",
        "wrong_intervention": "Wrong intervention",
        "wrong_study_design": "Wrong study design",
        "wrong_comparison": "Wrong comparison",
        "wrong_outcomes": "Wrong outcomes",
        "wrong_year": "Outside date range",
        "wrong_language": "Wrong language",
        "unavailable_full_text": "Full text unavailable",
        "not_relevant": "Not relevant to research question",
        "unspecified": "Unspecified reason"
    },
    "Spanish": {
        "wrong_population": "Población incorrecta",
        "wrong_intervention": "Intervención incorrecta",
        "wrong_study_design": "Diseño de estudio incorrecto",
        "wrong_comparison": "Comparador no aplica",
        "wrong_outcomes": "Resultados no evaluados",
        "wrong_year": "Fuera del rango de años",
        "wrong_language": "Idioma excluido",
        "unavailable_full_text": "Texto completo no disponible",
        "not_relevant": "No relevante para la pregunta de investigación",
        "unspecified": "Razón no especificada"
    }
}

_CONSENSUS_TRANSLATIONS = {
    "English": {
        "full_agreement": "Full agreement",
        "majority_agreement": "Majority agreement",
        "mixed_results": "Mixed results",
        "no_consensus": "No consensus",
    },
    "Spanish": {
        "full_agreement": "Acuerdo total",
        "majority_agreement": "Acuerdo mayoritario",
        "mixed_results": "Resultados mixtos",
        "no_consensus": "Sin consenso",
    }
}

_REFERENCES_LABELS = {
    "English": {
        "included_title":   "References (included, n={n})",
        "restricted_title": "References — restricted access (n={n})",
        "empty_section":    "_No papers in this category._",
    },
    "Spanish": {
        "included_title":   "Referencias (incluidas, n={n})",
        "restricted_title": "Referencias — acceso restringido (n={n})",
        "empty_section":    "_No hay artículos en esta categoría._",
    },
}


async def writer_tables_node(state: AxiomState) -> dict:
    """Construye las 3 tablas en Markdown, localizadas al idioma de la pregunta.

    Headers y títulos de sección se toman de _TABLE_LABELS según
    _detect_language(question). El contenido (citas, claims, GRADE values) NO
    se traduce — son strings emitidos por el pipeline upstream.
    """
    consensus        = state.get("consensus_clusters", []) or []
    papers_found     = state.get("papers_found", []) or []
    screened_papers  = state.get("screened_papers", []) or []
    papers_excluded  = state.get("papers_excluded", []) or []
    extractions      = state.get("extractions", []) or []
    question         = state.get("question", "")

    lang = _detect_language(question)
    L = _TABLE_LABELS.get(_detect_language(question), _TABLE_LABELS["English"])
    R = _REASON_TRANSLATIONS.get(lang, _REASON_TRANSLATIONS["English"])

    references_table = _build_references_table(screened_papers)
    design_by_pid = {
        (e.get("paper_id") if isinstance(e, dict) else getattr(e, "paper_id", None)):
        (e.get("study_design") if isinstance(e, dict) else getattr(e, "study_design", None)) or "—"
        for e in extractions
    }

    # ── Tabla 1: PRISMA Flow ───────────────────────────────────────────────
    restricted_n = sum(1 for p in screened_papers if not p.get("is_open"))
    flow_rows = [
        [L["records_identified"], str(len(papers_found))],
        [L["records_screened"],   str(len(papers_found))],
        [L["records_included"],   str(len(screened_papers))],
        [L["records_excluded"],   str(len(papers_excluded))],
        [L["restricted_papers"],  str(restricted_n)],
    ]
    flow_md = _md_table([L["stage"], L["n"]], flow_rows)

    excl_md = ""
    if papers_excluded:
        excluded_by_reason = Counter()
        for p in papers_excluded:
            raw_reason = (p.get("screening") or {}).get("reason") or "unspecified"
            # Traducir la razón, si no existe en el dict hace un fallback limpiando el snake_case
            clean_reason = R.get(raw_reason, raw_reason.replace("_", " ").capitalize())
            excluded_by_reason[clean_reason] += 1
            
        excl_rows = [[r, str(n)] for r, n in excluded_by_reason.most_common()]
        excl_md = f"\n\n### {L['exclusion_reasons']}\n\n" + _md_table(
            [L["reason"], L["n"]], excl_rows,
        )

    # ── Tabla 2: resumen de clusters ───────────────────────────────────────
    cluster_header = [
        L["cluster"], L["core_claim"], L["n_papers"],
        L["agreement_pct"], L["consensus_level"], L["heterogeneity"], L["grade"],
    ]
    
    # Extraemos el diccionario de traducciones de consenso en el idioma adecuado
    C = _CONSENSUS_TRANSLATIONS.get(lang, _CONSENSUS_TRANSLATIONS["English"])
    
    cluster_rows = []
    for i, c in enumerate(consensus, start=1):
        raw_consensus = c.get("consensus_level") or "—"
        # Traducir. Fallback a limpiar el texto si no está en el diccionario.
        clean_consensus = C.get(raw_consensus, raw_consensus.replace("_", " ").capitalize())
        
        cluster_rows.append([
            f"C{i}",
            (c.get("core_claim") or "—"),
            str(c.get("total_papers_in_cluster") or "—"),
            str(c.get("agreement_percentage") or "—"),
            clean_consensus,  # <--- Usamos el texto limpio aquí
            L["yes"] if c.get("heterogeneity_detected") else L["no"],
            (c.get("grade_final_certainty") or L["not_assessed"]),
        ])
    clusters_md = _md_table(cluster_header, cluster_rows) if cluster_rows else L["no_clusters"]

    # ── Tabla 3: papers por cluster ────────────────────────────────────────
    paper_header = [L["cluster"], L["citation"], L["position"], L["year"], L["study_design"]]
    paper_rows = []
    for i, c in enumerate(consensus, start=1):
        for pid in (c.get("supporting_papers") or []):
            paper_rows.append([f"C{i}", references_table.get(pid, pid), L["supports"],
                               _extract_year_from_citation(references_table.get(pid, "")),
                               design_by_pid.get(pid, "—")])
        for pid in (c.get("contradicting_papers") or []):
            paper_rows.append([f"C{i}", references_table.get(pid, pid), L["contradicts"],
                               _extract_year_from_citation(references_table.get(pid, "")),
                               design_by_pid.get(pid, "—")])
        for pid in (c.get("neutral_papers") or []):
            paper_rows.append([f"C{i}", references_table.get(pid, pid), L["neutral"],
                               _extract_year_from_citation(references_table.get(pid, "")),
                               design_by_pid.get(pid, "—")])
    papers_md = _md_table(paper_header, paper_rows) if paper_rows else L["no_papers_in_clusters"]

    tables_md = (
        f"## {L['prisma_flow_title']}\n\n"
        f"{flow_md}"
        f"{excl_md}\n\n"
        f"## {L['cluster_summary_title']}\n\n"
        f"{clusters_md}\n\n"
        f"## {L['papers_by_cluster']}\n\n"
        f"{papers_md}\n"
    )

    logger.info(
        "writer_tables: 3 tablas generadas | lang=%s | flow=%d | clusters=%d | rows_papers=%d",
        _detect_language(question), len(flow_rows), len(cluster_rows), len(paper_rows),
    )
    return {"writer_tables_md": tables_md}


def _extract_year_from_citation(citation: str) -> str:
    """Extrae el año de una citación short-form como 'Smith et al., 2023a'.

    Devuelve "2023" sin el sufijo a/b/c, o "n.d." si no encuentra patrón.
    """
    match = re.search(r"\b(19|20)\d{2}\b", citation or "")
    return match.group(0) if match else "n.d."


# ────────────────────────────────────────────────────────────────────────────
# Nodo 3/4 — writer_references (Python puro, sin LLM)
# ────────────────────────────────────────────────────────────────────────────
async def writer_references_node(state: AxiomState) -> dict:
    """Construye la lista de referencias APA 7, numerada y separada en dos
    secciones (incluidas / acceso restringido). La numeración REINICIA en cada
    sección. Títulos localizados al idioma de la pregunta.

    El hanging indent (sangría francesa) sigue aplicándose vía CSS sobre
    `.references p` — cada entrada se emite como párrafo prefijado con
    `1.  `, `2.  `, etc., para que el indent funcione sin necesidad de <ol>.
    """
    screened = state.get("screened_papers", []) or []
    question = state.get("question", "")
    L = _REFERENCES_LABELS.get(_detect_language(question), _REFERENCES_LABELS["English"])

    included   = [p for p in screened if p.get("is_open")]
    restricted = [p for p in screened if not p.get("is_open")]

    def _sort_key(p: dict) -> str:
        authors = p.get("authors") or []
        if not authors:
            return "zzz"
        return _last_name(authors[0]).lower()

    included.sort(key=_sort_key)
    restricted.sort(key=_sort_key)

    def _numbered_section(title: str, papers: list[dict]) -> str:
        if not papers:
            return f"## {title}\n\n{L['empty_section']}\n"
        
        # Envolvemos cada entrada en un div en lugar de usar enumeración Markdown
        entries = "\n".join(
            f'<div class="reference-item">{_build_apa7_full_entry(p)}</div>'
            for p in papers
        )
        return f'## {title}\n\n<div class="references">\n{entries}\n</div>\n'

    n_inc = len(included)
    n_res = len(restricted)

    sections = [
        _numbered_section(L["included_title"].format(n=n_inc), included),
    ]
    if restricted:
        sections.append(
            _numbered_section(L["restricted_title"].format(n=n_res), restricted)
        )

    references_md = "\n".join(sections)

    logger.info(
        "writer_references: %d incluidas, %d restringidas (%d chars)",
        n_inc, n_res, len(references_md),
    )

    return {"writer_references_md": references_md}

# ────────────────────────────────────────────────────────────────────────────
# Nodo 4/4 — writer_assembler (Python puro, sin LLM)
# ────────────────────────────────────────────────────────────────────────────
#
# Estructura final del PDF (orden estilo manuscript académico):
#   1. Title page (meta_block en HTML)
#   2. Executive Summary + Synthesis of Findings  (de writer_synthesis_md)
#   3. Tables                                      (de writer_tables_md)
#   4. Discussion + Limitations + Future Research (de writer_synthesis_md)
#   5. References (included + restricted)         (de writer_references_md)
#
# Las secciones de la prosa (synthesis_md) están encabezadas por `## Header`.
# Las dividimos para intercalar las tablas entre "Synthesis of Findings" y
# "Discussion". Si el corte no es identificable, fallback: tablas van al final
# antes de references.
# ────────────────────────────────────────────────────────────────────────────

async def writer_assembler_node(state: AxiomState) -> dict:
    """Concatena los 5 markdown intermedios en orden y produce 1 PDF unificado.

    Orden final del documento:
      1. Title page (meta_block en HTML)
      2. Executive Summary + Synthesis of Findings  (writer_synthesis_md)
      3. Tables                                      (writer_tables_md)
      4. In-Depth Discussion                         (writer_discussion_md)
      5. Limitations + Future Research               (writer_limitations_md)
      6. References (incluidas + restringidas)       (writer_references_md)

    Si algún markdown intermedio viene vacío (porque su nodo falló), se
    reemplaza por un placeholder marcado claramente para que el lector vea
    que esa sección falló pero el documento sigue siendo entregable.
    """
    synthesis_md   = state.get("writer_synthesis_md")   or ""
    discussion_md  = state.get("writer_discussion_md")  or ""
    limitations_md = state.get("writer_limitations_md") or ""
    tables_md      = state.get("writer_tables_md")      or ""
    references_md  = state.get("writer_references_md")  or ""

    # Placeholders para secciones que fallaron upstream
    if not synthesis_md.strip():
        synthesis_md = (
            "## Synthesis of Findings\n\n"
            "_⚠ Synthesis section unavailable — the synthesis node failed. "
            "See errors in the run log._\n"
        )
    if not discussion_md.strip():
        discussion_md = (
            "## In-Depth Discussion\n\n"
            "_⚠ Discussion section unavailable — the discussion node failed._\n"
        )
    if not limitations_md.strip():
        limitations_md = (
            "## Limitations of the Evidence Base\n\n"
            "_⚠ Limitations section unavailable — the limitations node failed._\n"
        )
    if not tables_md.strip():
        tables_md = (
            "## Tables\n\n"
            "_⚠ Tables unavailable — no clusters or insufficient data._\n"
        )
    if not references_md.strip():
        references_md = (
            "## References\n\n"
            "_⚠ References list unavailable — no screened papers._\n"
        )

    combined_md = (
        synthesis_md.rstrip()
        + "\n\n---\n\n"
        + tables_md.rstrip()
        + "\n\n---\n\n"
        + discussion_md.rstrip()
        + "\n\n---\n\n"
        + limitations_md.rstrip()
        + "\n\n---\n\n"
        + references_md.rstrip()
        + "\n"
    )

    sr_id    = state.get("sr_id", "unknown")
    question = state.get("question", "Pregunta no definida")
    cochrane = state.get("cochrane_mode", False)

    timestamp = "—"
    try:
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y-%m-%d")
    except Exception:
        pass

    meta_block = (
        '<div class="axiom-meta">'
        f'<strong>Run ID:</strong> {_escape_html(sr_id)}<br/>'
        f'<strong>Research question:</strong> <em>{_escape_html(question)}</em><br/>'
        f'<strong>Date:</strong> {timestamp}<br/>'
        f'<strong>Methodology:</strong> {"PRISMA 2020 + Cochrane (RoB + GRADE)" if cochrane else "PRISMA 2020"}'
        '</div>'
    )

    pdf_path = _render_pdf(
        markdown_text=combined_md,
        output_path=REPORTS_DIR / f"Axiom_Report_{sr_id}.pdf",
        title="Axiom — Systematic Review Report",
        css=_CSS_UNIFIED,
        meta_block=meta_block,
    )

    logger.info(
        "writer_assembler: documento ensamblado (%d chars) | PDF=%s",
        len(combined_md),
        "✓" if pdf_path else "✗",
    )

    return {
        "executive_report_md":       combined_md,
        "executive_report_pdf_path": str(pdf_path) if pdf_path else None,
    }


# ────────────────────────────────────────────────────────────────────────────
# CSS unificado para el reporte final
# ────────────────────────────────────────────────────────────────────────────
# Combina el styling rich (tablas, código) del executive con la tipografía
# académica (serif, hanging indent en .references) del APA. Reemplaza
# _CSS_EXECUTIVE y _CSS_APA7 para el output unificado del Paso B; esos dos
# quedan en el archivo por si algún consumidor histórico los importa, pero
# el assembler usa solo este.
_CSS_UNIFIED = _CSS_BASE + """
/* Tipografía académica */
body { font-family: 'DejaVu Serif', 'Times New Roman', 'Times', serif; }
h1, h2, h3 { font-family: 'DejaVu Sans', 'Helvetica Neue', 'Arial', sans-serif; }

/* Prosa académica */
p { margin: 8px 0; text-align: justify; }
ul, ol { margin: 8px 0 8px 22px; }
li { margin: 4px 0; }

/* TABLAS: Estilo académico profesional */
table {
    border-collapse: collapse;
    width: 100%;
    margin: 16px 0 24px 0;
    font-size: 8.5pt; /* Reducido de 9.5pt para optimizar espacio y evitar desbordes */
}
th, td {
    border-bottom: 1px solid #e2e8f0;
    padding: 6px 8px;
    text-align: left;
    vertical-align: top;
    line-height: 1.3;
    word-break: break-word;     /* Fuerza el salto de línea en citas o textos largos */
    overflow-wrap: break-word;  /* Garantiza que el texto nunca empuje la celda fuera del margen */
}
th {
    background-color: #f4f6f8;
    font-weight: bold;
    color: #1a202c;
    border-bottom: 2px solid #cbd5e1;
    font-family: 'DejaVu Sans', sans-serif;
}
tr { page-break-inside: avoid; }
tr:nth-child(even) { background-color: #f8fafc; }

/* REFERENCIAS: APA 7 Sangría Francesa (Hanging Indent) */
.references {
    margin-top: 15px;
}
.reference-item {
    text-indent: -1.27cm; /* Tira la primera línea hacia la izquierda */
    padding-left: 1.27cm; /* Empuja todo el bloque hacia la derecha */
    margin-bottom: 16px;  /* Espacio real entre referencias */
    text-align: justify;
    line-height: 1.5;
    font-size: 10.5pt;
}

/* Separadores y énfasis */
hr { border: none; border-top: 1px solid #cbd5e0; margin: 24px 0; }
em, i { font-style: italic; }
strong { font-weight: 600; color: #1E3A8A; }
"""