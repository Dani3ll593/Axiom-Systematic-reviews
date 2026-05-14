"""
ui/screen_config.py
───────────────────
Screen 01 — Research question + (optional) PICOS criteria.
"""

from __future__ import annotations
import streamlit as st

from ui.components import render_header, render_chips, render_footer, render_mock_badge
from utils.api_client import is_mock_mode
from utils.form_to_state import map_form_to_initial_state
from utils.validators import validate_research_query, validate_year_range
from utils.i18n import t


def render_screen_config() -> None:
    render_header(step_key="step.config")

    if is_mock_mode():
        render_mock_badge()

    # Stack badges
    render_chips([
        ("QwQ-32B", "blue"),
        ("Qwen2.5-7B", "gold"),
        ("BGE-M3", "teal"),
        ("AMD MI300X", "violet"),
    ])

    st.markdown('<div class="axiom-card">', unsafe_allow_html=True)

    # Research question
    st.markdown(
        f'<div class="axiom-section-label">'
        f'<span class="axiom-section-num">01</span>'
        f'<span class="axiom-section-title">{t("config.section.question")}</span>'
        f'</div>',
        unsafe_allow_html=True,
    )

    query = st.text_area(
        label=t("config.section.question"),
        label_visibility="collapsed",
        placeholder=t("config.placeholder.question"),
        height=140,
        key="query_input",
    )

    word_count = len(query.split()) if query.strip() else 0
    st.caption(f"`{t('config.word_count', n=word_count, max=200)}`")

    # Advanced PICOS
    with st.expander(t("config.section.criteria"), expanded=False):
        col_p, col_i = st.columns(2)
        with col_p:
            st.markdown(f"**{t('config.label.population')}**")
            population = st.text_input(
                "P", label_visibility="collapsed",
                placeholder="ej. estudiantes de posgrado, LATAM",
                key="picos_p",
            )
        with col_i:
            st.markdown(f"**{t('config.label.intervention')}**")
            intervention = st.text_input(
                "I", label_visibility="collapsed",
                placeholder="ej. mindfulness-based interventions",
                key="picos_i",
            )

        col_c, col_o = st.columns(2)
        with col_c:
            st.markdown(f"**{t('config.label.comparison')}**")
            comparator = st.text_input(
                "C", label_visibility="collapsed",
                placeholder="ej. lista de espera / placebo activo",
                key="picos_c",
            )
        with col_o:
            st.markdown(f"**{t('config.label.outcomes')}**")
            outcomes = st.text_input(
                "O", label_visibility="collapsed",
                placeholder="ej. burnout, agotamiento emocional",
                key="picos_o",
            )

        st.divider()

        col_studies, col_filters = st.columns(2)

        with col_studies:
            st.markdown(f"**{t('config.label.study_design')}**")
            study_rct  = st.checkbox("RCT — Randomized Controlled Trials", value=True)
            study_obs  = st.checkbox("Observacional (cohorte, caso-control)", value=True)
            study_rev  = st.checkbox("Revisión sistemática / meta-análisis", value=False)
            study_qual = st.checkbox("Cualitativo (etnográfico, grounded theory)", value=False)

        with col_filters:
            st.markdown(f"**{t('config.label.year_range')}**")
            yr1, yr2 = st.columns(2)
            year_from = yr1.number_input(t("config.label.year_from"), min_value=1990, max_value=2026, value=2018, key="yr_from")
            year_to   = yr2.number_input(t("config.label.year_to"), min_value=1990, max_value=2026, value=2025, key="yr_to")

            st.markdown(f"**{t('config.label.languages')}**")
            lc1, lc2, lc3 = st.columns(3)
            lang_en = lc1.checkbox("English",   value=True)
            lang_es = lc2.checkbox("Español",   value=True)
            lang_pt = lc3.checkbox("Português", value=True)

    # Sources
    st.markdown(
        f'<div style="margin-top:12px; font-family:Space Mono,monospace; '
        f'font-size:11px; color:var(--text-muted);">{t("config.sources_strip")}</div>',
        unsafe_allow_html=True,
    )

    st.markdown('</div>', unsafe_allow_html=True)

    if st.button(t("config.cta.start"), use_container_width=True, type="primary"):
        ok, err_key, ctx = validate_research_query(query)
        if not ok:
            st.error(t(err_key, **ctx))
            return
        ok2, err_key2, ctx2 = validate_year_range(year_from, year_to)
        if not ok2:
            st.error(t(err_key2, **ctx2))
            return

        # ─── Mapping de UI a literales que el backend espera ──────────
        # Los checkboxes de study_types → strings que el screener prompt
        # usa literalmente. NO cambiar estos literales sin actualizar
        # también prisma_criteria_template.json y screener_prompt.txt.
        study_design_include: list[str] = []
        if study_rct:  study_design_include.append("randomized controlled trial")
        if study_obs:
            # "Observacional" cubre dos diseños distintos en PRISMA
            study_design_include.extend(["cohort study", "case-control"])
        if study_rev:  study_design_include.append("systematic review")
        if study_qual: study_design_include.append("qualitative")

        # Idiomas: nombres canónicos que el backend reconoce (no códigos ISO)
        languages: list[str] = []
        if lang_en: languages.append("English")
        if lang_es: languages.append("Spanish")
        if lang_pt: languages.append("Portuguese")

        # Los inputs P/I/C/O son text_inputs simples; permitimos al usuario
        # separar múltiples items con coma o punto y coma. Si en el futuro
        # se cambian por chips reales, esta función pasa a ser identity.
        def _split_csv(s: str) -> list[str]:
            return [x.strip() for x in (s or "").replace(";", ",").split(",") if x.strip()]

        # form: shape que map_form_to_initial_state consume directamente.
        form = {
            "question": query,
            "population_include":   _split_csv(population),
            "intervention_include": _split_csv(intervention),
            "comparison_include":   _split_csv(comparator),
            "outcomes_primary":     _split_csv(outcomes),
            "study_design_include": study_design_include,
            "year_min": int(year_from),
            "year_max": int(year_to),
            "languages": languages,
        }

        # st.session_state.config conserva el dict del form (útil para
        # screen_progress que muestra "CONSULTA: <query>" en el header).
        # state_payload es lo que se manda al backend.
        st.session_state.config        = form
        st.session_state.state_payload = map_form_to_initial_state(form)
        st.session_state.screen        = "progress"
        st.rerun()

    render_footer()
