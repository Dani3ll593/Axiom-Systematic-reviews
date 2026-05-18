"""
ui/screen_results.py
────────────────────
Screen 03 — Results.

Renders the adapted final_state from the backend:
  • report_md           — unified writer output (synthesis + tables + discussion + limitations + references)
  • gaps                — verified research gaps
  • restricted_papers   — paywalled papers that survived screening
  • stats               — corpus / included / excluded / restricted counts
  • kappa               — inter-rater agreement (currently always None — see backend Known Limitations)
  • cochrane_mode       — bool flag for whether RoB & GRADE tabs should appear
  • rob_assessments     — list of RoB 2.0 assessments per paper (Cochrane only)
  • consensus_clusters  — clusters enriched with grade_* fields (Cochrane only)

Mock fallback (_MOCK_REPORT, _MOCK_GAPS, _MOCK_RESTRICTED) is only used when
is_mock_mode() returns True. In real mode an empty/missing final_state is
treated as an error condition with a "back to config" CTA.

Downloads consolidated to a single PDF (the unified executive report),
plus a Markdown export and a disabled Word placeholder for a future release.
"""

from __future__ import annotations
from typing import Any, Iterable

import streamlit as st

from ui.components import render_header, render_footer, render_mock_badge
from utils.api_client import (
    is_mock_mode, fetch_report_pdf, PdfNotReady, PdfNotAvailable,
)
from utils.i18n import t


# ─── Mock final state (used ONLY when is_mock_mode() is True) ───────
_MOCK_REPORT = """## 1. Estado del Campo

La evidencia disponible sobre el efecto de las intervenciones basadas en mindfulness (IBM) en el burnout académico de estudiantes de posgrado es heterogénea pero convergente en señalar un efecto moderado-positivo (d = 0.42–0.68, IC 95%). Los estudios en contexto latinoamericano representan el 18% del corpus incluido (n=16/87).

**Consenso parcial identificado:** la reducción del agotamiento emocional emerge como el dominio más consistentemente afectado, mientras que la despersonalización muestra alta heterogeneidad metodológica (I² > 75%).

## 2. Vacíos de Investigación

1. **[Poblacional]** Ausencia casi total de estudios en doctorado en humanidades y ciencias sociales LATAM.
2. **[Metodológico]** Ningún estudio aplica diseño doble-ciego con intervención activa de control.
3. **[Temporal]** Seguimiento máximo: 12 meses.
4. **[Comparativo]** Falta de head-to-head IBM síncronas vs. asíncronas en educación remota.
"""

_MOCK_GAPS = [
    {"category": "Poblacional",  "color": "#4da6ff", "description": "Estudiantes de humanidades/CSOC en LATAM — 91% del corpus es STEM"},
    {"category": "Metodológico", "color": "#a87ae8", "description": "Ausencia de doble ciego con control activo en todos los estudios"},
    {"category": "Temporal",     "color": "#38d9b4", "description": "Seguimiento máximo 12 meses — efectos a largo plazo no documentados"},
    {"category": "Comparativo",  "color": "#d4aa5a", "description": "Sin head-to-head IBM síncrona vs. asíncrona en educación remota"},
]

_MOCK_RESTRICTED = [
    {"title": "Mindfulness-based stress reduction for doctoral students", "journal": "JAP",        "doi": "10.1037/apl0000581", "oa_url": None},
    {"title": "Burnout en posgrado latinoamericano: revisión sistemática", "journal": "Psych. Edu.", "doi": "10.5093/pe2023a12", "oa_url": "https://scielo.org"},
    {"title": "Cognitive outcomes of mindfulness in STEM graduate programs", "journal": "Nature HB",  "doi": "10.1038/s41562-022-0143", "oa_url": None},
]

_GAP_COLORS = ["#4da6ff", "#a87ae8", "#38d9b4", "#d4aa5a", "#f06070"]

# GRADE certainty → (color, i18n key)
_GRADE_COLORS = {
    "High":         ("#38d9b4", "rg.cert.high"),
    "Moderate":     ("#4da6ff", "rg.cert.moderate"),
    "Low":          ("#d4aa5a", "rg.cert.low"),
    "Very Low":     ("#f06070", "rg.cert.very_low"),
    "not_assessed": ("var(--text-muted)", "rg.cert.not_assessed"),
}

