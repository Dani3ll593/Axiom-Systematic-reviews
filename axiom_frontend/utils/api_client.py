"""
utils/api_client.py
───────────────────
HTTP client for the Axiom backend orchestrator (FastAPI on the MI300X
droplet). The frontend no longer imports LangGraph or invokes the model
directly — it POSTs the PICOS payload to /pipeline/start and consumes
the resulting Server-Sent Events stream from /pipeline/stream/{run_id}.

Two modes:
  • REAL — talks to the backend at AXIOM_BACKEND_URL.
  • MOCK — utils/pipeline_runner.py replays a deterministic script.

Mode is chosen by `is_mock_mode()` below: forced via AXIOM_MOCK=1, or
auto-enabled when AXIOM_BACKEND_URL / AXIOM_BACKEND_API_KEY are missing.
"""

from __future__ import annotations
import json
import os
from typing import Any, Iterator

import httpx
import streamlit as st


# Default request timeouts (seconds). Long read for the SSE stream;
# the backend can take 5–15 min on a heavy systematic review.
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_READ_TIMEOUT = 900.0


def _get_secret(key: str, default: str | None = None) -> str | None:
    """Read st.secrets first (HF Spaces / local secrets.toml), fall back
    to env var, then default. Never raises."""
    try:
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return os.environ.get(key, default)


def is_mock_mode() -> bool:
    """True when the UI should replay the canned mock pipeline instead of
    calling the backend. Forced by AXIOM_MOCK=1 or auto-enabled when
    backend creds are absent (so the demo always works offline)."""
    if (_get_secret("AXIOM_MOCK") or "").lower() in ("1", "true", "yes"):
        return True
    return not (_get_secret("AXIOM_BACKEND_URL") and _get_secret("AXIOM_BACKEND_API_KEY"))


def _backend_url() -> str:
    url = _get_secret("AXIOM_BACKEND_URL")
    if not url:
        raise RuntimeError(
            "AXIOM_BACKEND_URL no está configurado. Define el secreto en "
            ".streamlit/secrets.toml o en HuggingFace Settings → Secrets."
        )
    return url.rstrip("/")


def _auth_headers() -> dict[str, str]:
    api_key = _get_secret("AXIOM_BACKEND_API_KEY") or ""
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
        "Accept":        "text/event-stream",
        "User-Agent":    f"axiom-frontend/1.0 ({_get_secret('CONTACT_EMAIL') or 'unknown'})",
    }


# ─── HTTP API ────────────────────────────────────────────────────────

def start_pipeline(state_payload: dict[str, Any]) -> str:
    """POST /pipeline/start — kicks off a new run on the backend.

    Returns:
        run_id (str) — opaque handle the backend uses to identify the
        running pipeline. Pass it to stream_pipeline_events().
    """
    url = f"{_backend_url()}/pipeline/start"
    resp = httpx.post(
        url,
        headers={k: v for k, v in _auth_headers().items() if k != "Accept"},
        json=state_payload,
        timeout=httpx.Timeout(DEFAULT_CONNECT_TIMEOUT, read=30.0),
    )
    resp.raise_for_status()
    data = resp.json()
    if "run_id" not in data:
        raise RuntimeError(f"Respuesta inválida del backend: {data}")
    return data["run_id"]


