"""
ui/components.py
────────────────
Reusable HTML fragments rendered via st.markdown(unsafe_allow_html=True).
Keep these tiny — the heavy styling lives in assets/style.css.
"""

from __future__ import annotations
from pathlib import Path
import streamlit as st

from utils.i18n import t


_CSS_LOADED_KEY = "_axiom_css_loaded"


def inject_css() -> None:
    """Inject the Axiom stylesheet on every rerun."""
    css_path = Path(__file__).parent.parent / "assets" / "style.css"
    if css_path.exists():
        st.markdown(f"<style>{css_path.read_text()}</style>", unsafe_allow_html=True)


def render_language_toggle() -> None:
    """Render an ES/EN toggle. Persists to st.session_state.language."""
    current = st.session_state.get("language", "es")
    col_es, col_en = st.columns(2, gap="small")

    with col_es:
        if st.button(
            "🇪🇸 ES",
            key="lang_btn_es",
            use_container_width=True,
            type="primary" if current == "es" else "secondary",
        ):
            if current != "es":
                st.session_state.language = "es"
                st.rerun()

    with col_en:
        if st.button(
            "🇬🇧 EN",
            key="lang_btn_en",
            use_container_width=True,
            type="primary" if current == "en" else "secondary",
        ):
            if current != "en":
                st.session_state.language = "en"
                st.rerun()


def render_header(step_key: str = "step.config") -> None:
    """Brand header with current pipeline step badge and language toggle."""
    st.markdown(
        f"""
        <div class="axiom-header">
          <svg width="32" height="36" viewBox="0 0 40 44">
            <polygon points="20,2 38,42 2,42" stroke="#c9a040" stroke-width="2.5" fill="none"/>
            <polygon points="20,10 30,34 10,34" stroke="#4da6ff" stroke-width="1.5" fill="rgba(77,166,255,0.08)"/>
            <line x1="20" y1="2" x2="20" y2="42" stroke="#c9a040" stroke-width="1" opacity="0.5"/>
            <circle cx="20" cy="22" r="3" fill="#c9a040"/>
          </svg>
          <div>
            <div class="axiom-brand">AXIOM</div>
            <div class="axiom-tagline">{t("common.tagline")}</div>
          </div>
          <div class="axiom-step-badge">{t(step_key)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Language toggle — right-aligned in a narrow column
    _, _, col_toggle = st.columns([6, 1, 1])
    with col_toggle:
        render_language_toggle()


def render_chip(label: str, color: str = "blue") -> str:
    """Return an HTML chip string. Caller is responsible for st.markdown."""
    return f'<span class="axiom-chip axiom-chip-{color}">{label}</span>'


def render_chips(chips: list[tuple[str, str]]) -> None:
    """Render a row of chips. Each tuple is (label, color)."""
    html = "".join(render_chip(l, c) for l, c in chips)
    st.markdown(html, unsafe_allow_html=True)


def render_stat_card(label: str, value: str | int, color: str = "#4da6ff") -> str:
    """Return a stat card HTML string."""
    return f"""
    <div class="axiom-stat">
      <div class="axiom-stat-value" style="color:{color}">{value}</div>
      <div class="axiom-stat-label">{label}</div>
    </div>
    """


def render_footer() -> None:
    st.markdown(
        f'<div class="axiom-footer">{t("common.footer")}</div>',
        unsafe_allow_html=True,
    )


def render_mock_badge() -> None:
    """Show a small banner indicating the UI is running on the canned mock pipeline."""
    st.markdown(
        f'<div class="axiom-mock-badge">'
        f'<span class="axiom-mock-dot"></span>'
        f'{t("common.mock_badge")}'
        f'</div>',
        unsafe_allow_html=True,
    )
