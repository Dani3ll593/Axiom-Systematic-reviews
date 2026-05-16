"""Agent 2 — Screener PRISMA en cascada (Qwen 7B FT → QwQ-32B).
 
Recibe `papers_found` del searcher (arXiv, OpenAlex, Crossref, Scielo),
evalúa cada abstract contra los criterios PRISMA del estado y enruta cada
decisión a:
 
  - `screened_papers` ← decision == "include" OR "uncertain"
  - `papers_excluded` ← decision == "exclude"   (auditoría / PRISMA flow)
  - `errors`          ← fallos no fatales (timeout, red, validación)
 
Cascada
-------
El 7B fine-tuned hace el primer pase. Si su `confidence == "low"` o
`decision == "uncertain"`, se re-evalúa con QwQ-32B. El veredicto del 32B
es final; si el 32B falla, conservamos el del 7B (`route="7b_fallback"`)
en lugar de perder el paper.
 
Estrategia frente a la latencia de Scielo
-----------------------------------------
Scielo p50 ~4.2s (axiom_stack_completo2.md § Riesgo 5). El searcher la
aísla con timeout dedicado y `return_exceptions=True`, así que pueden
llegar papers con `abstract` vacío al screener. NO los mandamos al LLM
(gasto inútil de tokens y de la cuota de vLLM): short-circuit a
`decision="uncertain", reason="unavailable_full_text"` — valor permitido
por `prisma_criteria_template.json § exclusion_reasons_fixed_list`. El
paper queda en `screened_papers` y el writer lo reportará en la sección
de "acceso restringido".
"""
from __future__ import annotations

import asyncio
import re
import json
import ast
import logging
from typing import Literal, Optional

import instructor
from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from axiom_backend.config import settings
from axiom_backend.prompts import (
    SCREENER_PROMPT_7B,
    SCREENER_FEWSHOT_7B,
    SCREENER_PROMPT_32B,
    SCREENER_FEWSHOT_32B,
)
from axiom_backend.state import AxiomState
from axiom_backend.tools.llm_router import LLM_FEATHERLESS, featherless_credit, COST_7B, COST_32B

logger = logging.getLogger(__name__)

# --- Tunables ---
# Mantengo concurrencia=1 en ambos nodos porque subir MAX_CONCURRENT a 4
# en la versión monolítica disparaba 429s desde Featherless (probablemente
# por competencia con otros nodos del grafo). Cambiar estos valores requiere
# medir el budget global de units, no solo el del screener.
MAX_CONCURRENT_7B = 1
MAX_CONCURRENT_32B = 1
LLM_TIMEOUT_S = 90.0
ESCALATE_CONFIDENCES = {"low"}
ESCALATE_DECISIONS = {"uncertain"}

# --- Schema Definitions ---
class CriteriaMet(BaseModel):
    population: Optional[bool] = False
    intervention: Optional[bool] = False
    outcomes: Optional[bool] = False
    study_design: Optional[bool] = False
    temporal: Optional[bool] = False
    language: Optional[bool] = False

class ScreenerDecision(BaseModel):
    chain_of_thought: Optional[str] = None
    justification: str
    criteria_met: CriteriaMet
    confidence: Literal["high", "medium", "low"]
    reason: Optional[str] = None
    decision: Literal["include", "exclude", "uncertain"]

# --- LLM Client Setup ---
_clients: dict = {}

def _get_client(base_url: str) -> instructor.AsyncInstructor:
    """Devuelve un cliente instructor envolviendo Featherless.

    El argumento `base_url` queda como cache key para no romper la firma —
    en la práctica siempre apunta a Featherless. Los callers que pasan
    settings.vllm_url_7b / vllm_url_32b ahora pasan strings dummy
    ('FEATHERLESS_7B', 'FEATHERLESS_32B') que solo discriminan en cache.
    """
    if base_url not in _clients:
        _clients[base_url] = instructor.from_openai(
            LLM_FEATHERLESS,
            mode=instructor.Mode.JSON,
        )
    return _clients[base_url]

