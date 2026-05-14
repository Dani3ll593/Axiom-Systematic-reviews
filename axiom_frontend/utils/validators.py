"""
utils/validators.py
───────────────────
Lightweight input validation. Returns (is_valid, error_key, context_dict)
so the caller can translate via utils.i18n.t(error_key, **context).
"""

from __future__ import annotations


def validate_research_query(
    query: str, max_words: int = 200, min_words: int = 3
) -> tuple[bool, str | None, dict]:
    """Validate length and emptiness of the research question.

    Returns:
        (is_valid, i18n_key_or_None, format_context_dict)
    """
    if not query or not query.strip():
        return False, "config.error.empty_query", {}

    word_count = len(query.split())

    if word_count < min_words:
        return False, "config.error.too_short", {"n": word_count, "min": min_words}

    if word_count > max_words:
        return False, "config.error.too_long", {"n": word_count, "max": max_words}

    return True, None, {}


def validate_year_range(year_from: int, year_to: int) -> tuple[bool, str | None, dict]:
    """Sanity check on the PRISMA year range."""
    if year_from > year_to:
        return False, "config.error.bad_year_range", {"y1": year_from, "y2": year_to}
    return True, None, {}
