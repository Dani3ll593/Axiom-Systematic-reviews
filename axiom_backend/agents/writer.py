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
from axiom_backend.tools.llm_router import LLM_32B, extract_json_from_response
from axiom_backend.prompts import WRITER_SYNTHESIS_PROMPT, WRITER_APA7_RULES

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
async def _call_qwq_with_retry(system_prompt: str, user_msg: str) -> "SynthesisOutput":
    """Llama al writer (synthesis) y, si la respuesta no parsea o no valida, reintenta una vez.

    QwQ-32B / DeepSeek-R1-Distill es errático: a veces ignora las tags
    <json>...</json> y emite prosa narrativa ("Alright, I've got this task..."),
    o un JSON con campos faltantes. Un solo reintento con temperatura distinta
    suele bastar — la varianza del modelo es alta entre llamadas aunque el
    prompt sea idéntico.

    Fix R1 (`content or reasoning`): Featherless separa el output del modelo
    en dos campos cuando el modelo es R1-style — el reasoning (incluido el
    bloque <json>) va a `message.reasoning` y `message.content` queda vacío.
    Sin este fallback, el parser ve "" y tira ValueError. Mismo fix aplicado
    en screener_32b.
    """
    last_err: Exception | None = None
    for attempt, temperature in ((1, 0.3), (2, 0.55)):
        try:
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
            return SynthesisOutput(**parsed_json)
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
async def writer_synthesis_node(state: AxiomState) -> dict:
    """Nodo 1/4 del writer: genera prosa académica cohesiva (synthesis_md).

    Input del state:
      - consensus_clusters, research_gaps, screened_papers, papers_excluded,
        question, sr_id.
    Output del state:
      - writer_synthesis_md: markdown con secciones Executive Summary,
        Synthesis of Findings, Discussion, Limitations, Future Research.

    NO produce PDF, NO produce references list, NO produce tablas. Esas
    salidas son responsabilidad de los nodos downstream del writer.
    """
    screened_papers = state.get("screened_papers", [])
    consensus       = state.get("consensus_clusters", [])
    gaps            = state.get("research_gaps", [])
    question        = state.get("question", "Pregunta no definida")

    # references_table sigue construyéndose aquí porque el synthesis necesita
    # las citas inline. La lista APA 7 completa la arma writer_references_node
    # con sus propios helpers; este dict es solo {paper_id: cita-short}.
    references_table = _build_references_table(screened_papers)

    # Condensar consensos al payload mínimo que el prompt necesita.
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

    output_language = _detect_language(question)
    logger.info("writer_synthesis: idioma detectado = %s", output_language)
    system_prompt = (
        WRITER_SYNTHESIS_PROMPT
        .replace("{apa7_rules_text}", WRITER_APA7_RULES)
        .replace("{output_language}", output_language)
    )

    user_msg = f"SYNTHESIS PAYLOAD:\n{json.dumps(payload, ensure_ascii=False)}"

    logger.info(
        "writer_synthesis: %d incluidos, %d gaps, %d clusters",
        len(screened_papers), len(gaps), len(consensus),
    )

    try:
        validated = await _call_qwq_with_retry(system_prompt, user_msg)
        logger.info(
            "writer_synthesis: ¡Prosa generada con éxito! (%d chars)",
            len(validated.synthesis_md),
        )
        return {"writer_synthesis_md": validated.synthesis_md}

    except ValidationError as e:
        logger.error("writer_synthesis: Pydantic validation error tras reintento: %s", e)
        return {
            "writer_synthesis_md": "",
            "errors": [{"node": "writer_synthesis", "error": f"validation_error: {e}"}],
        }
    except Exception as e:
        logger.exception("writer_synthesis: Fallo en la generación tras reintento")
        return {
            "writer_synthesis_md": "",
            "errors": [{"node": "writer_synthesis", "error": str(e)}],
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
    doi = (paper.get("doi") or "").strip()

    parts = [f"{authors} ({year}). {title}."]
    if journal:
        parts.append(f"*{journal}*.")
    if doi:
        # Normalizar a https://doi.org/... si vino en otro formato
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
async def writer_tables_node(state: AxiomState) -> dict:
    """Construye 3 tablas en Markdown:

      1. PRISMA Flow: encontrados / decididos / incluidos / excluidos / restringidos
      2. Resumen de clusters: una fila por cluster con claim, n, %, GRADE
      3. Papers por cluster: una fila por paper con cluster #, citación, posición, etc.

    Output: `writer_tables_md` (string Markdown con las 3 tablas).

    Sin LLM — los datos ya están estructurados en `consensus_clusters` y
    `extractions`. Generar esto en Python es determinístico, gratis y
    100% confiable.
    """
    papers_found    = state.get("papers_found", []) or []
    screened_papers = state.get("screened_papers", []) or []
    papers_excluded = state.get("papers_excluded", []) or []
    consensus       = state.get("consensus_clusters", []) or []
    extractions     = state.get("extractions", []) or []

    references_table = _build_references_table(screened_papers)
    prisma_flow = _build_prisma_flow(papers_found, screened_papers, papers_excluded)
    restricted = _build_restricted_list(screened_papers)

    # Lookup auxiliar: study_design por paper_id (desde extractions)
    design_by_pid = {
        e.get("paper_id"): (e.get("study_design") or "—")
        for e in extractions
    }

    # ── Tabla 1: PRISMA flow ───────────────────────────────────────────────
    flow_rows = [
        ["Records identified",       prisma_flow["found"]],
        ["Records screened",         prisma_flow["found"]],
        ["Records included",         prisma_flow["included"]],
        ["Records excluded",         prisma_flow["excluded_total"]],
        ["Restricted access papers", len(restricted)],
    ]
    flow_md = _md_table(["Stage", "n"], [[r[0], str(r[1])] for r in flow_rows])

    # Sub-tabla: exclusiones por razón (solo si hay datos)
    excl_by_reason = prisma_flow["excluded_by_reason"]
    excl_md = ""
    if excl_by_reason:
        excl_rows = [[r, str(n)] for r, n in sorted(
            excl_by_reason.items(), key=lambda kv: -kv[1]
        )]
        excl_md = "\n\n### Exclusion reasons\n\n" + _md_table(
            ["Reason", "n"], excl_rows,
        )

    # ── Tabla 2: resumen de clusters ───────────────────────────────────────
    cluster_header = [
        "Cluster", "Core claim", "n papers",
        "Agreement (%)", "Consensus level", "Heterogeneity", "GRADE",
    ]
    cluster_rows = []
    for i, c in enumerate(consensus, start=1):
        cluster_rows.append([
            f"C{i}",
            (c.get("core_claim") or "—"),
            str(c.get("total_papers_in_cluster") or "—"),
            str(c.get("agreement_percentage") or "—"),
            (c.get("consensus_level") or "—"),
            "Yes" if c.get("heterogeneity_detected") else "No",
            (c.get("grade_final_certainty") or "not assessed"),
        ])
    clusters_md = _md_table(cluster_header, cluster_rows) if cluster_rows else "_No clusters formed._"

    # ── Tabla 3: papers por cluster ────────────────────────────────────────
    paper_header = ["Cluster", "Citation", "Position", "Year", "Study design"]
    paper_rows = []
    for i, c in enumerate(consensus, start=1):
        # Cada paper aparece en una de tres categorías
        for pid in (c.get("supporting_papers") or []):
            paper_rows.append([
                f"C{i}",
                references_table.get(pid, pid),
                "Supports",
                _extract_year_from_citation(references_table.get(pid, "")),
                design_by_pid.get(pid, "—"),
            ])
        for pid in (c.get("contradicting_papers") or []):
            paper_rows.append([
                f"C{i}",
                references_table.get(pid, pid),
                "Contradicts",
                _extract_year_from_citation(references_table.get(pid, "")),
                design_by_pid.get(pid, "—"),
            ])
        for pid in (c.get("neutral_papers") or []):
            paper_rows.append([
                f"C{i}",
                references_table.get(pid, pid),
                "Neutral",
                _extract_year_from_citation(references_table.get(pid, "")),
                design_by_pid.get(pid, "—"),
            ])
    papers_md = _md_table(paper_header, paper_rows) if paper_rows else "_No papers in any cluster._"

    # ── Ensamble del bloque completo de tablas ─────────────────────────────
    tables_md = (
        "## PRISMA Flow\n\n"
        f"{flow_md}"
        f"{excl_md}\n\n"
        "## Cluster Summary\n\n"
        f"{clusters_md}\n\n"
        "## Papers by Cluster\n\n"
        f"{papers_md}\n"
    )

    logger.info(
        "writer_tables: 3 tablas generadas | flow=%d stages | clusters=%d | rows_papers=%d",
        len(flow_rows), len(cluster_rows), len(paper_rows),
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
    """Construye la lista de referencias APA 7 separada en dos secciones:

      - "References (included)": papers OA o accesibles que efectivamente
        formaron parte del análisis.
      - "References (restricted access)": papers relevantes pero detrás de
        paywall. Se listan para auditoría PRISMA.

    Ordenadas alfabéticamente por primer autor, formato hanging indent
    (aplicado en CSS via `.references p`).
    """
    screened = state.get("screened_papers", []) or []

    included    = [p for p in screened if p.get("is_open")]
    restricted  = [p for p in screened if not p.get("is_open")]

    def _sort_key(p: dict) -> str:
        """Primer autor en minúsculas para ordenar alfabéticamente."""
        authors = p.get("authors") or []
        if not authors:
            return "zzz"  # sin autor → al final
        return _last_name(authors[0]).lower()

    included.sort(key=_sort_key)
    restricted.sort(key=_sort_key)

    def _section(title: str, papers: list[dict]) -> str:
        if not papers:
            return f"## {title}\n\n_No papers in this category._\n"
        entries = "\n\n".join(_build_apa7_full_entry(p) for p in papers)
        # El wrapper <div class="references"> hace que el CSS aplique
        # hanging indent (text-indent: -1.27cm + padding-left: 1.27cm).
        return f'## {title}\n\n<div class="references">\n\n{entries}\n\n</div>\n'

    refs_md = (
        _section(f"References (included, n={len(included)})", included)
        + "\n"
        + _section(f"References — restricted access (n={len(restricted)})", restricted)
    )

    logger.info(
        "writer_references: %d incluidas, %d restringidas",
        len(included), len(restricted),
    )
    return {"writer_references_md": refs_md}


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
def _split_synthesis_for_assembly(synthesis_md: str) -> tuple[str, str]:
    """Divide synthesis en (early_sections, late_sections).

    early_sections  = Executive Summary + Synthesis of Findings
    late_sections   = Discussion + Limitations + Future Research

    El corte se hace en el primer `## Discussion` (o variante en español).
    Si no se encuentra, devolvemos todo el synthesis como `early` y un
    string vacío como `late` — las tablas terminarán al final.
    """
    if not synthesis_md:
        return "", ""

    # Patrones de corte en ambos idiomas
    cut_patterns = [
        r"\n##\s+Discussion\b",
        r"\n##\s+Discusión\b",
        r"\n##\s+Discussion and",
        r"\n##\s+Discusión y",
    ]
    for pattern in cut_patterns:
        match = re.search(pattern, synthesis_md, flags=re.IGNORECASE)
        if match:
            return synthesis_md[: match.start()], synthesis_md[match.start():]
    # No se encontró corte → todo es early, late vacío
    return synthesis_md, ""


async def writer_assembler_node(state: AxiomState) -> dict:
    """Concatena los 3 markdown intermedios y produce 1 PDF unificado.

    Output del state:
      - `executive_report_md`:        markdown completo del documento final
      - `executive_report_pdf_path`:  ruta al PDF generado (str | None)

    Si alguno de los markdown intermedios viene vacío (porque su nodo falló),
    se reemplaza por un placeholder marcado claramente para que el lector vea
    que esa sección falló pero el documento sigue siendo entregable.
    """
    synthesis_md  = state.get("writer_synthesis_md")  or ""
    tables_md     = state.get("writer_tables_md")     or ""
    references_md = state.get("writer_references_md") or ""

    # Placeholders para secciones que fallaron upstream
    if not synthesis_md.strip():
        synthesis_md = (
            "## Synthesis of Findings\n\n"
            "_⚠ Synthesis section unavailable — the synthesis node failed. "
            "See errors in the run log._\n"
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

    # Partir la prosa para intercalar las tablas
    synthesis_early, synthesis_late = _split_synthesis_for_assembly(synthesis_md)

    # Orden final
    combined_md = (
        synthesis_early.rstrip()
        + "\n\n---\n\n"
        + tables_md.rstrip()
        + "\n\n---\n\n"
        + (synthesis_late.rstrip() + "\n\n---\n\n" if synthesis_late else "")
        + references_md.rstrip()
        + "\n"
    )

    # Renderizar PDF
    sr_id = state.get("sr_id", "unknown")
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
/* Tipografía académica: serif para body, sans-serif para títulos */
body { font-family: 'DejaVu Serif', 'Times New Roman', 'Times', serif; }
h1, h2, h3 { font-family: 'DejaVu Sans', 'Helvetica Neue', 'Arial', sans-serif; }

/* Prosa: párrafos justificados, sin sangría (estilo manuscript moderno) */
p { margin: 8px 0; text-align: justify; }

/* Listas */
ul, ol { margin: 8px 0 8px 22px; }
li { margin: 4px 0; }

/* Tablas: estilo académico con borders limpios y headers destacados */
table {
    border-collapse: collapse;
    width: 100%;
    margin: 14px 0;
    font-size: 9.5pt;
    page-break-inside: auto;
}
th, td {
    border: 1px solid #cbd5e0;
    padding: 6px 9px;
    text-align: left;
    vertical-align: top;
}
th {
    background: #f1f5f9;
    font-weight: 600;
    color: #1E3A8A;
    font-family: 'DejaVu Sans', sans-serif;
}
tr { page-break-inside: avoid; }

/* Referencias: hanging indent estricto APA 7 */
.references p {
    text-indent: -1.27cm;
    padding-left: 1.27cm;
    margin-bottom: 8px;
    text-align: left;
}

/* Separadores entre secciones grandes (de los --- en el markdown) */
hr {
    border: none;
    border-top: 1px solid #cbd5e0;
    margin: 24px 0;
}

/* Énfasis (DOIs, journals italicizados) */
em { font-style: italic; }
strong { font-weight: 600; color: #1E3A8A; }
"""