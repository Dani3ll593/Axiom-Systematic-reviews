"""
ui/screen_progress.py
─────────────────────
Screen 02 — Real-time pipeline progress.

Renders up to 9 agent rows (7 standard + 2 Cochrane). Each row shows the
agent's gif (from assets/gifs/), its label/description, model badge, and
a state indicator (⏳ pending · 🔄 active · ✅ done). The 2 Cochrane rows
(rob_assessor, grade_profiler) are hidden when state_payload["cochrane_mode"]
is False.

Cancellation is cooperative: clicking "Cancel" POSTs to the backend's
/pipeline/{run_id}/cancel endpoint. The currently-running node finishes
its in-flight LLM call (cannot be interrupted mid-network-call), then the
graph stops. The UI navigates back to config as soon as the POST returns
2xx — we don't wait for the backend `cancelled` SSE event to keep the UX
snappy.

There's an inherent Streamlit limitation: the events loop blocks the
script, so the cancel button click only registers after the current SSE
iteration completes (worst case ~15s due to the heartbeat interval).
For active runs events arrive every few seconds, so the typical wait
is much shorter.
"""

from __future__ import annotations
import time
from pathlib import Path

import httpx
import streamlit as st

# Imports directos (NUNCA usar "from ui import ...")
from ui.components import render_header, render_footer, render_mock_badge
from utils.api_client import is_mock_mode, start_pipeline, cancel_pipeline, CancelFailed
from utils.pipeline_runner import run_pipeline_events, PipelineEvent
from utils.i18n import t, translate_log


# ─── Agent registry ─────────────────────────────────────────────────
# Each entry: (ui_key, label_i18n_key, model_chip, desc_i18n_key, gif_filename, cochrane_only)
# ui_key is what NODE_TO_UI maps backend node names to.
AGENTS: list[tuple[str, str, str, str, str, bool]] = [
    ("searcher",       "agent.searcher",   "Qwen2.5-7B",        "agent.searcher.desc",   "searcher2.gif", False),
    ("screener",       "agent.screener",   "Qwen2.5-7B → R1",   "agent.screener.desc",   "screener.gif",  False),
    ("extractor",      "agent.extractor",  "Qwen2.5-7B (ft)",   "agent.extractor.desc",  "extractor.gif", False),
    ("rob_assessor",   "agent.rob",        "DeepSeek-R1",       "agent.rob.desc",        "robb.gif",      True),
    ("analyst_7b",     "agent.analyst7b",  "Qwen2.5-7B",        "agent.analyst7b.desc",  "7b.gif",        False),
    ("analyst_32b",    "agent.analyst32b", "DeepSeek-R1",       "agent.analyst32b.desc", "32b.gif",       False),
    ("grade_profiler", "agent.grade",      "DeepSeek-R1",       "agent.grade.desc",      "grade.gif",     True),
    ("gapfinder",      "agent.gapfinder",  "DeepSeek-R1",       "agent.gapfinder.desc",  "gap.gif",       False),
    ("writer",         "agent.writer",     "Kimi-K2",           "agent.writer.desc",     "writer.gif",    False),
]

# Backend node names → UI agent keys. The collapsing here is intentional:
# screener_7b/_32b appear as one row, the 6 writer sub-nodes as one, etc.
NODE_TO_UI = {
    "searcher":          "searcher",
    "screener_7b":       "screener",
    "screener_32b":      "screener",
    "screener":          "screener",
    "extractor":         "extractor",
    "rob_assessor":      "rob_assessor",
    "analyst_7b":        "analyst_7b",
    "analyst_32b":       "analyst_32b",
    "grade_profiler":    "grade_profiler",
    "gapfinder":         "gapfinder",
    "writer_synthesis":  "writer",
    "writer_discussion": "writer",
    "writer_limitations":"writer",
    "writer_tables":     "writer",
    "writer_references": "writer",
    "writer_assembler":  "writer",
    "writer":            "writer",
    # Internal nodes — explicitly mapped to None so we never count them
    # as a UI agent transition.
    "clusterer":         None,
    "reconciler":        None,
}


