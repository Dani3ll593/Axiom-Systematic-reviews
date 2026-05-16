"""Agent — GRADE Evidence Profiler.

Evalúa la certeza de la evidencia (High / Moderate / Low / Very Low) para
cada consensus cluster siguiendo GRADE (Guyatt GH et al., BMJ 2008;336:924).

Posición en el grafo: después de `reconciler`, antes de `gap_finder`. Solo
se ejecuta cuando state["cochrane_mode"] == True.

Reads:
    state["consensus_clusters"]: list[dict]   (output del reconciler)
    state["rob_assessments"]:    list[dict]   (output del rob_assessor)
    state["extractions"]:        list[dict]   (para study_design y sample.n)

Writes:
    state["consensus_clusters"]: list[dict]   (REEMPLAZA — escritura atómica)
        Cada cluster es enriquecido con:
          - grade_starting_certainty: "High" | "Low"
          - grade_downgrades:    list[{factor, severity, rationale}]   (5 entries)
          - grade_upgrades:      list[{factor, rationale}]             (0-3 entries)
          - grade_final_certainty: "High" | "Moderate" | "Low" | "Very Low"
          - grade_summary: str

Si la evaluación de un cluster falla (timeout, parse error), el cluster
original se conserva con grade_final_certainty = "not_assessed" en vez de
perderlo — el writer downstream maneja ese estado.
"""

import asyncio
import json
import logging

from pydantic import BaseModel, ValidationError, Field

from axiom_backend.state import AxiomState
from axiom_backend.config import settings
from axiom_backend.tools.llm_router import LLM_32B, extract_json_from_response, featherless_credit, COST_32B
from axiom_backend.prompts import GRADE_PROFILER_PROMPT

logger = logging.getLogger(__name__)

# --- Tunables ---
# GRADE es por cluster (no por paper) → menor cardinalidad. Mantenemos
# concurrencia baja porque cada evaluación es más pesada que un RoB
# individual (razona sobre múltiples papers + 5 downgrades + upgrades).
MAX_CONCURRENT_CLUSTERS = 2
TIMEOUT_S = settings.cochrane_grade_timeout_s



# --- Esquemas (espejo de grade_profiler_prompt.txt § Output Format) ---
class GradeDowngrade(BaseModel):
    factor: str = Field(
        ...,
        pattern="^(risk_of_bias|inconsistency|indirectness|imprecision|publication_bias)$",
    )
    severity: str = Field(..., pattern="^(none|serious|very_serious)$")
    rationale: str = Field(..., min_length=1)


class GradeUpgrade(BaseModel):
    factor: str = Field(
        ...,
        pattern="^(large_effect|dose_response|plausible_confounding)$",
    )
    rationale: str = Field(..., min_length=1)


class GradeOutput(BaseModel):
    starting_certainty: str = Field(..., pattern="^(High|Low)$")
    downgrades: list[GradeDowngrade]
    upgrades:   list[GradeUpgrade]
    final_certainty: str = Field(..., pattern="^(High|Moderate|Low|Very Low)$")
    summary: str = Field(..., min_length=1)


# --- Niveles GRADE como índices para el check aritmético ---
# Orden: 0=Very Low, 1=Low, 2=Moderate, 3=High. Permite sumar/restar pasos.
_CERTAINTY_INDEX = {"Very Low": 0, "Low": 1, "Moderate": 2, "High": 3}