def limpiar_respuesta_qwq(texto_crudo: str) -> dict | None:
    """El extractor definitivo: Combina XML, Markdown, y Conteo de Llaves."""
    bloque_json = None
    
    # Intento 1: Buscar etiquetas XML <json>
    match_xml = re.search(r'<json>\s*(\{.*?\})\s*</json>', texto_crudo, re.DOTALL | re.IGNORECASE)
    if match_xml:
        bloque_json = match_xml.group(1)
    else:
        # Intento 2: Buscar bloque Markdown ```json
        match_md = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', texto_crudo, re.DOTALL | re.IGNORECASE)
        if match_md:
            bloque_json = match_md.group(1)
            
    # Intento 3: Búsqueda cruda de llaves a la fuerza
    if not bloque_json:
        match_llave = re.search(r'\{\s*["\'](chain_of_thought|justification)["\']', texto_crudo)
        inicio = match_llave.start() if match_llave else texto_crudo.find('{')
        if inicio != -1:
            sub_texto = texto_crudo[inicio:]
            llaves = 0
            for i, char in enumerate(sub_texto):
                if char == '{': llaves += 1
                elif char == '}':
                    llaves -= 1
                    if llaves == 0:
                        bloque_json = sub_texto[:i+1]
                        break

    if not bloque_json:
        return None

    # --- LAVADORA DE TEXTO ---
    bloque_json = bloque_json.replace('\xa0', ' ').replace('\t', ' ')
    bloque_json = re.sub(r'//.*', '', bloque_json) # Quita comentarios de una línea
    bloque_json = re.sub(r'/\*.*?\*/', '', bloque_json, flags=re.DOTALL) # Quita comentarios multilínea
    bloque_json = re.sub(r',\s*}', '}', bloque_json) # Quita trailing commas
    bloque_json = re.sub(r',\s*\]', ']', bloque_json)

    # Parser principal
    try:
        return json.loads(bloque_json)
    except json.JSONDecodeError:
        # Parser de rescate (AST) para comillas simples
        try:
            python_str = bloque_json.replace('true', 'True').replace('false', 'False').replace('null', 'None')
            datos = ast.literal_eval(python_str)
            if isinstance(datos, dict):
                return datos
        except Exception:
            pass
            
    return None

def _empty_abstract_decision() -> ScreenerDecision:
    """Short-circuit determinista para papers sin abstract (Scielo/Crossref)."""
    return ScreenerDecision(
        justification=(
            "Abstract not available; cannot evaluate eligibility from "
            "metadata alone."
        ),
        criteria_met=CriteriaMet(
            population=False, intervention=False, outcomes=False,
            study_design=False, temporal=False, language=False,
        ),
        confidence="low",
        reason="unavailable_full_text",
        decision="uncertain",
    )

