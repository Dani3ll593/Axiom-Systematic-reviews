"""
utils/form_to_state.py
──────────────────────
Maps the Streamlit form to the PICOS-shaped initial_state the Axiom backend
expects (see backend axiom_api.py contract — same shape produced by
prompts/test_*.py files).

This module is the single source of truth for the form → backend contract.

DEFENSIVE BY DESIGN
────────────────────
The form is intentionally permissive. A user can submit only the research
question and skip every other field — the mapper fills sensible defaults
and tries to extract PICOS hints, year ranges, and language hints from the
free-text question itself.

Three extraction layers, in order of precedence:

  1. Explicit form fields (advanced mode)            ← highest precedence
  2. Regex-extracted hints from free text             ← middle
  3. Defaults                                          ← lowest

The mapper NEVER raises. If something is missing or malformed, it logs a
warning (caller decides what to do) and falls through to defaults.
"""

from __future__ import annotations
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, TypedDict

logger = logging.getLogger(__name__)


# ============================================================================
# Constants — DO NOT modify without backend alignment
# ============================================================================
EXCLUSION_REASONS_FIXED_LIST = [
    "wrong_population", "wrong_intervention", "wrong_study_design",
    "wrong_language", "wrong_year", "wrong_outcomes", "not_relevant",
    "duplicate", "unavailable_full_text",
]

# Defaults para los 4 escenarios sin form lleno
DEFAULT_YEAR_MIN = 2020
DEFAULT_YEAR_MAX = 2025
DEFAULT_LANGUAGES = ["English", "Spanish"]
DEFAULT_STUDY_DESIGN_INCLUDE = [
    "randomized controlled trial",
    "cohort study",
    "cross-sectional",
]
DEFAULT_STUDY_DESIGN_EXCLUDE = ["case report", "editorial"]
DEFAULT_PUBLICATION_STATUS_ACCEPTED = ["published"]

FRONTEND_VERSION = "1.1"


# ============================================================================
# Form data shape
# ============================================================================
class FormData(TypedDict, total=False):
    # The ONLY field we treat as truly required
    question: str

    # Everything else is optional; defaults / regex extraction kick in
    domain: str
    year_min: int
    year_max: int
    languages: list[str]

    population_include: list[str]
    population_exclude: list[str]
    intervention_include: list[str]
    intervention_exclude: list[str]
    comparison_include: list[str]
    comparison_exclude: list[str]
    outcomes_primary: list[str]
    outcomes_secondary: list[str]
    study_design_include: list[str]
    study_design_exclude: list[str]
    publication_status_accepted: list[str]
    publication_status_rejected: list[str]


# ============================================================================
# Helpers — list cleaning
# ============================================================================
def _clean_list(items: Any) -> list[str]:
    """Strip whitespace and trailing punctuation, drop empties, dedupe.

    Tolerant input: accepts None, lists, tuples, sets, even single strings.
    Trailing periods/semicolons/commas are stripped because regex-extracted
    items often end at a sentence boundary.
    """
    if items is None:
        return []
    if isinstance(items, str):
        items = [items]
    if not hasattr(items, "__iter__"):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        s = (str(item) if item is not None else "").strip()
        # Strip trailing sentence punctuation that bleeds in from regex splits
        s = re.sub(r"[\s.,;]+$", "", s).strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _split_list(value: Any) -> list[str]:
    """Like _clean_list but also splits comma/semicolon-separated strings.

    Lets the user paste 'rct, cohort study; cross-sectional' as a single field.
    """
    if value is None:
        return []
    if isinstance(value, str):
        # split on `,` or `;` or newlines
        parts = re.split(r"[,;\n]+", value)
        return _clean_list(parts)
    return _clean_list(value)


