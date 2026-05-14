"""utils package — backend HTTP client, validators, pipeline runner, i18n."""
from utils.api_client import (
    is_mock_mode, start_pipeline, stream_pipeline_events, fetch_final_state,
    fetch_report_pdf, PdfNotReady, PdfNotAvailable,
)
from utils.form_to_state import form_to_state
from utils.validators import validate_research_query, validate_year_range
from utils.pipeline_runner import run_pipeline_events, PipelineEvent
from utils.i18n import t, get_language, TRANSLATIONS, DEFAULT_LANGUAGE

__all__ = [
    "is_mock_mode", "start_pipeline", "stream_pipeline_events", "fetch_final_state",
    "fetch_report_pdf", "PdfNotReady", "PdfNotAvailable",
    "form_to_state",
    "validate_research_query", "validate_year_range",
    "run_pipeline_events", "PipelineEvent",
    "t", "get_language", "TRANSLATIONS", "DEFAULT_LANGUAGE",
]