async def _screen_one(
    paper: dict,
    prisma_json: str,
    model: str,
    base_url: str,
    prior_decision: dict | None = None,
) -> ScreenerDecision | None:
    """Ejecuta una llamada LLM con validación Pydantic estructurada.

    Selecciona el prompt y el fewshot según el modelo:
      - 7B → SCREENER_PROMPT_7B + SCREENER_FEWSHOT_7B (rol: first reviewer).
      - 32B → SCREENER_PROMPT_32B + SCREENER_FEWSHOT_32B (rol: adjudicator
              que recibe `prior_decision` en el user message para enfocar
              su reasoning en lo que el 7B no pudo resolver).
    """
    client = _get_client(base_url)

    is_32b = (model == settings.model_32b_name)

    # str.replace preserves actual JSON brackets within the prompt.
    if is_32b:
        system_prompt = SCREENER_PROMPT_32B.replace("{prisma_criteria_json}", prisma_json)
        fewshot       = SCREENER_FEWSHOT_32B
    else:
        system_prompt = SCREENER_PROMPT_7B.replace("{prisma_criteria_json}", prisma_json)
        fewshot       = SCREENER_FEWSHOT_7B

    # Para el 32B, anteponemos el veredicto del 7B al abstract en el user
    # message para que el modelo lo razone explícitamente. Para el 7B no
    # hay prior_decision (es el primer reviewer).
    abstract = paper.get("abstract", "")
    if is_32b and prior_decision:
        user_msg = (
            f"PRIOR REVIEWER VERDICT (from 7B model):\n"
            f"{json.dumps(prior_decision, ensure_ascii=False, indent=2)}\n\n"
            f"ABSTRACT:\n{abstract}"
        )
    else:
        user_msg = f"ABSTRACT:\n{abstract}"

    try:
        mensajes = [
            {"role": "system", "content": system_prompt},
            {"role": "system", "content": f"Examples:\n{fewshot}"},
            {"role": "user",   "content": user_msg},
        ]

        # --- RAMA QWQ-32B: cliente raw + parser <json> ---
        # NOTA: el formato <json>...</json> ahora está dentro del prompt 32B,
        # no se inyecta aquí. Esto elimina el `esquema_json` que antes vivía
        # en runtime y duplicaba lo que ya estaba en el prompt.
        if is_32b:
            try:

                # Pre-binding: declaramos `texto` antes del try interno para que el
                # except pueda referenciarlo aunque la excepción ocurra durante la
                # llamada HTTP (antes de que `texto` se asigne). Sin esto, el log
                # del except crashearía con UnboundLocalError cuando el fallo es
                # 429/timeout HTTP.
                texto: str | None = None

                # Reutilizamos el cliente Featherless global en vez de crear uno nuevo —
                # antes esto creaba un AsyncOpenAI por llamada al 32B, lo cual
                # malgastaba conexiones del pool y se saltaba el semáforo global.
                raw_client = LLM_FEATHERLESS
                # Semáforo credit-based: el 32B cuesta 2 units, así que con
                # cap=4 solo 2 papers pueden estar en flight simultáneamente.
                # Antes este sitio usaba FEATHERLESS_SEMAPHORE (legacy shim que
                # adquiere 1 unit) → sub-contaba y disparaba 429s en cascada
                # cuando otros agentes 32B estaban activos.
                async with featherless_credit(cost=COST_32B):
                    respuesta_cruda = await asyncio.wait_for(
                        raw_client.chat.completions.create(
                            model=model,
                            messages=mensajes,
                            temperature=0.3,
                            max_tokens=4096,
                            # Quitamos extra_body porque vLLM lo está ignorando
                        ),
                        timeout=600.0,
                    )
                
                # Featherless (y otros providers de modelos R1-style) separa
                # el reasoning del answer: cuando un modelo emite tags como
                # <think>...</think><json>...</json>, el provider rutea el
                # output a `message.reasoning` y deja `content` vacío.
                # Leemos ambos campos para cubrir los dos formatos:
                #   - OpenAI estándar       → content tiene el texto completo
                #   - Featherless R1-style  → content vacío, reasoning lo tiene
                # limpiar_respuesta_qwq ya sabe parsear ambos casos porque
                # busca <json>...</json> en el texto crudo.
                msg = respuesta_cruda.choices[0].message
                texto = msg.content or getattr(msg, "reasoning", None) or ""

                if not texto:
                    raise ValueError("Texto vacío")
                    
                # 2. Usamos nuestra función limpiadora a prueba de balas
                datos = limpiar_respuesta_qwq(texto)
                
                if datos:
                    return ScreenerDecision(**datos)
                else:
                    raise ValueError("No se pudo extraer JSON válido del texto libre.")
                    
            except Exception as e:
                # DIAGNOSTIC: antes de fallar al 7B, logueamos QUÉ falló.
                # Esto reemplaza el silencio anterior — sin esto era imposible
                # diferenciar parse error vs timeout vs 429 vs validation.
                #
                # Nivel 1: tipo + mensaje (siempre disponible)
                logger.error(
                    "screener: 32b inner exception: %s - %s",
                    type(e).__name__, str(e)[:300],
                    extra={
                        "paper_id": paper.get("paper_id"),
                        "model": model,
                        "node": "screener",
                    },
                )
                # Nivel 2: preview del output crudo (solo si llegamos a obtenerlo).
                # 500 chars suelen ser suficientes para ver si el modelo respetó
                # las tags <json> o devolvió algo inesperado.
                if texto:
                    preview = texto[:500].replace("\n", " ")
                    logger.error(
                        "screener: 32b raw output preview (500 chars): %r",
                        preview,
                        extra={
                            "paper_id": paper.get("paper_id"),
                            "node": "screener",
                        },
                    )
                # Comportamiento intacto: re-raise para caer al fallback del 7B.
                raise ValueError("Fallo en 32B")


        # --- COMPORTAMIENTO NORMAL PARA QWEN 7B ---
        else:
            # Semáforo credit-based: el 7B cuesta 1 unit, hasta 4 papers paralelos.
            # MAX_CONCURRENT local también es 4, así que el cuello de botella
            # es Featherless. Este lock previene los 429 que veíamos en run 2.
            async with featherless_credit(cost=COST_7B):
                return await asyncio.wait_for(
                    client.chat.completions.create(
                        model=model,
                        response_model=ScreenerDecision,
                        messages=mensajes,
                        max_retries=3,
                        presence_penalty=0.0,
                        frequency_penalty=0.0,
                        temperature=0.3,
                        max_tokens=1024,
                    ),
                    timeout=LLM_TIMEOUT_S,
                )

    except (ValidationError, json.JSONDecodeError, asyncio.TimeoutError, ValueError) as e:
        # DIAGNOSTIC: estos son los fallos esperables del 7B con instructor
        # (validation de schema, parse JSON, timeout, value error). Antes
        # caían en silencio y solo aparecían como "screener_7b_failed".
        # Ahora logueamos qué pasó para poder diagnosticar.
        # Simétrico al patch que aplicamos al 32B.
        logger.error(
            "screener: %s inner exception: %s - %s",
            "32b" if is_32b else "7b",
            type(e).__name__, str(e)[:300],
            extra={
                "paper_id": paper.get("paper_id"),
                "model": model,
                "node": "screener_32b" if is_32b else "screener_7b",
            },
        )
        return None
    except Exception as e:
        logger.exception(
            f"screener LLM call failed: {str(e)}",
            extra={
                "paper_id": paper.get("paper_id"),
                "model": model,
                "node": "screener_32b" if is_32b else "screener_7b",
            },
        )
        return None