# ============================================================================
# Helpers — Free-text PICOS extraction
# ============================================================================
# Headers we recognize. ES and EN; case-insensitive; followed by ":" or "—".
# Order matters for greedy matching: longer first.
# `_metadata` keys ("years", "languages") are recognized only as separators —
# they cleanly terminate a preceding section but are NOT stored as PICOS fields.
_PICOS_HEADERS = {
    "population": [
        r"population", r"poblaci[óo]n", r"participantes",
    ],
    "intervention": [
        r"intervention", r"intervenci[óo]n",
    ],
    "comparison": [
        r"comparator", r"comparison", r"comparaci[óo]n",
    ],
    "outcomes": [
        r"outcomes?", r"resultados", r"resultados? \(outcomes\)",
    ],
    "study_design": [
        r"study designs?", r"dise[ñn]o(?: de estudio)?", r"tipos? de estudio",
    ],
    "_years_meta": [
        r"years?", r"a[ñn]os?",
    ],
    "_languages_meta": [
        r"languages?", r"idiomas?",
    ],
}


def _extract_picos_sections(text: str) -> dict[str, str]:
    """Extract PICOS sections from free text.

    Returns a dict like {'population': '...', 'intervention': '...', ...}
    with only the sections that were present and non-empty.

    Strategy: build a single regex that matches ANY known header, then
    use a sliding window to capture the text between consecutive headers.
    Robust to ordering and to extra spacing/punctuation.
    """
    if not text or len(text) < 30:
        return {}

    # Build alternation of all headers, capturing which canonical key matched
    header_alts: list[tuple[str, str]] = []  # (canonical_key, regex_alt)
    for canon, alts in _PICOS_HEADERS.items():
        for alt in alts:
            header_alts.append((canon, alt))

    # Pattern that matches "<header>" followed by ":" or "—" or "-"
    # Captures the header itself for identification
    pattern = "|".join(
        f"(?P<h_{i}>{alt})\\s*[:\\u2014\\u2013\\-]" for i, (_, alt) in enumerate(header_alts)
    )

    # Find all header matches with their positions
    matches: list[tuple[int, int, str]] = []  # (start, end, canonical_key)
    for m in re.finditer(pattern, text, flags=re.IGNORECASE):
        for i, (canon, _) in enumerate(header_alts):
            if m.group(f"h_{i}"):
                matches.append((m.start(), m.end(), canon))
                break

    if not matches:
        return {}

    # Sort by position, then extract text between consecutive headers
    matches.sort(key=lambda x: x[0])
    sections: dict[str, str] = {}
    for i, (start, end, canon) in enumerate(matches):
        next_start = matches[i + 1][0] if i + 1 < len(matches) else len(text)
        body = text[end:next_start].strip()
        # Strip trailing punctuation / connector words at section boundary
        body = re.sub(r"[.;\s]+$", "", body)
        # Skip _meta sections — they're separators only (years, languages)
        if canon.startswith("_") or not body:
            continue
        if canon not in sections:  # first occurrence wins
            sections[canon] = body

    return sections


def _split_outcomes(text: str) -> tuple[list[str], list[str]]:
    """Split an outcomes section into (primary, secondary) lists.

    Recognizes 'Primary:'/'Primario:' and 'Secondary:'/'Secundarios:' subheaders.
    Falls back to all-as-primary if no subheaders present.
    """
    if not text:
        return [], []

    primary_pat = re.compile(
        r"(?:primary|primario|primarios)\s*[:\u2014\u2013\-]\s*(.*?)"
        r"(?=(?:secondary|secundario|secundarios)\s*[:\u2014\u2013\-]|$)",
        re.IGNORECASE | re.DOTALL,
    )
    secondary_pat = re.compile(
        r"(?:secondary|secundario|secundarios)\s*[:\u2014\u2013\-]\s*(.*?)$",
        re.IGNORECASE | re.DOTALL,
    )

    p_match = primary_pat.search(text)
    s_match = secondary_pat.search(text)

    if not p_match and not s_match:
        return _split_list(text), []

    primary = _split_list(p_match.group(1)) if p_match else []
    secondary = _split_list(s_match.group(1)) if s_match else []
    return primary, secondary