def stream_pipeline_events(run_id: str) -> Iterator[dict[str, Any]]:
    """GET /pipeline/stream/{run_id} — yields decoded SSE events.

    Each event payload follows the contract defined in PipelineEvent
    (see utils/pipeline_runner.py): keys `type`, `agent`, `progress`,
    `message`, `level`, `payload`. Caller maps these to UI updates.

    Raises httpx.HTTPError on connection problems; the runner surfaces
    them as PipelineEvent(type='log', level='error').
    """
    url = f"{_backend_url()}/pipeline/stream/{run_id}"
    headers = _auth_headers()

    timeout = httpx.Timeout(DEFAULT_CONNECT_TIMEOUT, read=DEFAULT_READ_TIMEOUT)
    with httpx.stream("GET", url, headers=headers, timeout=timeout) as resp:
        resp.raise_for_status()
        # Standard SSE: events are blocks of `field: value` lines separated
        # by blank lines. We only care about `data:` lines (JSON payloads).
        data_buf: list[str] = []
        for line in resp.iter_lines():
            if line == "":
                if data_buf:
                    raw = "\n".join(data_buf)
                    data_buf = []
                    try:
                        yield json.loads(raw)
                    except json.JSONDecodeError:
                        # Backend sent a malformed event — surface as error
                        yield {
                            "type": "log",
                            "level": "error",
                            "message": f"Evento malformado del backend: {raw[:120]}",
                        }
                continue
            if line.startswith(":"):
                # SSE comment / keepalive — ignore
                continue
            if line.startswith("data:"):
                data_buf.append(line[5:].lstrip())


def fetch_final_state(run_id: str) -> dict[str, Any]:
    """GET /pipeline/result/{run_id} — fetches the final AxiomState once
    the stream has emitted `finished`. Returns the full state dict the
    Results screen consumes (report_md, gaps, restricted_papers, stats…)."""
    url = f"{_backend_url()}/pipeline/result/{run_id}"
    resp = httpx.get(
        url,
        headers={k: v for k, v in _auth_headers().items() if k != "Accept"},
        timeout=httpx.Timeout(DEFAULT_CONNECT_TIMEOUT, read=60.0),
    )
    resp.raise_for_status()
    return resp.json()


# ─── PDF download ───────────────────────────────────────────────────

class PdfNotReady(RuntimeError):
    """Raised when the backend reports the run is still in progress (HTTP 202)."""


class PdfNotAvailable(RuntimeError):
    """Raised when the backend has finished but no PDF was generated (HTTP 404)."""


def fetch_report_pdf(run_id: str) -> bytes:
    """GET /pipeline/{run_id}/report.pdf — returns the PDF bytes.

    Per BRIEF_BACKEND_PDF_ADDENDUM:
      • 200 → bytes (Content-Type: application/pdf)
      • 202 → run still in progress     → raises PdfNotReady
      • 404 → PDF not available / missing → raises PdfNotAvailable
      • any other non-2xx → propagates httpx.HTTPStatusError
    """
    url = f"{_backend_url()}/pipeline/{run_id}/report.pdf"
    headers = {k: v for k, v in _auth_headers().items() if k != "Accept"}
    resp = httpx.get(url, headers=headers, timeout=httpx.Timeout(DEFAULT_CONNECT_TIMEOUT, read=60.0))
    if resp.status_code == 202:
        raise PdfNotReady("Run still in progress")
    if resp.status_code == 404:
        raise PdfNotAvailable("PDF not available for this run")
    resp.raise_for_status()
    return resp.content

def fetch_apa7_pdf(run_id: str) -> bytes:
    """GET /pipeline/{run_id}/apa7.pdf — returns the PDF bytes.
    Mirrors fetch_report_pdf; only the endpoint and error string differ.
      • 200 → bytes (Content-Type: application/pdf)
      • 202 → run still in progress     → raises PdfNotReady
      • 404 → PDF not available / missing → raises PdfNotAvailable
      • any other non-2xx → propagates httpx.HTTPStatusError
    """
    url = f"{_backend_url()}/pipeline/{run_id}/apa7.pdf"
    headers = {k: v for k, v in _auth_headers().items() if k != "Accept"}
    resp = httpx.get(url, headers=headers, timeout=httpx.Timeout(DEFAULT_CONNECT_TIMEOUT, read=60.0))
    if resp.status_code == 202:
        raise PdfNotReady("Run still in progress")
    if resp.status_code == 404:
        raise PdfNotAvailable("APA 7 PDF not available for this run")
    resp.raise_for_status()
    return resp.content