def _should_escalate(d: ScreenerDecision) -> bool:
    return d.confidence in ESCALATE_CONFIDENCES or d.decision in ESCALATE_DECISIONS


# --- LangGraph Nodes ---
async def screener_7b_node(state: AxiomState) -> dict:
    """Fase 1 del screening: corre Qwen 7B sobre todos los papers.

    Ramifica en tres outputs:
      - `screened_papers`  ← decisiones 7B definitivas (no requieren escalación)
                             incluye papers sin abstract (route="skip_no_abstract").
      - `papers_excluded`  ← exclusiones 7B definitivas (high/medium confidence).
      - `papers_to_escalate` ← papers con confidence=low o decision=uncertain.
                               Cada uno con su `_7b_decision` adjunta para
                               que screener_32b pueda hacer fallback si el
                               32B falla.

    El reducer del state es `operator.add` para screened/excluded/errors, así
    que screener_32b puede acumular sin pisar lo que dejamos aquí.
    """
    papers = state.get("papers_found", [])
    prisma = state.get("prisma_criteria") or {}

    if not papers:
        logger.warning("screener_7b: papers_found empty, nothing to screen", extra={"node": "screener_7b"})
        return {}

    prisma_json = json.dumps(prisma, ensure_ascii=False)
    sem = asyncio.Semaphore(MAX_CONCURRENT_7B)

    async def _gated(p: dict) -> tuple[ScreenerDecision | None, bool]:
        """Retorna (decision, is_short_circuit).

        is_short_circuit=True solo cuando el paper no tiene abstract — no
        gastamos LLM, retornamos `_empty_abstract_decision()` directamente.
        El loop usa este flag (NO `result.reason`) para mapear a la route
        `skip_no_abstract`. Esto distingue el caso determinista del caso en
        que el MODELO retorne `reason=unavailable_full_text`, que es señal
        legítima de ambigüedad y debe escalar al 32B.
        """
        if not (p.get("abstract") or "").strip():
            return _empty_abstract_decision(), True
        async with sem:
            decision = await _screen_one(
                p, prisma_json, settings.model_7b_name, "FEATHERLESS_7B",
            )
            return decision, False

    results = await asyncio.gather(
        *(_gated(p) for p in papers),
        return_exceptions=True,
    )

    screened: list[dict] = []
    excluded: list[dict] = []
    to_escalate: list[dict] = []
    errors: list[dict] = []

    for paper, result in zip(papers, results):
        pid = paper.get("paper_id", "unknown_id")

        if isinstance(result, Exception):
            errors.append({
                "node": "screener_7b",
                "paper_id": pid,
                "error": f"{type(result).__name__}: {result}",
            })
            continue

        decision, is_short_circuit = result

        # Caso 1: paper sin abstract → short-circuit determinista.
        # Conserva route legacy "skip_no_abstract" para no romper writer.
        if is_short_circuit:
            screened.append({
                **paper,
                "screening": {**decision.model_dump(), "route": "skip_no_abstract"},
            })
            continue

        # Caso 2: el modelo corrió pero falló (validation/timeout/parse).
        if decision is None:
            errors.append({
                "node": "screener_7b",
                "paper_id": pid,
                "error": "screener_7b_failed",
            })
            continue

        # Caso 3: el modelo decidió. Aplicar lógica de escalación normal.
        # Si el modelo retornó `reason=unavailable_full_text` aquí (con
        # abstract presente), `_should_escalate` lo capturará vía
        # `confidence=low` o `decision=uncertain` y lo mandará al 32B.
        if not _should_escalate(decision):
            screened_paper = {
                **paper,
                "screening": {**decision.model_dump(), "route": "7b"},
            }
            if decision.decision == "exclude":
                excluded.append(screened_paper)
            else:
                screened.append(screened_paper)
            continue

        # 7B requiere escalación → mandamos al 32B con el veredicto 7B
        # adjunto para usarlo como fallback si el 32B falla.
        to_escalate.append({
            **paper,
            "_7b_decision": decision.model_dump(),
        })

    logger.info(
        "screener_7b: %d incl/uncert directos, %d excl, %d to_escalate, %d errores (de %d papers)",
        len(screened), len(excluded), len(to_escalate), len(errors), len(papers),
    )

    return {
        "screened_papers":    screened,
        "papers_excluded":    excluded,
        "papers_to_escalate": to_escalate,
        "errors":             errors,
    }