# RoB judgment → color
_ROB_JUDGMENT_COLORS = {
    "low":  "#38d9b4",
    "some": "#d4aa5a",
    "high": "#f06070",
    "n/a":  "var(--text-muted)",
}


# ─── PDF download helper ────────────────────────────────────────────
@st.cache_data(ttl=3600, show_spinner=False)
def _cached_report_pdf_bytes(run_id: str) -> bytes:
    """Cached wrapper around fetch_report_pdf — avoids re-fetching on every
    rerun. Cached only on success; PdfNotReady / PdfNotAvailable propagate
    out of cache so the UI shows the right message and the next attempt
    can retry."""
    return fetch_report_pdf(run_id)


def _render_report_pdf_download(run_id: str, sr_id: str) -> None:
    """Two-step UX: a fetch button that pulls bytes from the backend, then
    a download_button that hands them to the browser."""
    filename  = f"Axiom_Report_{sr_id or run_id[:8]}.pdf"
    cache_key = f"_pdf_bytes_report_{run_id}"
    error_key = f"_pdf_error_report_{run_id}"

    pdf_bytes = st.session_state.get(cache_key)
    err_msg   = st.session_state.get(error_key)

    if pdf_bytes is None:
        if st.button(t("results.export.report_pdf"), use_container_width=True,
                     key=f"pdf_fetch_report_{run_id}"):
            with st.spinner(t("results.export.pdf_loading")):
                try:
                    pdf_bytes = _cached_report_pdf_bytes(run_id)
                    st.session_state[cache_key] = pdf_bytes
                    st.session_state.pop(error_key, None)
                    st.rerun()
                except PdfNotReady:
                    st.session_state[error_key] = ("warn", t("results.export.pdf_not_ready"))
                    st.rerun()
                except PdfNotAvailable:
                    st.session_state[error_key] = ("error", t("results.export.pdf_unavailable"))
                    st.rerun()
                except Exception as e:  # noqa: BLE001 — surface anything else verbatim
                    st.session_state[error_key] = ("error", t("results.export.pdf_error", err=str(e)))
                    st.rerun()
    else:
        st.download_button(
            label=t("results.export.pdf_save"),
            data=pdf_bytes,
            file_name=filename,
            mime="application/pdf",
            use_container_width=True,
            key=f"pdf_save_report_{run_id}",
        )

    if err_msg:
        level, text = err_msg
        (st.warning if level == "warn" else st.error)(text)


# ─── State resolver ─────────────────────────────────────────────────
def _resolve_state(results: dict[str, Any]) -> dict[str, Any] | None:
    """Return a normalized state dict, or None if no real results are
    available in real mode.

    Mock fallback is only used when is_mock_mode() is True. In real mode
    an empty/missing final_state returns None — the caller renders an
    error and a back-to-config CTA.
    """
    final = results.get("final_state") or {}

    if final:
        return {
            "report_md":         final.get("report_md") or "",
            "gaps":              final.get("gaps") or [],
            "restricted_papers": final.get("restricted_papers") or [],
            "stats": {
                "found":      (final.get("stats") or {}).get("found", 0),
                "included":   (final.get("stats") or {}).get("included", 0),
                "excluded":   (final.get("stats") or {}).get("excluded", 0),
                "restricted": (final.get("stats") or {}).get("restricted", 0),
            },
            "kappa":              final.get("kappa"),
            "sr_id":              final.get("sr_id") or "",
            "pdf_path":           final.get("executive_report_pdf_path"),
            # Cochrane payload — empty/False when not Cochrane mode.
            "cochrane_mode":      bool(final.get("cochrane_mode", False)),
            "rob_assessments":    final.get("rob_assessments") or [],
            "consensus_clusters": final.get("consensus_clusters") or [],
        }

    # No real final_state. If mock mode, fall back to canned data; otherwise None.
    if is_mock_mode():
        stats = results.get("stats") or {}
        return {
            "report_md":         _MOCK_REPORT,
            "gaps":              _MOCK_GAPS,
            "restricted_papers": _MOCK_RESTRICTED,
            "stats": {
                "found":      stats.get("found",      312),
                "included":   stats.get("included",   87),
                "excluded":   stats.get("excluded",   225),
                "restricted": stats.get("restricted", 14),
            },
            "kappa":              results.get("kappa") or 0.81,
            "sr_id":              "",
            "pdf_path":           None,
            "cochrane_mode":      False,
            "rob_assessments":    [],
            "consensus_clusters": [],
        }
    return None