def _validate_grade_arithmetic(grade: GradeOutput) -> None:
    """Verifica que final_certainty sea consistente con downgrades/upgrades.

    Aritmética:
        net_steps = sum(severity_value) − len(upgrades)
        expected_idx = clamp(starting_idx − net_steps, 0, 3)

    DeepSeek-R1 a veces produce JSON coherente individualmente pero con
    arithmetic que no cuadra — preferimos rechazar y reintentar a aceptar
    una salida inconsistente.
    """
    severity_map = {"none": 0, "serious": 1, "very_serious": 2}
    downgrade_steps = sum(severity_map[d.severity] for d in grade.downgrades)

    # Upgrades solo cuentan si starting_certainty es Low (regla GRADE).
    upgrade_steps = len(grade.upgrades) if grade.starting_certainty == "Low" else 0
    if grade.starting_certainty == "High" and len(grade.upgrades) > 0:
        raise ValueError(
            f"upgrades not allowed when starting_certainty=High "
            f"(got {len(grade.upgrades)} upgrades)"
        )

    starting_idx = _CERTAINTY_INDEX[grade.starting_certainty]
    expected_idx = max(0, min(3, starting_idx - downgrade_steps + upgrade_steps))
    actual_idx = _CERTAINTY_INDEX[grade.final_certainty]

    if expected_idx != actual_idx:
        expected_label = [k for k, v in _CERTAINTY_INDEX.items() if v == expected_idx][0]
        raise ValueError(
            f"final_certainty='{grade.final_certainty}' inconsistent with arithmetic: "
            f"start={grade.starting_certainty} −{downgrade_steps} +{upgrade_steps} "
            f"= {expected_label}"
        )


def _build_cluster_payload(
    cluster: dict,
    rob_by_paper: dict[str, dict],
    extr_by_paper: dict[str, dict],
) -> dict:
    """Construye el payload compacto que ve el LLM por cluster.

    Le damos al modelo lo MÍNIMO que necesita para juzgar los 5 downgrades
    y eventuales upgrades:
      - Claim + heterogeneidad + contradicciones (Inconsistency)
      - Por paper: study_design (Starting certainty + Indirectness),
                   sample_n (Imprecision),
                   rob_overall (Risk of bias).

    Lo que NO mandamos: full extractions (results, variables, etc.).
    El payload sería 10x más grande sin aportar a las 5 dimensiones GRADE.
    """
    paper_ids = cluster.get("paper_ids", [])

    paper_summaries = []
    for pid in paper_ids:
        extr = extr_by_paper.get(pid, {})
        rob  = rob_by_paper.get(pid, {})
        paper_summaries.append({
            "paper_id":     pid,
            "study_design": extr.get("study_design", "unknown"),
            "sample_n":     extr.get("sample", {}).get("n"),
            "rob_overall":  rob.get("overall", {}).get("judgment", "not_assessed"),
        })

    return {
        "core_claim":             cluster.get("core_claim"),
        "agreement_percentage":   cluster.get("agreement_percentage"),
        "heterogeneity_detected": cluster.get("heterogeneity_detected"),
        "contradictions":         cluster.get("contradiction_quotes", {}),
        "paper_count":            len(paper_summaries),
        "papers":                 paper_summaries,
    }


def _fallback_cluster(cluster: dict, reason: str) -> dict:
    """Devuelve el cluster original con campos GRADE = not_assessed.

    Se usa cuando el LLM falla (timeout / parse / inconsistencia aritmética).
    Preservamos el cluster en vez de perderlo — el writer mostrará "GRADE
    no evaluado" para este outcome.
    """
    fallback = dict(cluster)
    fallback["grade_starting_certainty"] = "not_assessed"
    fallback["grade_downgrades"] = []
    fallback["grade_upgrades"]   = []
    fallback["grade_final_certainty"] = "not_assessed"
    fallback["grade_summary"] = f"GRADE evaluation failed: {reason}"
    return fallback


