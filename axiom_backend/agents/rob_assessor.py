"""Agent — Risk of Bias Assessor (Cochrane RoB 2.0).

Evalúa cada paper extraído en los 5 dominios de RoB 2.0 (Sterne JAC et al.,
BMJ 2019;366:l4898) usando el modelo de reasoning (DeepSeek-R1 vía Featherless).

Posición en el grafo: entre `extractor` y `clusterer`. Solo se ejecuta
cuando state["cochrane_mode"] == True (la decisión la toma el grafo, no
este nodo). Si state.cochrane_mode es False, este nodo no se invoca.

Reads:
    state["extractions"]:     list[dict]
    state["question"]:        str   (para autodetect de idioma)
    state["output_language"]: str   (opcional; "English"/"Spanish"/"auto")

Writes:
    state["rob_assessments"]: list[dict]  (Annotated con operator.add)
        Cada entry tiene la forma:
        {
          "paper_id": str,
          "domain_1_randomization": {"judgment": "low|some|high|n/a", "rationale": str},
          "domain_2_deviations":    {"judgment": "low|some|high",     "rationale": str},
          "domain_3_missing_data":  {"judgment": "low|some|high",     "rationale": str},
          "domain_4_outcome_meas":  {"judgment": "low|some|high",     "rationale": str},
          "domain_5_reporting":     {"judgment": "low|some|high",     "rationale": str},
          "overall":                {"judgment": "low|some|high",     "rationale": str},
        }

El downstream grade_profiler hace lookup por paper_id, no por índice — el
orden de la lista no importa.

Las `rationale` salen en `output_language` (English o Spanish). El resto
de campos (judgment values: "low"/"some"/"high"/"n/a") son enums que NO
se traducen — son parte del contrato y el frontend los mapea a labels
localizados vía i18n.
"""

import asyncio
import json
import logging

from pydantic import BaseModel, ValidationError, Field

from axiom_backend.state import AxiomState
from axiom_backend.config import settings
from axiom_backend.tools.llm_router import LLM_32B, extract_json_from_response, featherless_credit, COST_32B
from axiom_backend.prompts import ROB_ASSESSOR_PROMPT
from axiom_backend.utils.language import resolve_output_language

logger = logging.getLogger(__name__)

# --- Tunables ---
# Respeta el cap global de Featherless Premium (4 conexiones). Si el screener
# u otro agente está corriendo en paralelo, este número debería bajarse desde
# el grafo. Como rob_assessor es secuencial en el DAG (no comparte slot con
# screener), 4 está OK.
MAX_CONCURRENT_PAPERS = 1   # Featherless Premium: el 32B cuesta 2 units.
                            # 2 paralelos × 2 units = 4 (cap del plan).

# DeepSeek-R1 razonando un paper completo: 60-90s típico. Margen para outliers.
TIMEOUT_S = settings.cochrane_rob_timeout_s


# ─── Lenguaje de salida ─────────────────────────────────────────────
# El prompt rob_assessor_prompt.md contiene placeholders {output_language}
# en su sección "OUTPUT LANGUAGE" y en cada rationale del JSON. Los
# reemplazamos antes de la llamada, igual que hace writer.py con
# WRITER_SYNTHESIS_PROMPT. La instrucción queda en el system prompt
# principal, no en un segundo message — los enums (judgment values
# "low"/"some"/"high"/"n/a") NO se traducen, eso queda explícito en el
# prompt y reforzado por el Pydantic schema que solo acepta esos literales.


# --- Esquema (espejo del rob_assessor_prompt.txt) ---
class Judgment(BaseModel):
    # n/a solo se permite explícitamente en Domain 1 (non-RCTs).
    # Pydantic no diferencia campos aquí; la validación de "n/a solo en D1"
    # la hacemos como business rule en _validate_rob_consistency.
    judgment: str = Field(..., pattern="^(low|some|high|n/a)$")
    rationale: str = Field(..., min_length=1)


class RoBAssessment(BaseModel):
    domain_1_randomization: Judgment
    domain_2_deviations:    Judgment
    domain_3_missing_data:  Judgment
    domain_4_outcome_meas:  Judgment
    domain_5_reporting:     Judgment
    overall:                Judgment


def _validate_rob_consistency(assessment: RoBAssessment) -> None:
    """Verifica reglas de negocio que pydantic no puede expresar.

    1. 'n/a' solo es válido en domain_1 (randomization).
    2. overall != 'n/a' nunca.
    3. overall='low' es incompatible con cualquier domain en 'high' o 'some'
       (excepto D1='n/a').

    Lanza ValueError si alguna regla se viola — el caller la maneja como
    una falla recuperable (paper queda sin assessment).
    """
    if assessment.overall.judgment == "n/a":
        raise ValueError("overall judgment cannot be 'n/a'")

    domains_no_d1 = [
        ("D2", assessment.domain_2_deviations.judgment),
        ("D3", assessment.domain_3_missing_data.judgment),
        ("D4", assessment.domain_4_outcome_meas.judgment),
        ("D5", assessment.domain_5_reporting.judgment),
    ]
    for label, j in domains_no_d1:
        if j == "n/a":
            raise ValueError(f"{label} cannot be 'n/a' (only domain_1 allows it)")

    if assessment.overall.judgment == "low":
        bad = [label for label, j in domains_no_d1 if j in ("some", "high")]
        if bad:
            raise ValueError(
                f"overall='low' inconsistent with {bad} not at 'low'"
            )