# ─── Render ─────────────────────────────────────────────────────────
def render_screen_results() -> None:
    render_header(step_key="step.results")

    if is_mock_mode():
        render_mock_badge()

    state = _resolve_state(st.session_state.get("results", {}))

    # No final_state in real mode → error + back to config.
    if state is None:
        st.error(t("results.error.no_results"))
        if st.button(t("results.cta.back_config"), type="primary"):
            for k in ("config", "state_payload", "results", "screen"):
                st.session_state.pop(k, None)
            st.session_state.screen = "config"
            st.rerun()
        render_footer()
        return

    stats = state["stats"]

    # ─── Summary bar ────────────────────────────────────────────────
    kappa_str = f"{state['kappa']:.2f}" if isinstance(state['kappa'], (int, float)) else "—"
    summary_items = [
        (t("results.summary.corpus"),     t("results.summary.found", n=stats['found']),  "#4da6ff"),
        (t("results.summary.included"),   t("results.summary.papers", n=stats['included']),   "#38d9b4"),
        (t("results.summary.excluded"),   t("results.summary.papers", n=stats['excluded']),   "var(--text-muted)"),
        (t("results.summary.restricted"), t("results.summary.papers", n=stats['restricted']), "#d4aa5a"),
        (t("results.summary.kappa"),      kappa_str, "#a87ae8"),
        (t("results.summary.gaps"),       f"{len(state['gaps'])}", "#38d9b4"),
    ]
    summary_html = "".join([
        f'<div style="flex:1;min-width:120px;display:flex;flex-direction:column;gap:4px;'
        f'padding:12px 16px;border-right:1px solid rgba(77,166,255,0.08);">'
        f'<span style="font-size:10px;font-family:Space Mono,monospace;color:var(--text-muted);'
        f'letter-spacing:0.06em;">{label}</span>'
        f'<span style="font-size:14px;font-weight:700;color:{color};">{value}</span>'
        f'</div>'
        for label, value, color in summary_items
    ])
    st.markdown(
        f'<div style="display:flex;flex-wrap:wrap;background:rgba(14,24,41,0.6);'
        f'border:1px solid rgba(77,166,255,0.12);border-radius:10px;overflow:hidden;margin-bottom:16px;">'
        f'{summary_html}</div>',
        unsafe_allow_html=True,
    )

    main_col, side_col = st.columns([3, 1])

    # ─── Main column — tabs ─────────────────────────────────────────
    with main_col:
        tab_labels = [
            t("results.tab.report"),
            f"{t('results.tab.gaps')} ({len(state['gaps'])})",
            t("results.tab.restricted", n=stats['restricted']),
        ]
        if state["cochrane_mode"]:
            tab_labels.append(t("results.tab.rob_grade"))

        tabs = st.tabs(tab_labels)

        # Tab 1 — report
        with tabs[0]:
            if state["report_md"].strip():
                st.markdown(state["report_md"])
            else:
                st.info(t("results.error.no_results"))

        # Tab 2 — gaps
        with tabs[1]:
            if not state["gaps"]:
                st.info(t("results.error.no_results"))
            else:
                for i, gap in enumerate(state["gaps"], 1):
                    color = gap.get("color") or _GAP_COLORS[(i - 1) % len(_GAP_COLORS)]
                    cat = gap.get("category", f"Gap {i}")
                    desc = gap.get("description", "")
                    st.markdown(
                        f'<div style="border-left:3px solid {color};padding:14px 16px;'
                        f'background:rgba(9,15,31,0.4);border-radius:0 8px 8px 0;margin-bottom:10px;">'
                        f'<span style="font-size:10px;font-family:Space Mono,monospace;color:{color};'
                        f'background:{color}18;border:1px solid {color}40;padding:2px 8px;'
                        f'border-radius:4px;letter-spacing:0.08em;">'
                        f'{t("results.gap.label", i=i, category=cat.upper())}</span>'
                        f'<p style="font-size:13px;color:var(--text-secondary);line-height:1.6;'
                        f'margin-top:8px;">{desc}</p>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )

        # Tab 3 — restricted
        with tabs[2]:
            if not state["restricted_papers"]:
                st.success("—")
            else:
                st.warning(t("results.restricted.warn", n=stats['restricted']))
                for paper in state["restricted_papers"]:
                    title   = paper.get("title", "—")
                    journal = paper.get("journal", "—")
                    doi     = paper.get("doi", "—")
                    oa_url  = paper.get("oa_url")
                    oa_badge = (
                        f'<span style="font-size:11px;color:#38d9b4;background:rgba(56,217,180,0.1);'
                        f'padding:2px 8px;border-radius:4px;border:1px solid rgba(56,217,180,0.3);">'
                        f'{t("results.restricted.oa_yes")}</span>'
                        if oa_url
                        else f'<span style="font-size:11px;color:#f06070;background:rgba(240,96,112,0.08);'
                             f'padding:2px 8px;border-radius:4px;border:1px solid rgba(240,96,112,0.2);">'
                             f'{t("results.restricted.oa_no")}</span>'
                    )
                    st.markdown(
                        f'<div style="background:rgba(9,15,31,0.5);border:1px solid rgba(77,166,255,0.1);'
                        f'border-radius:8px;padding:14px 16px;margin-bottom:10px;">'
                        f'<div style="font-size:13px;font-weight:500;color:var(--text-primary);margin-bottom:6px;">{title}</div>'
                        f'<div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;">'
                        f'<span style="font-family:Space Mono,monospace;font-size:11px;color:var(--text-muted);">{journal}</span>'
                        f'<span style="font-family:Space Mono,monospace;font-size:11px;color:var(--text-muted);">{doi}</span>'
                        f'{oa_badge}</div></div>',
                        unsafe_allow_html=True,
                    )

        # Tab 4 — RoB & GRADE (Cochrane only)
        if state["cochrane_mode"]:
            with tabs[3]:
                _render_rob_grade_tab(state["consensus_clusters"], state["rob_assessments"])

    # ─── Side column — exports + pipeline info ──────────────────────
    with side_col:
        st.markdown('<div class="axiom-card" style="padding:14px;">', unsafe_allow_html=True)
        st.markdown(
            f'<div style="font-family:Space Mono,monospace;font-size:10px;'
            f'color:var(--text-muted);letter-spacing:0.1em;margin-bottom:8px;">'
            f'{t("results.export.label")}</div>',
            unsafe_allow_html=True,
        )

        # 1) Unified PDF — only in real mode (mock has no run_id / no PDF on disk).
        run_id = (st.session_state.get("results") or {}).get("run_id")
        if not is_mock_mode() and run_id and state.get("pdf_path"):
            _render_report_pdf_download(run_id, state.get("sr_id") or "")

        # 2) Markdown — always works, content is in-memory.
        st.download_button(
            label=t("results.export.report_md"),
            data=(state["report_md"] or "").encode("utf-8"),
            file_name="axiom_report.md",
            mime="text/markdown",
            use_container_width=True,
        )

        # 3) Word — placeholder for a future release. Disabled button to make
        # the future feature visible without misleading the user.
        st.button(
            label=t("results.export.docx_coming"),
            disabled=True,
            use_container_width=True,
            help=t("results.export.docx_help"),
            key="docx_coming_soon",
        )

        st.markdown('</div>', unsafe_allow_html=True)

        # Pipeline metadata card
        st.markdown(
            f'<div class="axiom-card" style="padding:14px;">'
            f'<div style="font-family:Space Mono,monospace;font-size:10px;color:var(--text-muted);'
            f'letter-spacing:0.1em;margin-bottom:8px;">{t("results.pipeline.label")}</div>',
            unsafe_allow_html=True,
        )
        for k_label, v in [
            (t("results.pipeline.main_model"),    "DeepSeek-R1"),
            (t("results.pipeline.extraction"),    "Qwen2.5-7B (ft)"),
            (t("results.pipeline.embeddings"),    "BGE-M3"),
            (t("results.pipeline.hardware"),      "AMD MI300X"),
            (t("results.pipeline.vector_store"),  "ChromaDB"),
            (t("results.pipeline.orchestrator"),  "LangGraph (backend)"),
        ]:
            st.markdown(
                f'<div style="display:flex;justify-content:space-between;padding:5px 0;'
                f'border-bottom:1px solid rgba(77,166,255,0.06);font-size:11px;">'
                f'<span style="color:var(--text-muted);">{k_label}</span>'
                f'<span style="font-family:Space Mono,monospace;color:var(--text-secondary);">{v}</span>'
                f'</div>',
                unsafe_allow_html=True,
            )
        st.markdown('</div>', unsafe_allow_html=True)

    # ─── Bottom CTA ─────────────────────────────────────────────────
    if st.button(t("results.cta.new_review"), use_container_width=True):
        for k in ("config", "state_payload", "results", "screen"):
            st.session_state.pop(k, None)
        st.session_state.screen = "config"
        st.rerun()

    render_footer()


# ─── RoB & GRADE tab ────────────────────────────────────────────────
def _render_rob_grade_tab(
    consensus_clusters: list[dict],
    rob_assessments: list[dict],
) -> None:
    """Two sections: GRADE certainty per cluster, then RoB per paper.

    Both gracefully degrade — empty lists show an info message rather than
    breaking the layout. Designed to handle the partial-failure case where
    some clusters / papers have grade_final_certainty=not_assessed or RoB
    assessments are missing.
    """
    # ─── GRADE section ──────────────────────────────────────────────
    st.markdown(
        f'<div style="font-size:14px;font-weight:600;color:var(--text-primary);'
        f'margin:8px 0 12px;">{t("rg.grade.title")}</div>',
        unsafe_allow_html=True,
    )

    if not consensus_clusters:
        st.info(t("rg.grade.empty"))
    else:
        for idx, cluster in enumerate(consensus_clusters, 1):
            certainty = cluster.get("grade_final_certainty") or "not_assessed"
            color, cert_key = _GRADE_COLORS.get(certainty, _GRADE_COLORS["not_assessed"])
            cert_label = t(cert_key)
            claim = cluster.get("core_claim") or f"Cluster {idx}"
            summary = cluster.get("grade_summary") or "—"

            st.markdown(
                f'<div style="border:1px solid rgba(77,166,255,0.12);border-radius:10px;'
                f'background:rgba(9,15,31,0.4);padding:14px 16px;margin-bottom:10px;">'
                f'<div style="display:flex;align-items:center;gap:10px;margin-bottom:8px;">'
                f'<span style="font-size:10px;font-family:Space Mono,monospace;'
                f'color:{color};background:{color}18;border:1px solid {color}55;'
                f'padding:3px 10px;border-radius:4px;letter-spacing:0.08em;">'
                f'{t("rg.grade.certainty").upper()} · {cert_label.upper()}</span>'
                f'<span style="font-size:10px;font-family:Space Mono,monospace;'
                f'color:var(--text-muted);">#{idx}</span>'
                f'</div>'
                f'<div style="font-size:13px;color:var(--text-primary);font-weight:500;'
                f'line-height:1.5;margin-bottom:6px;">{claim}</div>'
                f'<div style="font-size:12px;color:var(--text-secondary);line-height:1.6;">{summary}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

            with st.expander(t("rg.grade.details"), expanded=False):
                starting = cluster.get("grade_starting_certainty") or "—"
                downgrades = cluster.get("grade_downgrades") or []
                upgrades   = cluster.get("grade_upgrades") or []

                st.markdown(f"**{t('rg.grade.starting')}**: `{starting}`")

                st.markdown(f"**{t('rg.grade.downgrades')}**")
                if not downgrades:
                    st.caption(t("rg.grade.no_downgrades"))
                else:
                    for d in downgrades:
                        sev = d.get("severity", "—")
                        factor = d.get("factor", "—")
                        rationale = d.get("rationale", "")
                        sev_color = {"none": "#38d9b4", "serious": "#d4aa5a", "very_serious": "#f06070"}.get(sev, "#888")
                        st.markdown(
                            f'<div style="font-size:12px;margin:4px 0;">'
                            f'<code style="color:{sev_color};">{factor}</code> · '
                            f'<em style="color:var(--text-muted);">{sev}</em> — '
                            f'<span style="color:var(--text-secondary);">{rationale}</span></div>',
                            unsafe_allow_html=True,
                        )

                st.markdown(f"**{t('rg.grade.upgrades')}**")
                if not upgrades:
                    st.caption(t("rg.grade.no_upgrades"))
                else:
                    for u in upgrades:
                        factor = u.get("factor", "—")
                        rationale = u.get("rationale", "")
                        st.markdown(
                            f'<div style="font-size:12px;margin:4px 0;">'
                            f'<code style="color:#38d9b4;">{factor}</code> — '
                            f'<span style="color:var(--text-secondary);">{rationale}</span></div>',
                            unsafe_allow_html=True,
                        )

    # ─── RoB section ────────────────────────────────────────────────
    st.markdown(
        f'<div style="font-size:14px;font-weight:600;color:var(--text-primary);'
        f'margin:24px 0 12px;">{t("rg.rob.title")}</div>',
        unsafe_allow_html=True,
    )

    if not rob_assessments:
        st.info(t("rg.rob.empty"))
        return

    # Compact summary table (Markdown table for fast skim)
    domain_keys = [
        ("D1", "domain_1_randomization"),
        ("D2", "domain_2_deviations"),
        ("D3", "domain_3_missing_data"),
        ("D4", "domain_4_outcome_meas"),
        ("D5", "domain_5_reporting"),
    ]

    header = (
        f"| {t('rg.rob.paper')} | {t('rg.rob.d1')} | {t('rg.rob.d2')} | "
        f"{t('rg.rob.d3')} | {t('rg.rob.d4')} | {t('rg.rob.d5')} | {t('rg.rob.overall')} |\n"
        "|---|---|---|---|---|---|---|"
    )
    rows = [header]
    for r in rob_assessments:
        paper_id = r.get("paper_id", "—")
        cells = [f"`{paper_id}`"]
        for _label, key in domain_keys:
            j = (r.get(key) or {}).get("judgment", "—")
            cells.append(_rob_dot(j))
        overall_j = (r.get("overall") or {}).get("judgment", "—")
        cells.append(_rob_dot(overall_j, with_label=True))
        rows.append("| " + " | ".join(cells) + " |")
    st.markdown("\n".join(rows))

    # Per-paper rationale expanders
    for r in rob_assessments:
        paper_id = r.get("paper_id", "—")
        overall_j = (r.get("overall") or {}).get("judgment", "—")
        title = f"`{paper_id}` · {t('rg.rob.overall')}: {_rob_label(overall_j)}"
        with st.expander(title, expanded=False):
            for label, key in domain_keys:
                d = r.get(key) or {}
                judgment = d.get("judgment", "—")
                rationale = d.get("rationale", "")
                st.markdown(
                    f'<div style="font-size:12px;margin:6px 0;">'
                    f'<strong>{label}</strong> · {_rob_dot(judgment, with_label=True)}<br>'
                    f'<span style="color:var(--text-muted);font-style:italic;">{rationale}</span>'
                    f'</div>',
                    unsafe_allow_html=True,
                )


def _rob_dot(judgment: str, *, with_label: bool = False) -> str:
    """Render an inline dot for a RoB judgment. with_label=True also adds the
    localized label after the dot — used for overall column and expanders."""
    color = _ROB_JUDGMENT_COLORS.get(judgment, "var(--text-muted)")
    dot = (
        f'<span style="display:inline-block;width:10px;height:10px;border-radius:50%;'
        f'background:{color};vertical-align:middle;"></span>'
    )
    if with_label:
        return f"{dot} {_rob_label(judgment)}"
    return dot


def _rob_label(judgment: str) -> str:
    """Map a RoB judgment to its localized label. Falls through to the raw
    judgment string when the value is unknown."""
    return {
        "low":  t("rg.rob.judgment_low"),
        "some": t("rg.rob.judgment_some"),
        "high": t("rg.rob.judgment_high"),
        "n/a":  t("rg.rob.judgment_na"),
    }.get(judgment, judgment)