# ============================================================================
# Helpers — Free-text year extraction
# ============================================================================
# Patterns ordered most-specific to most-generic
_YEAR_RANGE_PATTERNS = [
    # "between 2018 and 2025" / "entre 2018 y 2025"
    re.compile(r"(?:between|entre)\s+(\d{4})\s+(?:and|y)\s+(\d{4})", re.IGNORECASE),
    # "2018–2025" / "2018-2025" / "2018 a 2025" / "from 2018 to 2025"
    re.compile(r"(?:from|desde)?\s*(\d{4})\s*(?:[\u2013\u2014\-–]|to|a)\s*(\d{4})", re.IGNORECASE),
]
_YEAR_FROM_PATTERNS = [
    # "since 2019" / "desde 2019" / "from 2019" / "a partir de 2019"
    re.compile(r"(?:since|desde|from|a partir de)\s+(\d{4})", re.IGNORECASE),
    re.compile(r"(?:after|despu[ée]s de|posteriores? a)\s+(\d{4})", re.IGNORECASE),
]


def _extract_year_range(text: str) -> tuple[int | None, int | None]:
    """Find a year range in free text. Returns (year_min, year_max) or (None, None).

    A range pattern (e.g., '2018-2025') always wins over a from-only ('since 2019').
    Years must be plausible (1990 ≤ y ≤ current+5).
    """
    if not text:
        return None, None

    current_year = datetime.now(timezone.utc).year
    plausible = lambda y: 1990 <= y <= current_year + 5

    # 1. Try ranges first
    for pat in _YEAR_RANGE_PATTERNS:
        m = pat.search(text)
        if m:
            y1, y2 = int(m.group(1)), int(m.group(2))
            if plausible(y1) and plausible(y2):
                return (min(y1, y2), max(y1, y2))

    # 2. Try "from X" patterns; year_max defaults to current
    for pat in _YEAR_FROM_PATTERNS:
        m = pat.search(text)
        if m:
            y = int(m.group(1))
            if plausible(y):
                return (y, current_year)

    return None, None


# ============================================================================
# Helpers — Free-text language extraction
# ============================================================================
_LANGUAGE_KEYWORDS = {
    "English":    [r"\benglish\b", r"\bingl[ée]s\b", r"\binglesa?s?\b"],
    "Spanish":    [r"\bspanish\b", r"\bespa[ñn]ol\b", r"\bcastellano\b"],
    "Portuguese": [r"\bportuguese\b", r"\bportugu[ée]s\b"],
    "French":     [r"\bfrench\b", r"\bfranc[ée]s\b"],
}


def _extract_languages(text: str) -> list[str]:
    """Detect language mentions in free text. Returns canonical names (English/Spanish/...)."""
    if not text:
        return []
    found: list[str] = []
    for canon, patterns in _LANGUAGE_KEYWORDS.items():
        for pat in patterns:
            if re.search(pat, text, re.IGNORECASE):
                if canon not in found:
                    found.append(canon)
                break
    return found