# ─── Gif loader ─────────────────────────────────────────────────────
# Streamlit static file serving (habilitado en .streamlit/config.toml con
# `enableStaticServing = true`) sirve cualquier archivo que viva en el
# directorio `static/` del frontend bajo la URL `/app/static/<path>`. Esto
# reemplaza el approach anterior de embeber los gifs en base64 dentro del
# HTML de cada update, que generaba payloads websocket de ~2.4 MB y
# desincronizaba la conexión ("Bad message format · Tried to use SessionInfo
# before it was initialized"). Ahora el payload es ~3 KB y el browser
# cachea los gifs una sola vez.
#
# Los archivos viven en `axiom_frontend/static/gifs/`. La función _gif_url
# devuelve la ruta relativa que el browser puede pedir directamente.

# Resuelvo qué gifs existen una sola vez al cargar el módulo (no en cada
# render), porque Path.exists() es muy barato pero `streamlit run` reejecuta
# este archivo en cada interacción. Cachear es prolijo y barato.
_STATIC_GIFS_DIR = Path(__file__).parent.parent / "static" / "gifs"


@st.cache_data(show_spinner=False)
def _gif_exists(filename: str) -> bool:
    """¿Existe el gif en static/gifs/? Cached para no chequear filesystem
    en cada redraw."""
    return (_STATIC_GIFS_DIR / filename).is_file()


def _gif_html(filename: str, *, size_px: int = 48) -> str:
    """Render a gif as an inline img tag pointing to the static URL.

    Falls back to a placeholder square with a question mark if the file
    isn't present, so a missing asset doesn't break the layout. The URL
    `app/static/gifs/<filename>` is served by Streamlit cuando
    `server.enableStaticServing = true` está activo en config.toml.
    """
    if _gif_exists(filename):
        # `app/static/...` es la ruta canónica que Streamlit expone cuando
        # static serving está habilitado. NO usar `/static/` (sin el prefijo
        # `app/`) porque Streamlit prepende su baseUrl en deploys (ej. HF
        # Spaces) y la URL absoluta se rompe.
        return (
            f'<img src="app/static/gifs/{filename}" '
            f'style="width:{size_px}px;height:{size_px}px;border-radius:8px;'
            f'object-fit:cover;background:rgba(77,166,255,0.08);" alt=""/>'
        )
    return (
        f'<div style="width:{size_px}px;height:{size_px}px;border-radius:8px;'
        f'background:rgba(77,166,255,0.06);border:1px dashed rgba(77,166,255,0.2);'
        f'display:flex;align-items:center;justify-content:center;'
        f'color:var(--text-muted);font-family:Space Mono,monospace;font-size:14px;">'
        f'?</div>'
    )