def _prune_for_rob(paper: dict) -> dict:
    """Reduce el paper a lo que el evaluador RoB necesita ver.

    RoB 2.0 evalúa METODOLOGÍA, no contenido. No mandamos results completos
    ni variables — el evaluador no necesita saber qué resultaron sino cómo
    se midieron y cómo se asignaron grupos.
    """
    return {
        "paper_id":     paper.get("paper_id"),
        "title":        paper.get("title"),
        "study_design": paper.get("study_design"),
        "methodology":  paper.get("methodology"),
        "sample":       paper.get("sample", {}),
        "limitations":  paper.get("limitations"),
        "source_fragments": paper.get("source_fragments", {}),
    }


async def _assess_paper(paper: dict, output_language: str) -> dict | None:
    """Evalúa un paper. Devuelve el dict con paper_id + 6 domains, o None si falla."""
    paper_id = paper.get("paper_id", "<unknown>")
    pruned = _prune_for_rob(paper)
    user_msg = f"PAPER TO ASSESS:\n{json.dumps(pruned, ensure_ascii=False)}"

    # Inyectar el idioma en el prompt antes de la llamada. Mismo patrón que
    # writer.py — un solo replace en el template del .md. El prompt tiene
    # múltiples ocurrencias de {output_language} (en la sección OUTPUT
    # LANGUAGE, en cada rationale del JSON template, y en CONSTRAINTS);
    # .replace() las reemplaza todas en un solo paso.
    system_prompt = ROB_ASSESSOR_PROMPT.replace("{output_language}", output_language)

    try:
        # Cap global de Featherless: 32B cuesta 2 units.
        async with featherless_credit(cost=COST_32B):
            response = await asyncio.wait_for(
                LLM_32B.chat.completions.create(
                    model=settings.model_32b_name,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user",   "content": user_msg},
                    ],
                    # Bajo temp: queremos juicios consistentes, no creatividad.
                    temperature=0.2,
                    # Extra para el bloque <think> de R1 + el JSON final.
                    max_tokens=4096,
                ),
                timeout=TIMEOUT_S,
            )

        raw_text = response.choices[0].message.content
        parsed_json = extract_json_from_response(raw_text)
        validated = RoBAssessment(**parsed_json)
        _validate_rob_consistency(validated)

        return {
            "paper_id": paper_id,
            **validated.model_dump(),
        }

    except asyncio.TimeoutError:
        logger.warning(
            "rob_assessor: timeout en paper %s tras %.0fs", paper_id, TIMEOUT_S,
            extra={"node": "rob_assessor", "paper_id": paper_id},
        )
        return None
    except (json.JSONDecodeError, ValidationError, ValueError) as e:
        logger.error(
            "rob_assessor: parse/validation error en paper %s: %s - %s",
            paper_id, type(e).__name__, e,
            extra={"node": "rob_assessor", "paper_id": paper_id},
        )
        return None
    except Exception as e:
        logger.exception(
            "rob_assessor: error inesperado en paper %s: %s", paper_id, e,
            extra={"node": "rob_assessor", "paper_id": paper_id},
        )
        return None


async def run_rob_assessor(state: AxiomState) -> dict:
    """Aplica RoB 2.0 a cada paper extraído (modo Cochrane only).

    Si no hay extractions, devuelve lista vacía sin error — el grafo
    sigue al clusterer normalmente. La concurrencia se limita a 4
    para no exceder el cap global de Featherless Premium.
    """
    extractions = state.get("extractions", [])
    if not extractions:
        logger.warning("rob_assessor: No extractions to assess.")
        return {"rob_assessments": []}

    # Idioma: respeta state["output_language"] si vino explícito, si no
    # autodetecta de state["question"]. Centralizado en utils/language.py
    # para que rob_assessor, grade_profiler y writer usen exactamente la
    # misma lógica.
    output_language = resolve_output_language(state)

    logger.info(
        "rob_assessor: evaluando %d papers con RoB 2.0 (concurrent=%d, timeout=%.0fs, lang=%s)...",
        len(extractions), MAX_CONCURRENT_PAPERS, TIMEOUT_S, output_language,
    )

    sem = asyncio.Semaphore(MAX_CONCURRENT_PAPERS)

    async def _gated(paper):
        async with sem:
            return await _assess_paper(paper, output_language)

    results = await asyncio.gather(
        *[_gated(p) for p in extractions],
        return_exceptions=True,
    )

    assessments: list[dict] = []
    errors: list[dict] = []

    for i, res in enumerate(results):
        paper_id = extractions[i].get("paper_id", "<unknown>")
        if isinstance(res, Exception):
            errors.append({
                "node": "rob_assessor",
                "paper_id": paper_id,
                "error": f"{type(res).__name__}: {res}",
            })
        elif res is None:
            errors.append({
                "node": "rob_assessor",
                "paper_id": paper_id,
                "error": "parse_timeout_or_validation",
            })
        else:
            assessments.append(res)

    logger.info(
        "rob_assessor: %d/%d evaluaciones completadas (%d errores).",
        len(assessments), len(extractions), len(errors),
    )

    return {"rob_assessments": assessments, "errors": errors}