async def screener_32b_node(state: AxiomState) -> dict:
    """Fase 2 del screening: corre QwQ-32B sobre los papers escalados.

    Consume `papers_to_escalate` (producido por screener_7b). Para cada paper:
      - 32B éxito → emite con route="32b".
      - 32B falla → fallback al veredicto 7B adjunto, route="7b_fallback".

    Si no hay nada que escalar, no-op (return {}).
    """
    to_escalate = state.get("papers_to_escalate", []) or []
    prisma = state.get("prisma_criteria") or {}

    if not to_escalate:
        logger.info("screener_32b: nothing to escalate, skipping", extra={"node": "screener_32b"})
        return {}

    prisma_json = json.dumps(prisma, ensure_ascii=False)
    sem = asyncio.Semaphore(MAX_CONCURRENT_32B)

    async def _gated(p: dict) -> ScreenerDecision | None:
        async with sem:
            # Pasamos el veredicto del 7B (adjunto por screener_7b_node) al
            # 32B como contexto. Esto convierte al 32B en "informed second
            # reviewer": en lugar de re-evaluar a ciegas, sabe qué dudó el
            # 7B y puede enfocar su reasoning en esos puntos.
            return await _screen_one(
                p, prisma_json, settings.model_32b_name, "FEATHERLESS_32B",
                prior_decision=p.get("_7b_decision"),
            )

    results = await asyncio.gather(
        *(_gated(p) for p in to_escalate),
        return_exceptions=True,
    )

    screened: list[dict] = []
    excluded: list[dict] = []
    errors:   list[dict] = []
    n_resolved = 0
    n_fallback = 0

    for paper, result in zip(to_escalate, results):
        pid = paper.get("paper_id", "unknown_id")
        # Reconstruimos el paper sin el campo interno antes de emitir hacia
        # el state global — `_7b_decision` es contrato privado del screener.
        paper_clean = {k: v for k, v in paper.items() if k != "_7b_decision"}

        if isinstance(result, Exception):
            errors.append({
                "node": "screener_32b",
                "paper_id": pid,
                "error": f"{type(result).__name__}: {result}",
            })
            # Aún así intentamos fallback al 7B si lo tenemos
            result = None

        if result is None:
            # Fallback: usar veredicto 7B adjunto.
            fallback_dump = paper.get("_7b_decision")
            if not fallback_dump:
                # Caso defensivo: no hay 7B decision adjunta (no debería pasar).
                errors.append({
                    "node": "screener_32b",
                    "paper_id": pid,
                    "error": "screener_32b_failed_no_fallback",
                })
                continue
            logger.warning(
                "screener_32b: 32b failed, keeping 7b veredict",
                extra={"paper_id": pid, "node": "screener_32b"},
            )
            fb = ScreenerDecision(**fallback_dump)
            screened_paper = {
                **paper_clean,
                "screening": {**fb.model_dump(), "route": "7b_fallback"},
            }
            if fb.decision == "exclude":
                excluded.append(screened_paper)
            else:
                screened.append(screened_paper)
            n_fallback += 1
            continue

        # 32B éxito → veredicto final.
        logger.info(
            "screener_32b: 32B took the final decision succesfully for the paper %s",
            pid, extra={"paper_id": pid, "node": "screener_32b"},
        )
        screened_paper = {
            **paper_clean,
            "screening": {**result.model_dump(), "route": "32b"},
        }
        if result.decision == "exclude":
            excluded.append(screened_paper)
        else:
            screened.append(screened_paper)
        n_resolved += 1

    logger.info(
        "screener_32b: %d a evaluar, %d resolved, %d fallback_to_7b, %d errores",
        len(to_escalate), n_resolved, n_fallback, len(errors),
    )

    return {
        "screened_papers": screened,
        "papers_excluded": excluded,
        "errors":          errors,
    }