async def _grade_cluster(cluster: dict, payload: dict, cluster_idx: int) -> dict | None:
    """Evalúa GRADE en un cluster. Devuelve cluster enriquecido o None si falla."""
    user_msg = f"CLUSTER FOR GRADE ASSESSMENT:\n{json.dumps(payload, ensure_ascii=False)}"

    try:
        # Cap global de Featherless: 32B cuesta 2 units.
        async with featherless_credit(cost=COST_32B):
            response = await asyncio.wait_for(
                LLM_32B.chat.completions.create(
                    model=settings.model_32b_name,
                    messages=[
                        {"role": "system", "content": GRADE_PROFILER_PROMPT},
                        {"role": "user",   "content": user_msg},
                    ],
                    temperature=0.2,
                    max_tokens=4096,
                ),
                timeout=TIMEOUT_S,
            )

        raw_text = response.choices[0].message.content
        parsed_json = extract_json_from_response(raw_text)
        validated = GradeOutput(**parsed_json)
        _validate_grade_arithmetic(validated)

        merged = dict(cluster)
        merged["grade_starting_certainty"] = validated.starting_certainty
        merged["grade_downgrades"] = [d.model_dump() for d in validated.downgrades]
        merged["grade_upgrades"]   = [u.model_dump() for u in validated.upgrades]
        merged["grade_final_certainty"] = validated.final_certainty
        merged["grade_summary"] = validated.summary
        return merged

    except asyncio.TimeoutError:
        logger.warning(
            "grade_profiler: timeout en cluster %d tras %.0fs", cluster_idx, TIMEOUT_S,
            extra={"node": "grade_profiler", "cluster_index": cluster_idx},
        )
        return None
    except (json.JSONDecodeError, ValidationError, ValueError) as e:
        logger.error(
            "grade_profiler: parse/validation/arithmetic error en cluster %d: %s - %s",
            cluster_idx, type(e).__name__, e,
            extra={"node": "grade_profiler", "cluster_index": cluster_idx},
        )
        return None
    except Exception as e:
        logger.exception(
            "grade_profiler: error inesperado en cluster %d: %s", cluster_idx, e,
            extra={"node": "grade_profiler", "cluster_index": cluster_idx},
        )
        return None


async def run_grade_profiler(state: AxiomState) -> dict:
    """Aplica GRADE a cada consensus cluster (modo Cochrane only).

    Si un cluster falla evaluación, se preserva con grade_final_certainty
    = "not_assessed". NUNCA perdemos clusters — el writer downstream necesita
    todos los outcomes para construir la Summary of Findings table.
    """
    consensus_clusters = state.get("consensus_clusters", [])
    if not consensus_clusters:
        logger.warning("grade_profiler: No consensus_clusters to grade.")
        return {}

    # Indexar por paper_id (las listas no garantizan orden).
    rob_assessments = state.get("rob_assessments", [])
    extractions     = state.get("extractions", [])
    rob_by_paper  = {r["paper_id"]: r for r in rob_assessments if r.get("paper_id")}
    extr_by_paper = {e["paper_id"]: e for e in extractions     if e.get("paper_id")}

    logger.info(
        "grade_profiler: evaluando %d clusters (concurrent=%d, timeout=%.0fs)...",
        len(consensus_clusters), MAX_CONCURRENT_CLUSTERS, TIMEOUT_S,
    )

    sem = asyncio.Semaphore(MAX_CONCURRENT_CLUSTERS)

    async def _gated(cluster, idx):
        async with sem:
            payload = _build_cluster_payload(cluster, rob_by_paper, extr_by_paper)
            return await _grade_cluster(cluster, payload, idx)

    results = await asyncio.gather(
        *[_gated(c, i) for i, c in enumerate(consensus_clusters)],
        return_exceptions=True,
    )

    final_clusters: list[dict] = []
    errors: list[dict] = []

    for i, res in enumerate(results):
        if isinstance(res, Exception):
            errors.append({
                "node": "grade_profiler",
                "cluster_index": i,
                "error": f"{type(res).__name__}: {res}",
            })
            final_clusters.append(_fallback_cluster(consensus_clusters[i], str(res)))
        elif res is None:
            errors.append({
                "node": "grade_profiler",
                "cluster_index": i,
                "error": "parse_timeout_or_validation",
            })
            final_clusters.append(
                _fallback_cluster(consensus_clusters[i], "timeout or invalid output")
            )
        else:
            final_clusters.append(res)

    n_assessed = sum(
        1 for c in final_clusters
        if c.get("grade_final_certainty") not in (None, "not_assessed")
    )
    logger.info(
        "grade_profiler: %d/%d clusters evaluados correctamente.",
        n_assessed, len(consensus_clusters),
    )

    return {"consensus_clusters": final_clusters, "errors": errors}
