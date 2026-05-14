"""
ui/screen_progress.py
─────────────────────
Screen 02 — Real-time pipeline progress.
"""

from __future__ import annotations
import streamlit as st

# Imports directos (NUNCA usar "from ui import ...")
from ui.components import render_header, render_footer, render_mock_badge
from utils.api_client import is_mock_mode, start_pipeline
from utils.pipeline_runner import run_pipeline_events
from utils.i18n import t

AGENTS = [
    ("searcher",   "agent.searcher",   "Qwen2.5-7B",      "agent.searcher.desc"),
    ("screener",   "agent.screener",   "Qwen2.5-7B (ft)", "agent.screener.desc"),
    ("extractor",  "agent.extractor",  "Qwen2.5-7B (ft)", "agent.extractor.desc"),
    ("analyst",    "agent.analyst",    "QwQ-32B",         "agent.analyst.desc"),
    ("gap_finder", "agent.gap_finder", "QwQ-32B",         "agent.gap_finder.desc"),
    ("writer",     "agent.writer",     "QwQ-32B",         "agent.writer.desc"),
]

def render_screen_progress() -> None:
    render_header(step_key="step.progress")

    if is_mock_mode():
        render_mock_badge()

    # --- 1. VALIDACIÓN DE SEGURIDAD ---
    payload = st.session_state.get("state_payload", {})
    if not payload and not is_mock_mode():
        st.warning(t("progress.warn.no_config", default="No hay configuración. Volviendo a la pantalla de inicio."))
        st.session_state.screen = "config"
        st.rerun()
        return

    # --- 2. INICIO DE RED ROBUSTO (Evitar Generador Zombie) ---
    if "current_run_id" not in st.session_state:
        try:
            st.session_state.current_run_id = start_pipeline(payload)
            st.session_state.event_history = []
            st.session_state.pipeline_finished = False
            st.session_state.kappa_value = None
            st.session_state.final_state_data = None
        except Exception as e:
            st.error(f"Error al conectar con el backend: {e}")
            if st.button("Volver", type="primary"):
                st.session_state.screen = "config"
                st.rerun()
            return

    # --- 3. CONTENEDORES DE UI (st.empty) ---
    with st.container():
        st.markdown('<div class="axiom-card">', unsafe_allow_html=True)
        agents_container = st.empty()
        kappa_container = st.empty()
        
        st.markdown(
            f'<div style="margin-top:20px; font-weight:bold; font-size:12px; color:var(--text-secondary);">'
            f'{t("progress.logs", default="PIPELINE LOGS")}</div>',
            unsafe_allow_html=True
        )
        logs_container = st.empty()
        st.markdown('</div>', unsafe_allow_html=True)

    action_container = st.empty()

    # --- 4. FUNCIONES DE DIBUJO ---
    def update_agents_ui(current_agent: str | None):
        html = '<div style="display:flex; flex-direction:column; gap:10px;">'
        agent_keys = [a[0] for a in AGENTS]
        try:
            current_idx = agent_keys.index(current_agent) if current_agent else -1
        except ValueError:
            current_idx = -1

        for idx, (key, label_key, model, desc_key) in enumerate(AGENTS):
            is_active = (key == current_agent)
            is_done = (current_idx > idx) or st.session_state.pipeline_finished
            
            color = "#38d9b4" if is_active else ("var(--text-secondary)" if is_done else "var(--text-muted)")
            weight = "bold" if is_active else "normal"
            spinner = "🔄 " if is_active else ("✅ " if is_done else "⏳ ")
            
            html += (
                f'<div style="display:flex; justify-content:space-between; align-items:center; padding:12px; border:1px solid rgba(77,166,255,0.1); border-radius:6px; background:rgba(0,0,0,0.2);">'
                f'<div>'
                f'<div style="color:{color}; font-weight:{weight}; font-size:14px; margin-bottom:4px;">{spinner} {t(label_key)}</div>'
                f'<div style="font-size:12px; color:var(--text-muted);">{t(desc_key)}</div>'
                f'</div>'
                f'<div style="font-family:Space Mono,monospace; font-size:11px; color:#4da6ff; background:rgba(77,166,255,0.1); padding:2px 6px; border-radius:4px;">{model}</div>'
                f'</div>'
            )
        html += '</div>'
        # Usamos .html() para evitar parpadeos y bugs de márgenes en markdown
        agents_container.html(html)

    def update_logs_ui():
        lines = []
        for ev in st.session_state.event_history[-8:]:
            if ev.type == "log" and ev.message:
                color = "#ff4d4d" if ev.level == "error" else ("#38d9b4" if ev.level == "success" else "var(--text-muted)")
                lines.append(f'<div style="font-family:Space Mono,monospace; font-size:11px; color:{color}; margin-bottom:4px;">> {ev.message}</div>')
        logs_html = f'<div style="background:#0e1117; padding:12px; border:1px solid #1f2937; border-radius:6px; height:180px; overflow-y:auto; box-shadow: inset 0 2px 4px rgba(0,0,0,0.5);">{"".join(lines)}</div>'
        logs_container.html(logs_html)

    def update_kappa_ui():
        if st.session_state.kappa_value is not None:
            kappa_container.html(
                f'<div class="axiom-card" style="padding:14px 16px; margin-top:16px; border-color:rgba(201,160,64,0.3); background:rgba(201,160,64,0.05);">'
                f'<div style="font-size:11px;font-family:Space Mono,monospace;color:#c9a040;letter-spacing:0.08em;">{t("progress.kappa.label", default="INTER-RATER RELIABILITY")}</div>'
                f'<div style="display:flex;align-items:baseline;gap:8px;margin-top:6px;">'
                f'<span style="font-size:36px;font-weight:700;color:#38d9b4;font-family:Space Mono,monospace;">{st.session_state.kappa_value}</span>'
                f'<span style="font-size:13px;color:var(--text-muted);">{t("progress.kappa.cohen", default="Cohen\'s Kappa Score")}</span>'
                f'</div></div>'
            )

    # --- 5. BUCLE DE CONSUMO (Fresco en cada reinicio de Streamlit) ---
    current_agent = "searcher" # Forzamos el estado Activo en el primero al iniciar
    update_agents_ui(current_agent)
    update_logs_ui()

    # Mapeo para traducir los nombres internos del backend a la interfaz
    NODE_TO_UI = {
        "searcher": "searcher",
        "screener": "screener",
        "extractor": "extractor",
        "analyst_7b": "analyst",
        "analyst_32b": "analyst",
        "gapfinder": "gap_finder",
        "writer": "writer"
    }

    try:
        from utils.pipeline_runner import PipelineEvent
        
        events_gen = run_pipeline_events(payload, run_id=st.session_state.current_run_id)
        
        for event in events_gen:
            # 1. Inyectar Log con el ID del proceso apenas inicia
            if event.type == "run_started":
                run_id = event.payload.get('run_id', 'Desconocido')
                log_msg = f"🚀 Pipeline iniciado exitosamente. Run ID: {run_id}"
                if not any(e.message == log_msg for e in st.session_state.event_history):
                    st.session_state.event_history.append(PipelineEvent(type="log", level="info", message=log_msg))

            if event not in st.session_state.event_history:
                st.session_state.event_history.append(event)
            
            # 2. Traducción y lógica de avance de los divs (Tarjetas)
            ui_agent = NODE_TO_UI.get(event.agent) if event.agent else None

            if event.type == "agent_done" and ui_agent:
                agent_keys = [a[0] for a in AGENTS]
                try:
                    idx = agent_keys.index(ui_agent)
                    # Si un agente terminó, encendemos el spinner del SIGUIENTE
                    if idx + 1 < len(agent_keys):
                        current_agent = agent_keys[idx + 1]
                    else:
                        current_agent = None
                except ValueError:
                    pass
                    
            elif event.type == "kappa":
                st.session_state.kappa_value = event.payload.get("value") if event.payload else None
            elif event.type == "final_state":
                st.session_state.final_state_data = event.payload
            elif event.type == "finished":
                st.session_state.pipeline_finished = True
                current_agent = None # Apaga todos los spinners al finalizar

            # Dibujar en vivo
            update_agents_ui(current_agent)
            update_logs_ui()
            update_kappa_ui()
            
    except Exception as e:
        # Si la red parpadea, Streamlit hará rerun y se reconectará al backend
        st.warning(f"Sincronizando con el servidor MI300X... ({e})")
        time.sleep(1)
        st.rerun()

    # --- 6. ACCIONES FINALES O CANCELAR ---
    with action_container.container():
        if st.session_state.pipeline_finished:
            if st.button(t("progress.cta.results", default="Ver Resultados Finales"), type="primary", use_container_width=True):
                # FIX: Empaquetar el estado y el run_id en la variable "results" que espera screen_results
                st.session_state.results = {
                    "final_state": st.session_state.final_state_data,
                    "run_id": st.session_state.current_run_id
                }
                st.session_state.screen = "results"
                st.rerun()
        else:
            if st.button(t("progress.cta.stop", default="Detener y Volver"), type="secondary", use_container_width=True):
                st.session_state.screen = "config"
                if "current_run_id" in st.session_state:
                    del st.session_state.current_run_id
                st.rerun()

    render_footer()


