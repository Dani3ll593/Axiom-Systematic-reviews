"""
utils/i18n.py
─────────────
Minimal i18n: dictionary-based string lookup with ES/EN.

Usage:
    from utils.i18n import t
    st.markdown(t("config.header_step"))

The current language is read from st.session_state.language (default "es").
Switching language is done by render_language_toggle() in components.py.
"""

from __future__ import annotations
import streamlit as st


# ─── Translation dictionary ─────────────────────────────────────────
# Convention: keys are dotted paths "screen.element.subelement".
# Add new keys at the bottom of the relevant section, never reorder
# existing keys (they may be referenced from anywhere).
TRANSLATIONS: dict[str, dict[str, str]] = {
    # ─── Common ───────────────────────────────────────────────────
    "common.app_title":          {"es": "Axiom — Auditoría Académica con IA",
                                  "en": "Axiom — AI-Powered Academic Due Diligence"},
    "common.tagline":            {"es": "AUDITORÍA ACADÉMICA CON IA",
                                  "en": "AI-POWERED ACADEMIC DUE DILIGENCE"},
    "common.footer":             {"es": "AXIOM · AMD MI300X HACKATHON · vLLM + LangGraph + QwQ-32B",
                                  "en": "AXIOM · AMD MI300X HACKATHON · vLLM + LangGraph + QwQ-32B"},
    "common.mock_badge":         {"es": "⚡ MODO DEMO · datos simulados · sin conexión al backend",
                                  "en": "⚡ DEMO MODE · simulated data · backend not connected"},

    # ─── Step badges ──────────────────────────────────────────────
    "step.config":               {"es": "01 / CONFIGURACIÓN",
                                  "en": "01 / CONFIGURATION"},
    "step.progress":             {"es": "02 / PIPELINE",
                                  "en": "02 / PIPELINE"},
    "step.results":              {"es": "03 / RESULTADOS",
                                  "en": "03 / RESULTS"},

    # ─── Screen 01 — Config ───────────────────────────────────────
    "config.section.question":   {"es": "Pregunta de investigación",
                                  "en": "Research question"},
    "config.placeholder.question": {
        "es": ("Describe tu pregunta en lenguaje natural. Sé específico sobre población, "
               "intervención, resultado y contexto. Ej.: ¿Cuál es el efecto de las intervenciones "
               "basadas en mindfulness en el burnout académico de estudiantes de posgrado en LATAM?"),
        "en": ("Describe your question in natural language. Be specific about population, "
               "intervention, outcome and context. e.g., What is the effect of mindfulness-based "
               "interventions on academic burnout in graduate students in Latin America?")},
    "config.word_count":         {"es": "{n} palabras / {max} máx",
                                  "en": "{n} words / {max} max"},
    "config.section.criteria":   {"es": "⚙ Criterios PRISMA de inclusión",
                                  "en": "⚙ PRISMA inclusion criteria"},
    "config.label.year_range":   {"es": "Rango de años",
                                  "en": "Year range"},
    "config.label.year_from":    {"es": "Desde",
                                  "en": "From"},
    "config.label.year_to":      {"es": "Hasta",
                                  "en": "To"},
    "config.label.languages":    {"es": "Idiomas",
                                  "en": "Languages"},
    "config.label.domain":       {"es": "Dominio / Área",
                                  "en": "Domain / Area"},
    "config.label.advanced":     {"es": "▼ Modo avanzado (criterios PICOS completos)",
                                  "en": "▼ Advanced mode (full PICOS criteria)"},
    "config.label.population":          {"es": "Población", "en": "Population"},
    "config.label.intervention":        {"es": "Intervención", "en": "Intervention"},
    "config.label.comparison":          {"es": "Comparación", "en": "Comparison"},
    "config.label.outcomes":            {"es": "Resultados", "en": "Outcomes"},
    "config.label.outcomes_primary":    {"es": "Primarios", "en": "Primary"},
    "config.label.outcomes_secondary":  {"es": "Secundarios", "en": "Secondary"},
    "config.label.study_design":        {"es": "Diseño de estudio", "en": "Study design"},
    "config.label.publication_status":  {"es": "Estado de publicación", "en": "Publication status"},
    "config.label.include":      {"es": "Incluir", "en": "Include"},
    "config.label.exclude":      {"es": "Excluir", "en": "Exclude"},
    "config.sources_strip":      {"es": "FUENTES: arXiv · PubMed · Scielo · OpenAlex",
                                  "en": "SOURCES: arXiv · PubMed · Scielo · OpenAlex"},
    "config.cta.start":          {"es": "▶ Iniciar revisión sistemática",
                                  "en": "▶ Start systematic review"},
    "config.error.empty_query":  {"es": "La consulta está vacía. Ingresa una pregunta de investigación.",
                                  "en": "The query is empty. Enter a research question."},
    "config.error.too_short":    {"es": "La consulta es muy corta ({n} palabras). Mínimo {min}.",
                                  "en": "The query is too short ({n} words). Minimum {min}."},
    "config.error.too_long":     {"es": "Tu consulta tiene {n} palabras. Límite sugerido: {max}.",
                                  "en": "Your query has {n} words. Suggested limit: {max}."},
    "config.error.bad_year_range": {"es": "El año inicial ({y1}) no puede ser mayor que el final ({y2}).",
                                    "en": "The starting year ({y1}) cannot be greater than the ending year ({y2})."},

    # ─── Screen 02 — Progress ─────────────────────────────────────
    "progress.label.query":         {"es": "CONSULTA", "en": "QUERY"},
    "progress.stat.found":          {"es": "ENCONTRADOS", "en": "FOUND"},
    "progress.stat.included":       {"es": "INCLUIDOS", "en": "INCLUDED"},
    "progress.stat.excluded":       {"es": "EXCLUIDOS", "en": "EXCLUDED"},
    "progress.stat.restricted":     {"es": "RESTRINGIDOS", "en": "RESTRICTED"},
    "progress.status.starting":     {"es": "⏳ Pipeline iniciado...",
                                     "en": "⏳ Pipeline started..."},
    "progress.status.running":      {"es": "▶ {agent} en ejecución...",
                                     "en": "▶ {agent} running..."},
    "progress.status.complete":     {"es": "✓ Pipeline completado",
                                     "en": "✓ Pipeline complete"},
    "progress.kappa.label":         {"es": "FIABILIDAD INTER-EVALUADOR",
                                     "en": "INTER-RATER RELIABILITY"},
    "progress.kappa.cohen":         {"es": "κ (Cohen)", "en": "κ (Cohen)"},
    "progress.kappa.substantial":   {"es": "SUSTANCIAL", "en": "SUBSTANTIAL"},
    "progress.cta.results":         {"es": "→ Ver resultados",
                                     "en": "→ View results"},
    "progress.warn.no_config":      {"es": "No hay configuración. Volviendo a la pantalla de inicio.",
                                     "en": "No configuration found. Returning to start screen."},
    "progress.logs":                {"es": "LOGS DEL PIPELINE", "en": "PIPELINE LOGS"},
    "progress.cta.stop":            {"es": "Detener y Volver", "en": "Stop and Return"},

    # Agent labels (used in progress bars)
    "agent.searcher":           {"es": "Buscador", "en": "Searcher"},
    "agent.searcher.desc":      {"es": "arXiv · PubMed · Scielo · OpenAlex",
                                 "en": "arXiv · PubMed · Scielo · OpenAlex"},
    "agent.screener":           {"es": "Screener", "en": "Screener"},
    "agent.screener.desc":      {"es": "Cascada PRISMA · κ inter-evaluador",
                                 "en": "PRISMA cascade · inter-rater κ"},
    "agent.extractor":          {"es": "Extractor", "en": "Extractor"},
    "agent.extractor.desc":     {"es": "PyMuPDF → JSON schema",
                                 "en": "PyMuPDF → JSON schema"},
    "agent.analyst":            {"es": "Analista", "en": "Analyst"},
    "agent.analyst.desc":       {"es": "BGE-M3 · consenso / controversia",
                                 "en": "BGE-M3 · consensus / controversy"},
    "agent.gap_finder":         {"es": "Detector de Vacíos", "en": "Gap Finder"},
    "agent.gap_finder.desc":    {"es": "5 categorías · verificación OpenAlex",
                                 "en": "5 categories · OpenAlex verification"},
    "agent.writer":             {"es": "Redactor", "en": "Writer"},
    "agent.writer.desc":        {"es": "Reporte ejecutivo · Borrador APA 7",
                                 "en": "Executive report · APA 7 draft"},

    # ─── Screen 03 — Results ──────────────────────────────────────
    "results.summary.corpus":       {"es": "CORPUS", "en": "CORPUS"},
    "results.summary.included":     {"es": "INCLUIDOS", "en": "INCLUDED"},
    "results.summary.excluded":     {"es": "EXCLUIDOS", "en": "EXCLUDED"},
    "results.summary.restricted":   {"es": "RESTRINGIDOS", "en": "RESTRICTED"},
    "results.summary.gaps":         {"es": "VACÍOS", "en": "GAPS"},
    "results.summary.kappa":        {"es": "INTER-RATER κ", "en": "INTER-RATER κ"},
    "results.tab.report":           {"es": "📄 Reporte ejecutivo",
                                     "en": "📄 Executive report"},
    "results.tab.gaps":             {"es": "🔍 Vacíos de investigación",
                                     "en": "🔍 Research gaps"},
    "results.tab.consensus":        {"es": "🧠 Consensos / Controversias",
                                     "en": "🧠 Consensus / Controversies"},
    "results.tab.restricted":       {"es": "⚠ Acceso restringido ({n})",
                                     "en": "⚠ Restricted access ({n})"},
    "results.gap.label":            {"es": "VACÍO {i} · {category}",
                                     "en": "GAP {i} · {category}"},
    "results.gap.confirmed":        {"es": "Confirmado",
                                     "en": "Confirmed"},
    "results.gap.partial":          {"es": "Parcialmente abordado",
                                     "en": "Partially addressed"},
    "results.gap.rescued":          {"es": "Recuperado",
                                     "en": "Rescued"},
    "results.restricted.warn":      {"es": "⚠ {n} artículos no accesibles · metadata completa guardada",
                                     "en": "⚠ {n} inaccessible articles · full metadata saved"},
    "results.restricted.oa_yes":    {"es": "OA disponible", "en": "OA available"},
    "results.restricted.oa_no":     {"es": "Sin OA", "en": "No OA"},
    "results.export.label":         {"es": "EXPORTAR", "en": "EXPORT"},
    "results.export.report_md":     {"es": "⬇ Reporte (Markdown)",
                                     "en": "⬇ Report (Markdown)"},
    "results.export.report_pdf":    {"es": "⬇ Reporte (PDF)",
                                     "en": "⬇ Report (PDF)"},
    "results.export.apa7_pdf":      {"es": "⬇ Reporte APA 7 (PDF)",
                                     "en": "⬇ APA 7 Report (PDF)"},
    "results.export.pdf_save":      {"es": "📥 Guardar PDF",
                                     "en": "📥 Save PDF"},
    "results.export.pdf_loading":   {"es": "Descargando PDF del backend…",
                                     "en": "Downloading PDF from backend…"},
    "results.export.pdf_not_ready": {"es": "El reporte PDF aún se está generando. Probá de nuevo en unos segundos.",
                                     "en": "The PDF report is still being generated. Try again in a few seconds."},
    "results.export.pdf_unavailable": {"es": "El backend no tiene un PDF disponible para este run.",
                                       "en": "The backend has no PDF available for this run."},
    "results.export.pdf_error":     {"es": "No se pudo descargar el PDF: {err}",
                                     "en": "Could not download the PDF: {err}"},
    "results.export.apa7_txt":      {"es": "⬇ Borrador APA 7 (TXT)",
                                     "en": "⬇ APA 7 draft (TXT)"},
    "results.export.json":          {"es": "⬇ Datos completos (JSON)",
                                     "en": "⬇ Full data (JSON)"},
    "results.pipeline.label":       {"es": "PIPELINE", "en": "PIPELINE"},
    "results.pipeline.main_model":  {"es": "Modelo principal", "en": "Main model"},
    "results.pipeline.extraction":  {"es": "Extracción", "en": "Extraction"},
    "results.pipeline.embeddings":  {"es": "Embeddings", "en": "Embeddings"},
    "results.pipeline.hardware":    {"es": "Hardware", "en": "Hardware"},
    "results.pipeline.vector_store":{"es": "Vector store", "en": "Vector store"},
    "results.pipeline.orchestrator":{"es": "Orquestador", "en": "Orchestrator"},
    "results.cta.new_review":       {"es": "↻ Nueva revisión",
                                     "en": "↻ New review"},
    "results.error.no_results":     {"es": "No hay resultados para mostrar. Vuelve a iniciar el pipeline.",
                                     "en": "No results to show. Restart the pipeline."},
    "results.error.partial_errors": {"es": "El pipeline tuvo {n} errores no críticos",
                                     "en": "The pipeline had {n} non-critical errors"},
}


# ─── Public helper ──────────────────────────────────────────────────
DEFAULT_LANGUAGE = "es"


def get_language() -> str:
    """Read current language from session state. Default 'es' if not set."""
    return st.session_state.get("language", DEFAULT_LANGUAGE)


def t(key: str, **kwargs) -> str:
    """Translate a key to the current language; format with kwargs if any.

    Returns the key itself (in brackets) if not found — useful for catching
    missing translations during development. Never raises.
    """
    lang = get_language()
    entry = TRANSLATIONS.get(key)
    if entry is None:
        return f"[{key}]"
    text = entry.get(lang) or entry.get(DEFAULT_LANGUAGE) or f"[{key}]"
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError):
            return text
    return text
