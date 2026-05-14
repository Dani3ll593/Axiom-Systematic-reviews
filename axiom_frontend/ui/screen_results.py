"""
ui/screen_results.py
────────────────────
Screen 03 — Results.
"""

from __future__ import annotations
from typing import Any
import json

import streamlit as st

from ui.components import render_header, render_footer, render_mock_badge
from utils.api_client import (
    is_mock_mode, fetch_report_pdf, fetch_apa7_pdf, PdfNotReady, PdfNotAvailable,
)
from utils.i18n import t


# ─── Mock final state (used when backend is offline) ────────────────
_MOCK_REPORT = """## 1. Estado del Campo

La evidencia disponible sobre el efecto de las intervenciones basadas en mindfulness (IBM) en el burnout académico de estudiantes de posgrado es heterogénea pero convergente en señalar un efecto moderado-positivo (d = 0.42–0.68, IC 95%). Los estudios en contexto latinoamericano representan el 18% del corpus incluido (n=16/87), con preponderancia de muestras mexicanas y brasileñas.

**Consenso parcial identificado:** la reducción del agotamiento emocional emerge como el dominio más consistentemente afectado, mientras que la despersonalización muestra alta heterogeneidad metodológica (I² > 75% en 4 de 6 meta-análisis).

## 2. Contradicciones Identificadas

| Papers en conflicto | Claims opuestos | Contexto |
|---|---|---|
| García-López et al. (2022) vs. Moreno & Silva (2023) | Efecto sostenido a 6 meses vs. degradación a 8 semanas | Distinto instrumento (MBI-SS vs. OLBI) |
| Chen et al. (2021) vs. Ribeiro (2024) | IBM-app superior a presencial vs. sin diferencia significativa | N=48 vs. N=312 |

## 3. Vacíos de Investigación

1. **[Poblacional]** Ausencia casi total de estudios en doctorado en humanidades y ciencias sociales LATAM — 91% del corpus es STEM o medicina.
2. **[Metodológico]** Ningún estudio aplica diseño doble-ciego con intervención activa de control.
3. **[Temporal]** Seguimiento máximo: 12 meses. Efectos a largo plazo no documentados.
4. **[Comparativo]** Falta de head-to-head IBM síncronas vs. asíncronas en educación remota post-pandemia.

## 4. Recomendaciones

La evidencia justifica un RCT con grupo control activo (psicoeducación), seguimiento de 18 meses, y estratificación por disciplina y modalidad de estudio. Priorizar muestras LATAM no-STEM cierra el gap poblacional más crítico.
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


# ─── PDF download helper ────────────────────────────────────────────
@st.cache_data(ttl=3600, show_spinner=False)
def _cached_report_pdf_bytes(run_id: str) -> bytes:
    """Cached wrapper around fetch_report_pdf. See _cached_apa7_pdf_bytes for notes."""
    return fetch_report_pdf(run_id)


@st.cache_data(ttl=3600, show_spinner=False)
def _cached_apa7_pdf_bytes(run_id: str) -> bytes:
    """Cached wrapper around fetch_apa7_pdf — avoids re-fetching on every
    rerun. Cached only on success; PdfNotReady / PdfNotAvailable propagate
    out of cache so the UI can show the right message and the next attempt
    can retry. TTL = 1h is well below the run-retention window."""
    return fetch_apa7_pdf(run_id)


def _render_pdf_download(
    run_id: str,
    sr_id: str,
    *,
    fetcher,                # callable: (run_id) -> bytes (one of _cached_*_pdf_bytes)
    filename_prefix: str,   # "Axiom_Report" or "Axiom_APA7"
    label_key: str,         # i18n key for the fetch button
    cache_suffix: str,      # disambiguates session_state keys per pdf type
) -> None:
    """Two-step UX shared by both PDFs (executive report and APA 7):
    a fetch button that pulls bytes from the backend, then a `download_button`
    that hands them to the browser. Cached so re-renders don't re-fetch.

    Both PDFs reuse the same i18n strings for transient states (loading /
    not-ready / unavailable / error) — only the fetch label differs.
    """
    filename  = f"{filename_prefix}_{sr_id or run_id[:8]}.pdf"
    cache_key = f"_pdf_bytes_{cache_suffix}_{run_id}"
    error_key = f"_pdf_error_{cache_suffix}_{run_id}"

    pdf_bytes = st.session_state.get(cache_key)
    err_msg   = st.session_state.get(error_key)

    if pdf_bytes is None:
        if st.button(t(label_key), use_container_width=True,
                     key=f"pdf_fetch_{cache_suffix}_{run_id}"):
            with st.spinner(t("results.export.pdf_loading")):
                try:
                    pdf_bytes = fetcher(run_id)
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
            key=f"pdf_save_{cache_suffix}_{run_id}",
        )

    if err_msg:
        level, text = err_msg
        (st.warning if level == "warn" else st.error)(text)


def _render_report_pdf_download(run_id: str, sr_id: str) -> None:
    """Executive report PDF — wrapper around _render_pdf_download."""
    _render_pdf_download(
        run_id=run_id,
        sr_id=sr_id,
        fetcher=_cached_report_pdf_bytes,
        filename_prefix="Axiom_Report",
        label_key="results.export.report_pdf",
        cache_suffix="report",
    )


def _render_apa7_pdf_download(run_id: str, sr_id: str) -> None:
    """APA 7 literature review PDF — wrapper around _render_pdf_download."""
    _render_pdf_download(
        run_id=run_id,
        sr_id=sr_id,
        fetcher=_cached_apa7_pdf_bytes,
        filename_prefix="Axiom_APA7",
        label_key="results.export.apa7_pdf",
        cache_suffix="apa7",
    )


def _resolve_state(results: dict[str, Any]) -> dict[str, Any]:
    """Return a normalized state dict regardless of source."""
    final = results.get("final_state") or {}
    stats = results.get("stats") or {}
    kappa = results.get("kappa")

    if final:
        return {
            "report_md":         final.get("report_md") or _MOCK_REPORT,
            "gaps":              final.get("gaps") or [],
            "restricted_papers": final.get("restricted_papers") or [],
            "stats": {
                "found":      final.get("stats", {}).get("found",      stats.get("found", 0)),
                "included":   final.get("stats", {}).get("included",   stats.get("included", 0)),
                "excluded":   final.get("stats", {}).get("excluded",   stats.get("excluded", 0)),
                "restricted": final.get("stats", {}).get("restricted", stats.get("restricted", 0)),
            },
            "kappa":     final.get("kappa", kappa),
            "apa_draft": final.get("apa_draft") or "",
            "sr_id":     final.get("sr_id") or "",
            "pdf_path":      final.get("executive_report_pdf_path"),
            "apa7_pdf_path": final.get("apa7_pdf_path"),
            "raw_json":  final,
        }

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
        "kappa":     kappa or 0.81,
        "apa_draft": "<draft placeholder — backend offline>",
        "sr_id":     "",
        "pdf_path":      None,
        "apa7_pdf_path": None,
        "raw_json":  None,
    }


def render_screen_results() -> None:
    render_header(step_key="step.results")

    if is_mock_mode():
        render_mock_badge()

    state = _resolve_state(st.session_state.get("results", {}))
    stats = state["stats"]

    summary_items = [
        (t("results.summary.corpus"),     f"{stats['found']} encontrados",     "#4da6ff"),
        (t("results.summary.included"),   f"{stats['included']} papers",       "#38d9b4"),
        (t("results.summary.excluded"),   f"{stats['excluded']} papers",       "var(--text-muted)"),
        (t("results.summary.restricted"), f"{stats['restricted']} papers",     "#d4aa5a"),
        (t("results.summary.kappa"),      f"{state['kappa']:.2f}" if isinstance(state['kappa'], (int, float)) else "—", "#a87ae8"),
        (t("results.summary.gaps"),       f"{len(state['gaps'])}",             "#38d9b4"),
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

    with main_col:
        tab_report, tab_gaps, tab_restricted = st.tabs([
            t("results.tab.report"),
            f"{t('results.tab.gaps')} ({len(state['gaps'])})",
            t("results.tab.restricted", n=stats['restricted']),
        ])

        with tab_report:
            st.markdown(state["report_md"])

        with tab_gaps:
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
                        f'border-radius:4px;letter-spacing:0.08em;">{t("results.gap.label", i=i, category=cat.upper())}</span>'
                        f'<p style="font-size:13px;color:var(--text-secondary);line-height:1.6;margin-top:8px;">{desc}</p>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )

        with tab_restricted:
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
                        f'padding:2px 8px;border-radius:4px;border:1px solid rgba(56,217,180,0.3);">{t("results.restricted.oa_yes")}</span>'
                        if oa_url
                        else f'<span style="font-size:11px;color:#f06070;background:rgba(240,96,112,0.08);'
                             f'padding:2px 8px;border-radius:4px;border:1px solid rgba(240,96,112,0.2);">{t("results.restricted.oa_no")}</span>'
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

    with side_col:
        st.markdown('<div class="axiom-card" style="padding:14px;">', unsafe_allow_html=True)
        st.markdown(
            f'<div style="font-family:Space Mono,monospace;font-size:10px;'
            f'color:var(--text-muted);letter-spacing:0.1em;margin-bottom:8px;">{t("results.export.label")}</div>',
            unsafe_allow_html=True,
        )

        st.download_button(
            label=t("results.export.report_md"),
            data=state["report_md"].encode("utf-8"),
            file_name="axiom_report.md",
            mime="text/markdown",
            use_container_width=True,
        )

        # PDF downloads — only when the backend is real AND a PDF was generated.
        # Mock mode hides both buttons (no run_id, no real PDFs on disk).
        run_id = (st.session_state.get("results") or {}).get("run_id")
        if not is_mock_mode() and run_id:
            if state.get("pdf_path"):
                _render_report_pdf_download(run_id, state.get("sr_id") or "")
            if state.get("apa7_pdf_path"):
                _render_apa7_pdf_download(run_id, state.get("sr_id") or "")

        st.download_button(
            label=t("results.export.apa7_txt"),
            data=(state["apa_draft"] or "").encode("utf-8"),
            file_name="axiom_draft_apa7.txt",
            mime="text/plain",
            use_container_width=True,
        )
        json_payload = state["raw_json"] or {
            "stats": state["stats"],
            "kappa": state["kappa"],
            "gaps": state["gaps"],
            "restricted_papers": state["restricted_papers"],
        }
        st.download_button(
            label=t("results.export.json"),
            data=json.dumps(json_payload, indent=2, ensure_ascii=False).encode("utf-8"),
            file_name="axiom_data.json",
            mime="application/json",
            use_container_width=True,
        )

        st.markdown('</div>', unsafe_allow_html=True)

        st.markdown(
            f'<div class="axiom-card" style="padding:14px;">'
            f'<div style="font-family:Space Mono,monospace;font-size:10px;color:var(--text-muted);letter-spacing:0.1em;margin-bottom:8px;">{t("results.pipeline.label")}</div>',
            unsafe_allow_html=True,
        )
        for k_label, v in [
            (t("results.pipeline.main_model"),    "QwQ-32B"),
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

    if st.button(t("results.cta.new_review"), use_container_width=True):
        for k in ("config", "state_payload", "results", "screen"):
            st.session_state.pop(k, None)
        st.session_state.screen = "config"
        st.rerun()

    render_footer()