# ============================================================================
# Main mapper
# ============================================================================
def map_form_to_initial_state(form: FormData | dict[str, Any]) -> dict[str, Any]:
    """Convert form input into the PICOS-shaped initial_state.

    Always emits all required keys (with empty arrays where applicable) so
    downstream nodes never KeyError. Never raises — falls through to defaults
    on malformed input.

    Precedence for each field:
      1. Explicit form value (if non-empty)
      2. Value extracted from free text (PICOS section, year mention, lang mention)
      3. Default constant
    """
    if not isinstance(form, dict):
        logger.warning("form_to_state: input is not a dict; returning defaults-only payload")
        form = {}

    question = (form.get("question") or "").strip()

    # --- PICOS extraction from free text (used as 2nd-tier hints) ---
    free_text_sections = _extract_picos_sections(question)

    # --- Year range: form > regex > default ---
    yr_min_form = form.get("year_min")
    yr_max_form = form.get("year_max")
    if yr_min_form and yr_max_form:
        try:
            year_min, year_max = int(yr_min_form), int(yr_max_form)
        except (TypeError, ValueError):
            year_min, year_max = DEFAULT_YEAR_MIN, DEFAULT_YEAR_MAX
    else:
        ymn, ymx = _extract_year_range(question)
        year_min = ymn if ymn is not None else DEFAULT_YEAR_MIN
        year_max = ymx if ymx is not None else DEFAULT_YEAR_MAX

    if year_min > year_max:
        logger.warning(
            "form_to_state: year_min (%d) > year_max (%d); swapping.",
            year_min, year_max,
        )
        year_min, year_max = year_max, year_min

    # --- Languages: form > regex > default ---
    langs_form = _clean_list(form.get("languages"))
    if langs_form:
        languages = langs_form
    else:
        langs_extracted = _extract_languages(question)
        languages = langs_extracted if langs_extracted else list(DEFAULT_LANGUAGES)

    # --- PICOS fields: form takes precedence; otherwise pull from regex sections ---
    population_include = (
        _clean_list(form.get("population_include"))
        or _split_list(free_text_sections.get("population", ""))
    )
    intervention_include = (
        _clean_list(form.get("intervention_include"))
        or _split_list(free_text_sections.get("intervention", ""))
    )
    comparison_include = (
        _clean_list(form.get("comparison_include"))
        or _split_list(free_text_sections.get("comparison", ""))
    )
    study_design_include = (
        _clean_list(form.get("study_design_include"))
        or _split_list(free_text_sections.get("study_design", ""))
        or list(DEFAULT_STUDY_DESIGN_INCLUDE)
    )

    # Outcomes: try to split primary/secondary from the free-text section if needed
    outcomes_primary = _clean_list(form.get("outcomes_primary"))
    outcomes_secondary = _clean_list(form.get("outcomes_secondary"))
    if not outcomes_primary and not outcomes_secondary:
        op, os_ = _split_outcomes(free_text_sections.get("outcomes", ""))
        outcomes_primary = op
        outcomes_secondary = os_

    return {
        "sr_id":  uuid.uuid4().hex[:8],
        "domain": (form.get("domain") or "general").strip() or "general",
        "question": question or "(no question provided)",
        "prisma_criteria": {
            "framework":      "PICOS",
            "prisma_version": "2020",
            "eligibility_criteria": {
                "population": {
                    "include": population_include,
                    "exclude": _clean_list(form.get("population_exclude")),
                },
                "intervention": {
                    "include": intervention_include,
                    "exclude": _clean_list(form.get("intervention_exclude")),
                },
                "comparison": {
                    "include": comparison_include,
                    "exclude": _clean_list(form.get("comparison_exclude")),
                },
                "outcomes": {
                    "primary":   outcomes_primary,
                    "secondary": outcomes_secondary,
                },
                "study_design": {
                    "include": study_design_include,
                    "exclude": _clean_list(form.get("study_design_exclude")) or list(DEFAULT_STUDY_DESIGN_EXCLUDE),
                },
                "temporal": {
                    "year_min": year_min,
                    "year_max": year_max,
                },
                "language": {
                    "accepted": languages,
                    "rejected": [],
                },
                "publication_status": {
                    "accepted": _clean_list(form.get("publication_status_accepted")) or list(DEFAULT_PUBLICATION_STATUS_ACCEPTED),
                    "rejected": _clean_list(form.get("publication_status_rejected")),
                },
            },
            "screening_instructions": {
                "phase_1": "title_abstract_only",
                "phase_2": "full_text_required",
                "doubtful_action": "escalate_to_second_reviewer",
                "exclusion_reasons_required": True,
                "exclusion_reasons_fixed_list": list(EXCLUSION_REASONS_FIXED_LIST),
            },
        },
        "_frontend_meta": {
            "frontend_version": FRONTEND_VERSION,
            "submitted_at":     datetime.now(timezone.utc).isoformat(),
        },
    }


# Backwards-compat alias for any code still calling the old name
def form_to_state(form: dict[str, Any]) -> dict[str, Any]:
    """DEPRECATED — use map_form_to_initial_state instead."""
    return map_form_to_initial_state(form)
