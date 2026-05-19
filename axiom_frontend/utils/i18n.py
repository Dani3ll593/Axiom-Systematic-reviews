"""
utils/i18n.py
─────────────
Minimal i18n: dictionary-based string lookup with ES/EN.

Usage:
    from utils.i18n import t
    st.markdown(t("config.header_step"))

The current language is read from st.session_state.language (default "es").
Switching language is done by render_language_toggle() in components.py.

This module also exposes LOG_PATTERNS: a list of (regex, key_es, key_en)
used by screen_progress.py to translate backend log lines on the fly.
The patterns are tried in order; the first match wins. Captured groups
are passed as keyword args to .format() — use named groups for clarity.
If nothing matches, the original message is shown verbatim (failsafe).
"""

from __future__ import annotations
import re
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
               "interventions on academic burnout in graduate students in Latin America?"),
    },
    "config.word_count":         {"es": "{n}/{max} palabras",
                                  "en": "{n}/{max} words"},
    "config.section.criteria":   {"es": "Criterios avanzados (PICOS)",
                                  "en": "Advanced criteria (PICOS)"},
    "config.label.population":   {"es": "Población (P)",      "en": "Population (P)"},
    "config.label.intervention": {"es": "Intervención (I)",   "en": "Intervention (I)"},
    "config.label.comparison":   {"es": "Comparación (C)",    "en": "Comparison (C)"},
    "config.label.outcomes":     {"es": "Resultados (O)",     "en": "Outcomes (O)"},
    "config.label.study_design": {"es": "Diseños de estudio", "en": "Study designs"},
    "config.label.year_range":   {"es": "Rango de años",      "en": "Year range"},
    "config.label.year_from":    {"es": "Desde",              "en": "From"},
    "config.label.year_to":      {"es": "Hasta",              "en": "To"},
    "config.label.languages":    {"es": "Idiomas",            "en": "Languages"},
    "config.placeholder.p":      {"es": "ej. estudiantes de posgrado, LATAM",
                                  "en": "e.g. graduate students, LATAM"},
    "config.placeholder.i":      {"es": "ej. intervenciones basadas en mindfulness",
                                  "en": "e.g. mindfulness-based interventions"},
    "config.placeholder.c":      {"es": "ej. lista de espera / placebo activo",
                                  "en": "e.g. waitlist / active placebo"},
    "config.placeholder.o":      {"es": "ej. burnout, agotamiento emocional",
                                  "en": "e.g. burnout, emotional exhaustion"},
    "config.study.rct":          {"es": "RCT — Ensayos controlados aleatorizados",
                                  "en": "RCT — Randomized Controlled Trials"},
    "config.study.obs":          {"es": "Observacional (cohorte, caso-control)",
                                  "en": "Observational (cohort, case-control)"},
    "config.study.rev":          {"es": "Revisión sistemática / meta-análisis",
                                  "en": "Systematic review / meta-analysis"},
    "config.study.qual":         {"es": "Cualitativo (etnográfico, grounded theory)",
                                  "en": "Qualitative (ethnographic, grounded theory)"},
    "config.lang.english":       {"es": "Inglés",             "en": "English"},
    "config.lang.spanish":       {"es": "Español",            "en": "Spanish"},
    "config.lang.portuguese":    {"es": "Portugués",          "en": "Portuguese"},
    "config.section.methodology": {"es": "Metodología",       "en": "Methodology"},
    "config.label.cochrane":     {"es": "Modo Cochrane (Risk of Bias 2.0 + GRADE)",
                                  "en": "Cochrane Mode (Risk of Bias 2.0 + GRADE)"},
    "config.help.cochrane":      {"es": ("Activa la evaluación de Risk of Bias 2.0 por estudio y la calificación "
                                         "de certeza GRADE por cluster de evidencia. Suma aproximadamente "
                                         "5–10 minutos al run; usa el modelo de razonamiento (DeepSeek-R1)."),
                                  "en": ("Enables per-study Risk of Bias 2.0 assessment and per-cluster GRADE "
                                         "certainty rating. Adds roughly 5–10 minutes to the run; uses the "
                                         "reasoning model (DeepSeek-R1).")},
    "config.label.report_language": {"es": "Idioma del reporte",
                                     "en": "Report language"},
    "config.help.report_language":  {
        "es": ("Idioma en el que se generará el reporte final (resumen, discusión, "
               "limitaciones, justificaciones de RoB y GRADE). NO controla la interfaz; "
               "el toggle ES/EN del header sigue siendo para los botones y etiquetas. "
               "Con 'Auto' el backend lo detecta a partir de la pregunta."),
        "en": ("Language for the generated report (summary, discussion, limitations, "
               "RoB and GRADE rationales). Does NOT control the UI; the ES/EN toggle "
               "in the header continues to control labels and buttons. With 'Auto' the "
               "backend detects it from the question text."),
    },
    "config.report_lang.auto":    {"es": "Auto (según la pregunta)",
                                   "en": "Auto (from the question)"},
    "config.report_lang.en":      {"es": "Inglés", "en": "English"},
    "config.report_lang.es":      {"es": "Español", "en": "Spanish"},
    "config.sources_strip":      {"es": "Fuentes · arXiv · PubMed · OpenAlex · Scielo · Crossref",
                                  "en": "Sources · arXiv · PubMed · OpenAlex · Scielo · Crossref"},
    "config.cta.start":          {"es": "▶ Iniciar revisión sistemática",
                                  "en": "▶ Start systematic review"},
    "config.validation.empty":   {"es": "La pregunta de investigación es obligatoria.",
                                  "en": "Research question is required."},
    "config.validation.short":   {"es": "La pregunta es muy corta (mínimo {n} palabras).",
                                  "en": "The question is too short (minimum {n} words)."},
    "config.validation.year":    {"es": "El rango de años es inválido.",
                                  "en": "Invalid year range."},

    # ─── Screen 02 — Progress ─────────────────────────────────────
    "progress.warn.no_config":   {"es": "No se encontró configuración. Volviendo a la pantalla de inicio.",
                                  "en": "No configuration found. Returning to start screen."},
    "progress.logs":             {"es": "LOGS DEL PIPELINE", "en": "PIPELINE LOGS"},
    "progress.cta.stop":         {"es": "Detener y Volver",  "en": "Stop and Return"},
    "progress.cta.cancel":       {"es": "✕ Cancelar proceso", "en": "✕ Cancel process"},
    "progress.cta.cancelling":   {"es": "Cancelando…",       "en": "Cancelling…"},
    "progress.cta.results":      {"es": "Ver resultados finales", "en": "View final results"},
    "progress.kappa.label":      {"es": "INTER-RATER RELIABILITY", "en": "INTER-RATER RELIABILITY"},
    "progress.kappa.cohen":      {"es": "Puntaje κ de Cohen", "en": "Cohen's Kappa Score"},
    "progress.cancel.confirm":   {"es": ("¿Cancelar el proceso? El nodo en curso terminará su llamada actual "
                                         "pero no se ejecutarán los siguientes."),
                                  "en": ("Cancel the process? The running node will finish its current call "
                                         "but no further nodes will run.")},
    "progress.cancel.requested": {"es": "Cancelación solicitada — esperando confirmación del backend…",
                                  "en": "Cancellation requested — waiting for backend confirmation…"},
    "progress.cancel.done":      {"es": "Proceso cancelado.", "en": "Process cancelled."},
    "progress.cancel.failed":    {"es": "No se pudo cancelar en el backend: {err}",
                                  "en": "Backend cancellation failed: {err}"},
    "progress.run_started":      {"es": "🚀 Pipeline iniciado · Run ID: {run_id}",
                                  "en": "🚀 Pipeline started · Run ID: {run_id}"},

    # Agent labels (used in progress rows)
    "agent.searcher":            {"es": "Buscador", "en": "Searcher"},
    "agent.searcher.desc":       {"es": "arXiv · PubMed · Scielo · OpenAlex",
                                  "en": "arXiv · PubMed · Scielo · OpenAlex"},
    "agent.screener":            {"es": "Screener", "en": "Screener"},
    "agent.screener.desc":       {"es": "Cascada PRISMA · κ inter-evaluador",
                                  "en": "PRISMA cascade · inter-rater κ"},
    "agent.extractor":           {"es": "Extractor", "en": "Extractor"},
    "agent.extractor.desc":      {"es": "PyMuPDF → JSON schema",
                                  "en": "PyMuPDF → JSON schema"},
    "agent.rob":                 {"es": "Risk of Bias", "en": "Risk of Bias"},
    "agent.rob.desc":            {"es": "Cochrane RoB 2.0 · 5 dominios",
                                  "en": "Cochrane RoB 2.0 · 5 domains"},
    "agent.analyst7b":           {"es": "Analista 7B", "en": "Analyst 7B"},
    "agent.analyst7b.desc":      {"es": "Síntesis rápida por cluster",
                                  "en": "Fast per-cluster synthesis"},
    "agent.analyst32b":          {"es": "Analista 32B", "en": "Analyst 32B"},
    "agent.analyst32b.desc":     {"es": "Razonamiento profundo · contradicciones",
                                  "en": "Deep reasoning · contradictions"},
    "agent.grade":               {"es": "GRADE", "en": "GRADE"},
    "agent.grade.desc":          {"es": "Certeza de evidencia por cluster",
                                  "en": "Per-cluster evidence certainty"},
    "agent.gapfinder":           {"es": "Detector de Vacíos", "en": "Gap Finder"},
    "agent.gapfinder.desc":      {"es": "5 categorías · verificación OpenAlex",
                                  "en": "5 categories · OpenAlex verification"},
    "agent.writer":              {"es": "Redactor", "en": "Writer"},
    "agent.writer.desc":         {"es": "Síntesis + tablas + discusión + referencias",
                                  "en": "Synthesis + tables + discussion + references"},

    # ─── Screen 03 — Results ──────────────────────────────────────
    "results.summary.corpus":    {"es": "CORPUS", "en": "CORPUS"},
    "results.summary.included":  {"es": "INCLUIDOS", "en": "INCLUDED"},
    "results.summary.excluded":  {"es": "EXCLUIDOS", "en": "EXCLUDED"},
    "results.summary.restricted":{"es": "RESTRINGIDOS", "en": "RESTRICTED"},
    "results.summary.gaps":      {"es": "VACÍOS", "en": "GAPS"},
    "results.summary.kappa":     {"es": "INTER-RATER κ", "en": "INTER-RATER κ"},
    "results.summary.found":     {"es": "{n} encontrados", "en": "{n} found"},
    "results.summary.papers":    {"es": "{n} papers", "en": "{n} papers"},
    "results.tab.report":        {"es": "📄 Reporte ejecutivo", "en": "📄 Executive report"},
    "results.tab.gaps":          {"es": "🔍 Vacíos de investigación",
                                  "en": "🔍 Research gaps"},
    "results.tab.consensus":     {"es": "🧠 Consensos / Controversias",
                                  "en": "🧠 Consensus / Controversies"},
    "results.tab.restricted":    {"es": "⚠ Acceso restringido ({n})",
                                  "en": "⚠ Restricted access ({n})"},
    "results.tab.rob_grade":     {"es": "⚖ RoB & GRADE",
                                  "en": "⚖ RoB & GRADE"},
    "results.gap.label":         {"es": "VACÍO {i} · {category}",
                                  "en": "GAP {i} · {category}"},
    "results.gap.confirmed":     {"es": "Confirmado", "en": "Confirmed"},
    "results.gap.partial":       {"es": "Parcialmente abordado", "en": "Partially addressed"},
    "results.gap.rescued":       {"es": "Recuperado", "en": "Rescued"},
    "results.restricted.warn":   {"es": "⚠ {n} artículos no accesibles · metadata completa guardada",
                                  "en": "⚠ {n} inaccessible articles · full metadata saved"},
    "results.restricted.oa_yes": {"es": "OA disponible", "en": "OA available"},
    "results.restricted.oa_no":  {"es": "Sin OA", "en": "No OA"},
    "results.export.label":      {"es": "EXPORTAR", "en": "EXPORT"},
    "results.export.report_md":  {"es": "⬇ Reporte (Markdown)", "en": "⬇ Report (Markdown)"},
    "results.export.report_pdf": {"es": "⬇ Reporte (PDF)", "en": "⬇ Report (PDF)"},
    "results.export.pdf_save":   {"es": "📥 Guardar PDF", "en": "📥 Save PDF"},
    "results.export.pdf_loading": {"es": "Descargando PDF del backend…",
                                   "en": "Downloading PDF from backend…"},
    "results.export.pdf_not_ready": {"es": "El reporte PDF aún se está generando. Probá de nuevo en unos segundos.",
                                     "en": "The PDF report is still being generated. Try again in a few seconds."},
    "results.export.pdf_unavailable": {"es": "El backend no tiene un PDF disponible para este run.",
                                       "en": "The backend has no PDF available for this run."},
    "results.export.pdf_error":  {"es": "No se pudo descargar el PDF: {err}",
                                  "en": "Could not download the PDF: {err}"},
    "results.export.docx_coming": {"es": "⬇ Word (próximamente)",
                                   "en": "⬇ Word (coming soon)"},
    "results.export.docx_help":  {"es": "La exportación a Word estará disponible en una próxima versión.",
                                  "en": "Word export will be available in a future release."},
    "results.pipeline.label":    {"es": "PIPELINE", "en": "PIPELINE"},
    "results.pipeline.main_model": {"es": "Modelo principal", "en": "Main model"},
    "results.pipeline.extraction": {"es": "Extracción", "en": "Extraction"},
    "results.pipeline.embeddings": {"es": "Embeddings", "en": "Embeddings"},
    "results.pipeline.hardware": {"es": "Hardware", "en": "Hardware"},
    "results.pipeline.vector_store": {"es": "Vector store", "en": "Vector store"},
    "results.pipeline.orchestrator": {"es": "Orquestador", "en": "Orchestrator"},
    "results.cta.new_review":    {"es": "↻ Nueva revisión", "en": "↻ New review"},
    "results.cta.back_config":   {"es": "← Volver a configuración",
                                  "en": "← Back to configuration"},
    "results.error.no_results":  {"es": "No hay resultados para mostrar. Vuelve a iniciar el pipeline.",
                                  "en": "No results to show. Restart the pipeline."},
    "results.error.partial_errors": {"es": "El pipeline tuvo {n} errores no críticos",
                                    "en": "The pipeline had {n} non-critical errors"},

    # ─── RoB / GRADE tab ──────────────────────────────────────────
    "rg.grade.title":            {"es": "Calificación GRADE por cluster de evidencia",
                                  "en": "GRADE rating per evidence cluster"},
    "rg.grade.empty":            {"es": "No hay clusters con calificación GRADE.",
                                  "en": "No clusters with a GRADE rating."},
    "rg.grade.certainty":        {"es": "Certeza", "en": "Certainty"},
    "rg.grade.summary":          {"es": "Resumen", "en": "Summary"},
    "rg.grade.starting":         {"es": "Certeza inicial", "en": "Starting certainty"},
    "rg.grade.downgrades":       {"es": "Reducciones (downgrades)", "en": "Downgrades"},
    "rg.grade.upgrades":         {"es": "Aumentos (upgrades)", "en": "Upgrades"},
    "rg.grade.no_downgrades":    {"es": "Sin reducciones aplicadas.", "en": "No downgrades applied."},
    "rg.grade.no_upgrades":      {"es": "Sin aumentos aplicados.", "en": "No upgrades applied."},
    "rg.grade.details":          {"es": "Detalles GRADE", "en": "GRADE details"},
    "rg.cert.high":              {"es": "Alta", "en": "High"},
    "rg.cert.moderate":          {"es": "Moderada", "en": "Moderate"},
    "rg.cert.low":               {"es": "Baja", "en": "Low"},
    "rg.cert.very_low":          {"es": "Muy baja", "en": "Very Low"},
    "rg.cert.not_assessed":      {"es": "No evaluada", "en": "Not assessed"},
    "rg.rob.title":              {"es": "Risk of Bias 2.0 por estudio",
                                  "en": "Risk of Bias 2.0 per study"},
    "rg.rob.empty":              {"es": "No hay evaluaciones RoB disponibles.",
                                  "en": "No RoB assessments available."},
    "rg.rob.paper":              {"es": "Paper", "en": "Paper"},
    "rg.rob.overall":            {"es": "Global", "en": "Overall"},
    "rg.rob.d1":                 {"es": "D1 · Randomización",  "en": "D1 · Randomization"},
    "rg.rob.d2":                 {"es": "D2 · Desviaciones",   "en": "D2 · Deviations"},
    "rg.rob.d3":                 {"es": "D3 · Datos perdidos", "en": "D3 · Missing data"},
    "rg.rob.d4":                 {"es": "D4 · Medición",       "en": "D4 · Measurement"},
    "rg.rob.d5":                 {"es": "D5 · Reporte",        "en": "D5 · Reporting"},
    "rg.rob.judgment_low":       {"es": "Bajo", "en": "Low"},
    "rg.rob.judgment_some":      {"es": "Algunas dudas", "en": "Some concerns"},
    "rg.rob.judgment_high":      {"es": "Alto", "en": "High"},
    "rg.rob.judgment_na":        {"es": "N/A", "en": "N/A"},
    "rg.rob.details":            {"es": "Ver justificaciones", "en": "View rationales"},

    # ─── Backend log line patterns (translated via LOG_PATTERNS) ──
    # These are referenced from the LOG_PATTERNS table below. Keep the
    # placeholder names in sync with the named regex groups.
    "log.searcher.start":        {"es": "Iniciando búsqueda en {api}…",
                                  "en": "Starting search on {api}…"},
    "log.searcher.results":      {"es": "{api}: {n} resultados encontrados",
                                  "en": "{api}: {n} results found"},
    "log.searcher.querying":     {"es": "{api}: consultando API…",
                                  "en": "{api}: querying API…"},
    "log.searcher.paging":       {"es": "{api}: paginando consultas…",
                                  "en": "{api}: paginating queries…"},
    "log.searcher.timeout":      {"es": "{api}: timeout extendido a {s}s",
                                  "en": "{api}: timeout extended to {s}s"},
    "log.screener.included":     {"es": "Screener: {n} papers incluidos tras la cascada 7B → 32B",
                                  "en": "Screener: {n} papers included after the 7B → 32B cascade"},
    "log.extractor.confidence":  {"es": "Extractor: scores de confianza asignados — promedio {avg}",
                                  "en": "Extractor: confidence scores assigned — avg {avg}"},
    "log.extractor.done":        {"es": "Extractor: {n} papers extraídos con fragmento de fuente",
                                  "en": "Extractor: {n} papers extracted with source fragment"},
    "log.rob.start":             {"es": "RoB 2.0: evaluando {n} papers (concurrent={c}, timeout={t}s)…",
                                  "en": "RoB 2.0: assessing {n} papers (concurrent={c}, timeout={t}s)…"},
    "log.rob.done":              {"es": "RoB 2.0: {ok}/{n} evaluaciones completadas",
                                  "en": "RoB 2.0: {ok}/{n} assessments completed"},
    "log.analyst.embeddings":    {"es": "Analista: embeddings BGE-M3 (multilingüe)…",
                                  "en": "Analyst: BGE-M3 embeddings (multilingual)…"},
    "log.analyst.chroma":        {"es": "ChromaDB: {n} vectores indexados",
                                  "en": "ChromaDB: {n} vectors indexed"},
    "log.analyst.think":         {"es": "<think>: detección de contradicciones…",
                                  "en": "<think>: detecting contradictions…"},
    "log.analyst.done":          {"es": "Mapa consenso/controversia generado: {n} clústeres",
                                  "en": "Consensus/controversy map generated: {n} clusters"},
    "log.grade.start":           {"es": "GRADE: evaluando {n} clusters (concurrent={c}, timeout={t}s)…",
                                  "en": "GRADE: grading {n} clusters (concurrent={c}, timeout={t}s)…"},
    "log.grade.done":            {"es": "GRADE: {ok}/{n} clusters evaluados correctamente",
                                  "en": "GRADE: {ok}/{n} clusters graded successfully"},
    "log.gapfinder.start":       {"es": "Gap Finder: 5 categorías — verificación secundaria…",
                                  "en": "Gap Finder: 5 categories — secondary verification…"},
    "log.gapfinder.rejected":    {"es": "OpenAlex: {n} gaps rechazados (ya cubiertos)",
                                  "en": "OpenAlex: {n} gaps rejected (already covered)"},
    "log.gapfinder.done":        {"es": "Gap Finder: {n} vacíos confirmados",
                                  "en": "Gap Finder: {n} confirmed gaps"},
    "log.writer.start":          {"es": "Redactor: iniciando síntesis narrativa…",
                                  "en": "Writer: starting narrative synthesis…"},
    "log.writer.executive":      {"es": "Redactor: generando reporte ejecutivo en Markdown…",
                                  "en": "Writer: generating executive report in Markdown…"},
    "log.writer.references":     {"es": "Redactor: referencias formateadas (APA 7)",
                                  "en": "Writer: references formatted (APA 7)"},
    "log.writer.assembled":      {"es": "Redactor: documento ensamblado",
                                  "en": "Writer: document assembled"},
    "log.pipeline.done":         {"es": "Pipeline completado exitosamente.",
                                  "en": "Pipeline completed successfully."},
    "log.pipeline.connecting":   {"es": "Conectando con Axiom backend…",
                                  "en": "Connecting to Axiom backend…"},
    "log.pipeline.run_id":       {"es": "Run iniciado · id={run_id}…",
                                  "en": "Run started · id={run_id}…"},
    "log.pipeline.cancelled":    {"es": "Pipeline cancelado por el usuario.",
                                  "en": "Pipeline cancelled by user."},

    # ─── Per-agent status milestones (shown under each agent row) ─
    # Single-line ticker that replaces the old free-form log readout.
    # Three states per agent: waiting / active / done.
    "status.waiting":            {"es": "Esperando turno…", "en": "Waiting…"},
    "status.cancelled":          {"es": "Cancelado", "en": "Cancelled"},
    # searcher
    "status.searcher.active":    {"es": "Buscando en APIs académicas…",
                                  "en": "Searching academic APIs…"},
    "status.searcher.done":      {"es": "Encontrados {n} papers",
                                  "en": "Found {n} papers"},
    # screener
    "status.screener.active":    {"es": "Filtrando con cascada PRISMA…",
                                  "en": "Filtering with PRISMA cascade…"},
    "status.screener.done":      {"es": "Incluidos {included} · excluidos {excluded}",
                                  "en": "Included {included} · excluded {excluded}"},
    # extractor
    "status.extractor.active":   {"es": "Extrayendo datos de PDFs…",
                                  "en": "Extracting data from PDFs…"},
    "status.extractor.done":     {"es": "Extraídos {n} papers",
                                  "en": "Extracted {n} papers"},
    # rob_assessor (Cochrane)
    "status.rob.active":         {"es": "Evaluando Risk of Bias 2.0…",
                                  "en": "Assessing Risk of Bias 2.0…"},
    "status.rob.done":           {"es": "{n} evaluaciones completadas",
                                  "en": "{n} assessments completed"},
    # analyst_7b
    "status.analyst7b.active":   {"es": "Sintetizando con Qwen 2.5-7B…",
                                  "en": "Synthesizing with Qwen 2.5-7B…"},
    "status.analyst7b.done":     {"es": "{n} clusters sintetizados",
                                  "en": "{n} clusters synthesized"},
    # analyst_32b
    "status.analyst32b.active":  {"es": "Razonando con DeepSeek-R1…",
                                  "en": "Reasoning with DeepSeek-R1…"},
    "status.analyst32b.done":    {"es": "{n} clusters analizados en profundidad",
                                  "en": "{n} clusters deeply analyzed"},
    # grade_profiler (Cochrane)
    "status.grade.active":       {"es": "Calificando certeza GRADE…",
                                  "en": "Grading GRADE certainty…"},
    "status.grade.done":         {"es": "{ok}/{total} clusters calificados",
                                  "en": "{ok}/{total} clusters graded"},
    # gapfinder
    "status.gapfinder.active":   {"es": "Identificando vacíos de evidencia…",
                                  "en": "Identifying evidence gaps…"},
    "status.gapfinder.done":     {"es": "{n} vacíos confirmados",
                                  "en": "{n} confirmed gaps"},
    # writer
    "status.writer.active":      {"es": "Redactando reporte con Kimi-K2…",
                                  "en": "Drafting report with Kimi-K2…"},
    "status.writer.done":        {"es": "Reporte ensamblado",
                                  "en": "Report assembled"},
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

    Accepts a special kwarg `default` for callers that want a fallback string
    instead of "[key]" when the key is missing — used for incremental rollout
    of new keys without breaking the UI.
    """
    fallback = kwargs.pop("default", None)
    lang = get_language()
    entry = TRANSLATIONS.get(key)
    if entry is None:
        return fallback if fallback is not None else f"[{key}]"
    text = entry.get(lang) or entry.get(DEFAULT_LANGUAGE) or (fallback if fallback is not None else f"[{key}]")
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError):
            return text
    return text


# ─── Backend log translation table ──────────────────────────────────
# Each entry is (compiled_regex, i18n_key). The regex named groups become
# kwargs for t(). Patterns are tried in order; first match wins. If nothing
# matches, the original log line is shown verbatim — that's the failsafe.
#
# Conventions:
#   * Patterns target ANCHORED matches via re.match (start of string).
#   * Named groups MUST match the placeholders in the i18n key.
#   * Order matters when a string could match multiple patterns — put
#     the more specific one first.
LOG_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Frontend-emitted (pipeline_runner.py)
    (re.compile(r"^Conectando con Axiom backend"), "log.pipeline.connecting"),
    (re.compile(r"^Run iniciado\s*·\s*id=(?P<run_id>[^…]+)"), "log.pipeline.run_id"),
    (re.compile(r"^🚀 Pipeline iniciado.*Run ID:\s*(?P<run_id>\S+)"), "progress.run_started"),

    # Searcher
    (re.compile(r"^Iniciando búsqueda en (?P<api>[A-Za-z]+)"), "log.searcher.start"),
    (re.compile(r"^(?P<api>arXiv|PubMed|OpenAlex|Scielo|Crossref):\s*(?P<n>\d+)\s*resultados"),
        "log.searcher.results"),
    (re.compile(r"^(?P<api>PubMed|OpenAlex|Scielo|Crossref):\s*consultando"),
        "log.searcher.querying"),
    (re.compile(r"^(?P<api>OpenAlex|PubMed):\s*paginando"), "log.searcher.paging"),
    (re.compile(r"^(?P<api>Scielo|arXiv|PubMed|OpenAlex|Crossref):.*timeout.*?(?P<s>\d+)\s*s"),
        "log.searcher.timeout"),

    # Screener
    (re.compile(r"^Screener.*?(?P<n>\d+)\s*papers", re.IGNORECASE), "log.screener.included"),

    # Extractor
    (re.compile(r"^Confidence scores asignados.*?avg\s*(?P<avg>[\d.]+)"),
        "log.extractor.confidence"),
    (re.compile(r"^Extractor.*?(?P<n>\d+)\s*papers extra", re.IGNORECASE),
        "log.extractor.done"),

    # RoB
    (re.compile(r"^rob_assessor.*evaluando\s+(?P<n>\d+)\s+papers.*concurrent=(?P<c>\d+).*timeout=(?P<t>[\d.]+)"),
        "log.rob.start"),
    (re.compile(r"^rob_assessor:\s*(?P<ok>\d+)/(?P<n>\d+)\s*evaluaciones"),
        "log.rob.done"),

    # Analyst
    (re.compile(r"^Analista:\s*BGE-M3"), "log.analyst.embeddings"),
    (re.compile(r"^ChromaDB:\s*(?P<n>\d+)\s*vectores"), "log.analyst.chroma"),
    (re.compile(r"^.*<think>.*contradicciones", re.IGNORECASE), "log.analyst.think"),
    (re.compile(r"^Mapa consenso.*?(?P<n>\d+)\s*cl[uú]steres"), "log.analyst.done"),

    # GRADE
    (re.compile(r"^grade_profiler.*evaluando\s+(?P<n>\d+)\s+clusters.*concurrent=(?P<c>\d+).*timeout=(?P<t>[\d.]+)"),
        "log.grade.start"),
    (re.compile(r"^grade_profiler:\s*(?P<ok>\d+)/(?P<n>\d+)\s*clusters"),
        "log.grade.done"),

    # Gap finder
    (re.compile(r"^Gap Finder:\s*5 categorías"), "log.gapfinder.start"),
    (re.compile(r"^OpenAlex.*?(?P<n>\d+)\s*gaps rechazados"), "log.gapfinder.rejected"),
    (re.compile(r"^Gap Finder:\s*(?P<n>\d+)\s*vacíos"), "log.gapfinder.done"),

    # Writer
    (re.compile(r"^Redactor.*iniciando síntesis"), "log.writer.start"),
    (re.compile(r"^Generando reporte ejecutivo"), "log.writer.executive"),
    (re.compile(r"^.*referencias formateadas"), "log.writer.references"),
    (re.compile(r"^writer_assembler:\s*documento ensamblado"), "log.writer.assembled"),

    # Pipeline-level
    (re.compile(r"^Pipeline completado", re.IGNORECASE), "log.pipeline.done"),
    (re.compile(r"^Pipeline cancelado", re.IGNORECASE), "log.pipeline.cancelled"),
]


def translate_log(message: str) -> str:
    """Translate a backend log message using LOG_PATTERNS.

    Returns the original message verbatim if no pattern matches. Never raises:
    a malformed regex/format would surface as the original message.
    """
    if not message:
        return message
    for pattern, key in LOG_PATTERNS:
        m = pattern.match(message)
        if m:
            kwargs = m.groupdict()
            return t(key, **kwargs)
    return message
