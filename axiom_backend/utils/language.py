"""
axiom_backend/utils/language.py
────────────────────────────────
Detección y resolución de idioma de salida para los nodos LLM.

Centraliza dos funciones que antes vivían dispersas:

  • detect_language(question) — heurística ES vs EN basada en acentos y
    stopwords. Antes vivía solo en writer.py como `_detect_language`.

  • resolve_output_language(state) — la lógica de prioridad que TODOS los
    nodos LLM consumidores de idioma (writer_synthesis, writer_discussion,
    writer_limitations, writer_tables, writer_references, rob_assessor,
    grade_profiler) deben usar para decidir en qué idioma generar texto:

      1) Si state["output_language"] == "English" / "Spanish"  → ese.
      2) Si state["output_language"] == "auto" o ausente       → autodetect.
      3) Cualquier otro valor inesperado                       → autodetect.

Por qué un módulo nuevo en vez de seguir importando de writer.py:
  - rob_assessor.py y grade_profiler.py necesitan la misma lógica y no
    deberían depender de writer.py (rompería capas: writer es el último
    nodo del grafo y consume todo el state, mientras que rob/grade son
    nodos intermedios).
  - Frontend manda 'auto' explícito cuando el usuario seleccionó "Auto"
    en el dropdown. Tratar 'auto' como sinónimo de "detectar" centraliza
    el contrato cliente-servidor en UN solo lugar.
"""

from __future__ import annotations
import re
from typing import Any


# ─── Heurística ES vs EN ─────────────────────────────────────────────
# Stopwords muy frecuentes y específicas de cada idioma. No pretende ser
# un detector general — solo distingue ES vs EN, que es lo único que el
# corpus actual produce. Si en el futuro entran PT/FR, ampliar acá o
# cambiar a `langdetect`.
_ES_MARKERS = {
    "el", "la", "los", "las", "de", "del", "en", "para", "por", "con", "sin",
    "que", "qué", "cuál", "cuáles", "cómo", "cuándo", "dónde", "es", "son",
    "y", "o", "u", "un", "una", "unos", "unas", "sobre", "entre",
    "efectividad", "eficacia", "comparado", "respecto",
}
_EN_MARKERS = {
    "the", "of", "in", "for", "with", "without", "and", "or", "an", "a",
    "what", "which", "how", "when", "where", "is", "are", "to", "from",
    "compared", "between", "among", "effectiveness", "efficacy",
}

# Idiomas que el sistema soporta como output explícito. Si el frontend
# manda algo fuera de este set (ej. "Portuguese"), caemos a autodetect
# silenciosamente para no romper runs viejos. Cuando agreguemos PT/FR,
# actualizar esta lista junto con el dropdown del frontend.
SUPPORTED_OUTPUT_LANGUAGES = {"English", "Spanish"}


def detect_language(question: str) -> str:
    """Devuelve 'Spanish' o 'English' según el idioma de la pregunta.

    Heurística:
      1. Si hay caracteres acentuados típicos del español (á é í ó ú ñ ¿ ¡),
         es ES inmediatamente — los hablantes hispanos suelen acentuar al
         menos una palabra en preguntas formales.
      2. Si no, cuenta tokens contra listas de stopwords ES vs EN; gana
         mayoría.
      3. Empate o sin señal → English (default conservador, igual al
         comportamiento histórico del writer antes de centralizar).
    """
    if not question or not question.strip():
        return "English"

    if re.search(r"[áéíóúñÁÉÍÓÚÑ¿¡]", question):
        return "Spanish"

    tokens = re.findall(r"\b[a-záéíóúñ]+\b", question.lower())
    es_hits = sum(1 for t in tokens if t in _ES_MARKERS)
    en_hits = sum(1 for t in tokens if t in _EN_MARKERS)

    if es_hits > en_hits:
        return "Spanish"
    return "English"


def resolve_output_language(state: dict[str, Any]) -> str:
    """Decide el idioma de output para los nodos LLM consumidores.

    Reglas (en orden):
      1) state["output_language"] == "English" o "Spanish"   → ese.
      2) state["output_language"] == "auto" / vacío / None  → autodetect.
      3) Cualquier valor desconocido                         → autodetect.

    Args:
        state: AxiomState (dict). Lee solo dos keys: 'output_language'
               y 'question'.

    Returns:
        "English" o "Spanish".
    """
    user_choice = (state.get("output_language") or "").strip()

    # Caso 1: usuario eligió explícitamente
    if user_choice in SUPPORTED_OUTPUT_LANGUAGES:
        return user_choice

    # Caso 2 y 3: auto o valor desconocido
    return detect_language(state.get("question", "") or "")