# ─── Main render ────────────────────────────────────────────────────
def render_screen_progress() -> None:
    render_header(step_key="step.progress")

    if is_mock_mode():
        render_mock_badge()

    # --- 1. VALIDACIÓN DE SEGURIDAD ---
    payload = st.session_state.get("state_payload", {})
    if not payload and not is_mock_mode():
        st.warning(t("progress.warn.no_config"))
        st.session_state.screen = "config"
        st.rerun()
        return

    # Cochrane mode propagates from the form payload — used to hide the
    # rob_assessor and grade_profiler rows when the user didn't opt in.
    cochrane_mode = bool(payload.get("cochrane_mode", False))
    visible_agents = [a for a in AGENTS if not (a[5] and not cochrane_mode)]

    # --- 2. INICIO DE RED ROBUSTO (Evitar Generador Zombie) ---
    if "current_run_id" not in st.session_state:
        try:
            st.session_state.current_run_id = start_pipeline(payload) if not is_mock_mode() else "mock"
            st.session_state.event_history = []
            st.session_state.pipeline_finished = False
            st.session_state.kappa_value = None
            st.session_state.final_state_data = None
            st.session_state.done_agents = set()
            st.session_state.active_agent = visible_agents[0][0] if visible_agents else None
            st.session_state.cancel_requested = False
        except Exception as e:
            st.error(f"Error al conectar con el backend: {e}")
            if st.button(t("results.cta.back_config"), type="primary"):
                st.session_state.screen = "config"
                st.rerun()
            return

    # Defensive init: claves que pueden faltar si el script entra por un
    # rerun en medio del flujo (cuando `current_run_id` ya existe pero
    # alguna key se introdujo en una versión más nueva del frontend, o
    # se perdió por una excepción anterior). Los `setdefault` son baratos
    # y nos evitan AttributeError que disparen el except del event loop
    # y se conviertan en loops de rerun.
    st.session_state.setdefault("agent_stats", {})
    st.session_state.setdefault("done_agents", set())
    st.session_state.setdefault("event_history", [])
    st.session_state.setdefault("pipeline_finished", False)
    st.session_state.setdefault("kappa_value", None)
    st.session_state.setdefault("final_state_data", None)
    st.session_state.setdefault("active_agent", visible_agents[0][0] if visible_agents else None)
    st.session_state.setdefault("cancel_requested", False)

    run_id = st.session_state.current_run_id

    # --- 3. SHORT-CIRCUIT: cancelación ya solicitada en un run anterior ---
    if st.session_state.get("cancel_requested") and not st.session_state.get("pipeline_finished"):
        st.warning(t("progress.cancel.requested"))
        time.sleep(1.2)
        for k in ("current_run_id", "event_history", "pipeline_finished",
                  "kappa_value", "final_state_data", "done_agents",
                  "active_agent", "cancel_requested", "agent_stats"):
            st.session_state.pop(k, None)
        st.success(t("progress.cancel.done"))
        time.sleep(0.6)
        st.session_state.screen = "config"
        st.rerun()
        return

    # --- 4. HEADER: cancel button + run info ---
    head_l, head_r = st.columns([5, 2])
    with head_l:
        st.markdown(
            f'<div style="font-family:Space Mono,monospace;font-size:11px;'
            f'color:var(--text-muted);letter-spacing:0.08em;">'
            f'RUN ID · <span style="color:var(--text-secondary);">{run_id[:8]}</span>'
            f'</div>',
            unsafe_allow_html=True,
        )
    with head_r:
        # Cancel button — only meaningful in real mode (mock can't be cancelled
        # server-side, and the mock generator exits quickly anyway).
        if not is_mock_mode() and not st.session_state.get("pipeline_finished"):
            if st.button(t("progress.cta.cancel"), key="cancel_btn",
                         type="secondary", use_container_width=True):
                _handle_cancel_click(run_id)
                return  # _handle_cancel_click calls st.rerun()

    # --- 5. CONTENEDORES DE UI (st.empty) ---
    with st.container():
        st.markdown('<div class="axiom-card">', unsafe_allow_html=True)
        agents_container = st.empty()
        kappa_container = st.empty()
        st.markdown('</div>', unsafe_allow_html=True)

    # Pipeline logs ahora viven en un expander cerrado por defecto. Los hitos
    # más importantes se muestran inline en cada fila de agente (status line),
    # así que el log queda como debug / fallback opcional.
    with st.expander(t("progress.logs"), expanded=False):
        logs_container = st.empty()

    # NOTA: removimos el `action_container = st.empty()` que estaba acá.
    # Los botones finales se dibujan directos al final del script — usar un
    # `st.empty()` con un `with .container():` posterior demostró ser frágil
    # después de loops SSE largos: a veces el bloque no se rendereaba y el
    # botón "Ver resultados" no aparecía aunque pipeline_finished fuera True.

    # --- 6. FUNCIONES DE DIBUJO ---
    def _format_agent_status(ui_key: str, *, is_active: bool, is_done: bool) -> str:
        """Return the localized status line for one agent.

        Three states, in priority order:
          • cancelled  → "Cancelado"            (only when user cancelled)
          • done       → key-specific completion text using stats payload
          • active     → key-specific "in progress" text
          • waiting    → generic "Esperando turno…"

        Stats live in st.session_state.agent_stats[ui_key]. Missing fields
        fall back to safe defaults (0) so a malformed payload never raises.
        """
        if st.session_state.get("cancel_requested") and not is_done:
            return t("status.cancelled")

        stats = st.session_state.get("agent_stats", {}).get(ui_key, {})

        if is_done:
            if ui_key == "searcher":
                return t("status.searcher.done", n=stats.get("papers_found", 0))
            if ui_key == "screener":
                return t(
                    "status.screener.done",
                    included=stats.get("included", 0),
                    excluded=stats.get("excluded", 0),
                )
            if ui_key == "extractor":
                return t("status.extractor.done", n=stats.get("extractions", 0))
            if ui_key == "rob_assessor":
                return t("status.rob.done", n=stats.get("papers_assessed", 0))
            if ui_key == "analyst_7b":
                return t("status.analyst7b.done", n=stats.get("clusters_analyzed", 0))
            if ui_key == "analyst_32b":
                return t("status.analyst32b.done", n=stats.get("clusters_analyzed", 0))
            if ui_key == "grade_profiler":
                return t(
                    "status.grade.done",
                    ok=stats.get("clusters_graded", 0),
                    total=stats.get("clusters_total", 0),
                )
            if ui_key == "gapfinder":
                return t("status.gapfinder.done", n=stats.get("gaps", 0))
            if ui_key == "writer":
                return t("status.writer.done")
            return ""

        if is_active:
            mapping = {
                "searcher":       "status.searcher.active",
                "screener":       "status.screener.active",
                "extractor":      "status.extractor.active",
                "rob_assessor":   "status.rob.active",
                "analyst_7b":     "status.analyst7b.active",
                "analyst_32b":    "status.analyst32b.active",
                "grade_profiler": "status.grade.active",
                "gapfinder":      "status.gapfinder.active",
                "writer":         "status.writer.active",
            }
            return t(mapping.get(ui_key, "status.waiting"))

        return t("status.waiting")

    def update_agents_ui():
        active = st.session_state.get("active_agent")
        done = st.session_state.get("done_agents", set())
        finished = st.session_state.get("pipeline_finished", False)

        rows_html = ['<div style="display:flex;flex-direction:column;gap:10px;">']
        for (key, label_key, model, desc_key, gif_name, _coch) in visible_agents:
            is_done = (key in done) or finished
            is_active = (key == active) and not is_done

            if is_active:
                state_icon, state_color, border_color, bg_color = "🔄", "#38d9b4", "rgba(77,166,255,0.4)", "rgba(77,166,255,0.06)"
                label_color, label_weight = "#38d9b4", "600"
            elif is_done:
                state_icon, state_color, border_color, bg_color = "✅", "#38d9b4", "rgba(56,217,180,0.3)", "rgba(56,217,180,0.04)"
                label_color, label_weight = "var(--text-secondary)", "500"
            else:
                state_icon, state_color, border_color, bg_color = "⏳", "var(--text-muted)", "rgba(77,166,255,0.08)", "rgba(9,15,31,0.4)"
                label_color, label_weight = "var(--text-muted)", "400"

            # Status line: per-agent live milestone shown below the description.
            status_text = _format_agent_status(key, is_active=is_active, is_done=is_done)
            status_color = (
                "#38d9b4" if is_done else
                ("#4da6ff" if is_active else "var(--text-muted)")
            )

            rows_html.append(
                f'<div style="display:flex;align-items:center;gap:14px;padding:12px 14px;'
                f'border:1px solid {border_color};border-radius:10px;background:{bg_color};'
                f'transition:all 0.3s;">'
                f'{_gif_html(gif_name, size_px=48)}'
                f'<div style="flex:1;min-width:0;">'
                f'<div style="display:flex;align-items:baseline;gap:8px;">'
                f'<span style="font-size:14px;font-weight:{label_weight};color:{label_color};">'
                f'{state_icon} {t(label_key)}</span>'
                f'</div>'
                f'<div style="font-size:11px;color:var(--text-muted);margin-top:2px;">{t(desc_key)}</div>'
                f'<div style="font-size:11px;color:{status_color};margin-top:4px;'
                f'font-family:Space Mono,monospace;font-weight:500;">{status_text}</div>'
                f'</div>'
                f'<div style="font-family:Space Mono,monospace;font-size:10px;color:#4da6ff;'
                f'background:rgba(77,166,255,0.1);padding:3px 8px;border-radius:4px;'
                f'border:1px solid rgba(77,166,255,0.2);white-space:nowrap;">{model}</div>'
                f'</div>'
            )
        rows_html.append('</div>')
        # NOTA: usamos .markdown(unsafe_allow_html=True) en vez de .html().
        # El método .html() inyecta el HTML como un component custom de
        # Streamlit, lo cual: (1) abre un iframe sandboxed, (2) envía el
        # contenido completo por el canal websocket en cada update. Con 9
        # gifs en base64 inline (~300 KB cada uno), el mensaje pasa fácil
        # los 2-3 MB y desincroniza el browser → toast "Bad message format".
        # .markdown() pasa por el canal nativo (más liviano) y no abre iframe.
        agents_container.markdown("".join(rows_html), unsafe_allow_html=True)

    def update_logs_ui():
        lines = []
        for ev in st.session_state.event_history[-10:]:
            if ev.type != "log" or not ev.message:
                continue
            color = (
                "#f06070" if ev.level == "error"
                else ("#d4aa5a" if ev.level == "warn"
                      else ("#38d9b4" if ev.level == "success" else "var(--text-secondary)"))
            )
            # Translate via LOG_PATTERNS; falls back to original if no match.
            text = translate_log(ev.message)
            lines.append(
                f'<div style="font-family:Space Mono,monospace;font-size:11px;'
                f'color:{color};margin-bottom:4px;line-height:1.5;">> {text}</div>'
            )
        if not lines:
            lines.append(
                f'<div style="font-family:Space Mono,monospace;font-size:11px;'
                f'color:var(--text-muted);">> …</div>'
            )
        logs_html = (
            f'<div style="background:#0a0f1a;padding:12px;'
            f'border:1px solid rgba(77,166,255,0.12);border-radius:6px;'
            f'height:200px;overflow-y:auto;box-shadow:inset 0 2px 4px rgba(0,0,0,0.5);">'
            f'{"".join(lines)}</div>'
        )
        logs_container.markdown(logs_html, unsafe_allow_html=True)

    def update_kappa_ui():
        if st.session_state.kappa_value is None:
            return
        kappa_container.markdown(
            f'<div class="axiom-card" style="padding:14px 16px;margin-top:16px;'
            f'border-color:rgba(201,160,64,0.3);background:rgba(201,160,64,0.05);">'
            f'<div style="font-size:11px;font-family:Space Mono,monospace;'
            f'color:#c9a040;letter-spacing:0.08em;">{t("progress.kappa.label")}</div>'
            f'<div style="display:flex;align-items:baseline;gap:8px;margin-top:6px;">'
            f'<span style="font-size:36px;font-weight:700;color:#38d9b4;'
            f'font-family:Space Mono,monospace;">{st.session_state.kappa_value}</span>'
            f'<span style="font-size:13px;color:var(--text-muted);">'
            f'{t("progress.kappa.cohen")}</span>'
            f'</div></div>',
            unsafe_allow_html=True,
        )

    # --- 7. INITIAL DRAW ---
    update_agents_ui()
    update_logs_ui()

    # --- 8. EVENT LOOP ---
    # Throttle de updates: cuando llegan muchos eventos seguidos (típico al
    # inicio de cada nodo, varios logs en pocos ms), reconstruir el HTML de
    # 9 filas con gifs en base64 en cada uno desincroniza el websocket. Acá
    # forzamos un mínimo de UI_MIN_INTERVAL_S entre redibujados; los eventos
    # importantes (cambio de estado de un agente, fin del run) lo bypassan.
    UI_MIN_INTERVAL_S = 0.4
    last_ui_update = 0.0

    try:
        events_gen = run_pipeline_events(payload, run_id=run_id)

        for event in events_gen:
            # Cooperative cancel check (registers when Streamlit reruns the
            # script after a button click between iterations).
            if st.session_state.get("cancel_requested"):
                break

            # run_started: inject a localized log line one time
            if event.type == "run_started":
                rid = (event.payload or {}).get("run_id", "?")
                log_msg = t("progress.run_started", run_id=rid)
                if not any(getattr(e, "message", None) == log_msg for e in st.session_state.event_history):
                    st.session_state.event_history.append(
                        PipelineEvent(type="log", level="info", message=log_msg)
                    )

            # Dedupe (event objects compare by field equality)
            if event not in st.session_state.event_history:
                st.session_state.event_history.append(event)

            # Map backend node → UI agent key
            ui_agent = NODE_TO_UI.get(event.agent) if event.agent else None

            # Track whether this event changes visible state — if so, force
            # an immediate UI update bypassing the throttle.
            state_changed = False

            if event.type == "agent_done" and ui_agent:
                # Acumular stats por ui_key. Algunos agentes UI agrupan varios
                # nodos backend (writer: 6 sub-nodos; screener: 7b+32b) y los
                # stats vienen distribuidos entre ellos — el merge preserva
                # campos previos y sobrescribe los nuevos. _derive_stats en
                # el backend no devuelve nada para screener_7b/_32b (solo
                # para "screener"), así que esos rows pueden quedar sin
                # contar si el grafo no agrega un nodo "screener" final. En
                # ese caso el status line cae al texto "active" → "done"
                # sin números, lo cual es aceptable.
                new_stats = (event.payload or {}).get("stats") or {}
                if new_stats:
                    prev = st.session_state.agent_stats.get(ui_agent, {})
                    st.session_state.agent_stats[ui_agent] = {**prev, **new_stats}

                st.session_state.done_agents = st.session_state.get("done_agents", set()) | {ui_agent}
                # Advance active_agent to the next non-done visible row
                ui_order = [a[0] for a in visible_agents]
                done = st.session_state.done_agents
                next_active = next((k for k in ui_order if k not in done), None)
                st.session_state.active_agent = next_active
                state_changed = True

            elif event.type == "agent_start" and ui_agent:
                # Some agents emit agent_start; treat as activation
                if ui_agent not in st.session_state.get("done_agents", set()):
                    st.session_state.active_agent = ui_agent
                    state_changed = True

            elif event.type == "kappa":
                st.session_state.kappa_value = (event.payload or {}).get("value")
                state_changed = True

            elif event.type == "final_state":
                st.session_state.final_state_data = event.payload

            elif event.type == "cancelled":
                # Backend confirmed cancellation
                st.session_state.cancel_requested = True
                break

            elif event.type == "finished":
                st.session_state.pipeline_finished = True
                st.session_state.active_agent = None
                state_changed = True

            # Redibujamos si: (a) la pantalla cambió de manera importante, o
            # (b) pasó suficiente tiempo desde el último update. Los logs
            # entre estados (que llegan muy seguidos) caen en (b) y se
            # acumulan en event_history pero solo se renderizan al siguiente
            # tick — la diferencia es invisible para el usuario y el
            # websocket no se satura.
            now = time.time()
            if state_changed or (now - last_ui_update) >= UI_MIN_INTERVAL_S:
                update_agents_ui()
                update_logs_ui()
                update_kappa_ui()
                last_ui_update = now

    except (httpx.HTTPError, httpx.StreamError, ConnectionError, TimeoutError) as e:
        # Errores transitorios de red: warning + reintentar. Solo atrapamos
        # excepciones de red conocidas — los bugs nuestros (AttributeError,
        # KeyError, TypeError…) DEBEN propagar para que se vean en pantalla
        # con el traceback rojo de Streamlit. Antes este except era genérico
        # y atrapaba todo, lo que creaba un loop de reruns silencioso cuando
        # algún acceso a session_state fallaba: el except disparaba rerun, el
        # rerun reabría el SSE, y el ciclo se repetía cada 1-2s.
        st.warning(f"Sincronizando con el backend… ({e})")
        time.sleep(1)
        st.rerun()
        return

    # Final UI flush: si el último evento cayó dentro de la ventana de
    # throttle, podríamos haber salido del loop sin haber dibujado el estado
    # más reciente. Forzamos un último update para garantizar consistencia.
    update_agents_ui()
    update_logs_ui()
    update_kappa_ui()

    # --- 9. ACCIONES FINALES ---
    # Botones dibujados a nivel raíz del script (sin st.empty intermedio)
    # para garantizar que aparezcan siempre que el flujo llegue hasta acá.
    if st.session_state.get("cancel_requested"):
        st.info(t("progress.cancel.requested"))
        time.sleep(0.8)
        for k in ("current_run_id", "event_history", "pipeline_finished",
                  "kappa_value", "final_state_data", "done_agents",
                  "active_agent", "cancel_requested", "agent_stats"):
            st.session_state.pop(k, None)
        st.session_state.screen = "config"
        st.rerun()
    elif st.session_state.get("pipeline_finished"):
        if st.button(t("progress.cta.results"), type="primary",
                     use_container_width=True, key="goto_results_btn"):
            st.session_state.results = {
                "final_state": st.session_state.get("final_state_data"),
                "run_id": st.session_state.get("current_run_id"),
            }
            st.session_state.screen = "results"
            st.rerun()

    render_footer()


# ─── Helpers ────────────────────────────────────────────────────────
def _handle_cancel_click(run_id: str) -> None:
    """Cancel button handler: POST to backend, set flag, rerun.

    On error we still flag cancel_requested locally so the user can leave
    the screen; we surface the error via a session_state toast that the
    next render picks up.
    """
    try:
        cancel_pipeline(run_id)
    except CancelFailed as e:
        # Surface the error but still proceed — user clearly wants to leave.
        st.session_state["_cancel_warn"] = t("progress.cancel.failed", err=str(e))
    st.session_state.cancel_requested = True
    st.rerun()
