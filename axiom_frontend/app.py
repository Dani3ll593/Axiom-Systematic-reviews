"""
app.py
──────
Axiom — AI-powered academic due diligence agent.
Streamlit entry point + 3-screen router.
"""

import streamlit as st

from ui import (
    inject_css,
    render_screen_config,
    render_screen_progress,
    render_screen_results,
)
from utils.api_client import is_mock_mode
from utils.i18n import t


st.set_page_config(
    page_title="Axiom — AI-Powered Academic Due Diligence",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed",
)

inject_css()

# ─── Initialize session state ───────────────────────────────────────
if "language" not in st.session_state:
    st.session_state.language = "es"

if "screen" not in st.session_state:
    st.session_state.screen = "config"

# ─── Top-level mock banner ──────────────────────────────────────────
if is_mock_mode():
    st.markdown(
        f'<div class="axiom-mock-banner">{t("common.mock_badge")}</div>',
        unsafe_allow_html=True,
    )

# ─── Route ──────────────────────────────────────────────────────────
SCREEN_ROUTER = {
    "config":   render_screen_config,
    "progress": render_screen_progress,
    "results":  render_screen_results,
}
render = SCREEN_ROUTER.get(st.session_state.screen, render_screen_config)
render()
