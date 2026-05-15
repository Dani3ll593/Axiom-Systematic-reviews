"""
Enrutador central de LLMs (Featherless · OpenAI-compatible API).

Reemplaza la arquitectura previa de dos servidores vLLM (puertos 8000/8001).
Ahora hay UN cliente apuntando al endpoint Featherless, y la elección de
modelo se hace por llamada vía el parámetro `model` (que los agentes ya
pasan desde `settings.model_*_name`).

Sigue exponiendo `LLM_7B` y `LLM_32B` como aliases del mismo client para no
romper los imports en `searcher.py`, `screener.py`, `extractor.py`,
`analyst_7b.py`, `analyst_32b.py`, `gap_finder.py`, `writer.py`,
`rob_assessor.py` y `grade_profiler.py`.

Cap de concurrencia
-------------------
Featherless Premium permite 4 conexiones concurrentes. Excederlo dispara
429s en cascada. Hay DOS capas de protección:

1. Semáforos locales en cada agente (analyst_32b=2, analyst_7b=2,
   rob_assessor=4, grade_profiler=2, etc). El grafo serializa los nodos,
   así que en cualquier momento solo UN agente está corriendo — excepto el
   fan-out clusterer→(analyst_7b + analyst_32b) que suma 2+2=4 conexiones.

2. `FEATHERLESS_SEMAPHORE` global expuesto en este módulo. Los agentes
   NUEVOS deben usarlo vía el helper `featherless_call()`. Los agentes
   existentes seguirán funcionando por las protecciones locales del punto
   1, pero conviene migrarlos en un próximo paso para defense-in-depth.
"""
import ast
import asyncio
import json
import logging
import re

from openai import AsyncOpenAI

from axiom_backend.config import settings

logger = logging.getLogger(__name__)


# --- 1. Cliente Featherless único ---------------------------------------
# Featherless es OpenAI-compatible: usamos el SDK oficial y solo cambiamos
# base_url. Sin key, log warning y fail-first en la primera llamada (mejor
# que cargar silenciosamente y morir runtime con un error oscuro).
if not settings.featherless_api_key:
    logger.warning(
        "FEATHERLESS_API_KEY no está configurado. Las llamadas LLM fallarán "
        "con 401 Unauthorized hasta que se setee la variable."
    )

LLM_FEATHERLESS = AsyncOpenAI(
    base_url=settings.featherless_base_url,
    api_key=settings.featherless_api_key or "MISSING",
    # Timeout generoso: DeepSeek-R1 con <think> puede tardar 60-180s por
    # llamada. Cada agente además impone su propio timeout vía asyncio.wait_for.
    timeout=300.0,
)

# Aliases legacy — TODOS apuntan al mismo cliente. La distinción entre
# modelos ahora ocurre en el parámetro `model=` de cada `chat.completions.create`,
# que los agentes ya pasan correctamente vía settings.model_*_name.
LLM_7B = LLM_FEATHERLESS
LLM_32B = LLM_FEATHERLESS

# Aliases para los modelos nuevos (Kimi-K2 y DeepSeek-V3). Mismos client,
# distinguidos en runtime por `settings.model_writer_name` y
# `settings.model_light_reasoning_name`. Para usarlos hay que migrar
# writer.py y analyst_7b.py (próximo paso).
LLM_WRITER = LLM_FEATHERLESS
LLM_LIGHT  = LLM_FEATHERLESS


# --- 2. Semáforo global de concurrencia ---------------------------------
# Cap duro: settings.featherless_max_concurrent (default 4 = Premium plan).
# Los agentes NUEVOS deben usar `featherless_call()` (abajo) que adquiere
# este semáforo automáticamente.
FEATHERLESS_SEMAPHORE = asyncio.Semaphore(settings.featherless_max_concurrent)


async def featherless_call(
    model: str,
    messages: list[dict],
    *,
    temperature: float = 0.3,
    max_tokens: int = 4096,
    timeout: float | None = None,
    **kwargs,
) -> str:
    """Llamada a Featherless con el semáforo global aplicado.

    Retorna el `content` crudo de la respuesta (string). Si el caller
    necesita más metadata (logprobs, finish_reason), llama directamente
    a LLM_FEATHERLESS.chat.completions.create dentro de un bloque
    `async with FEATHERLESS_SEMAPHORE`.

    Raises:
        openai.APIError y subclases: errores de red / 4xx / 5xx.
        asyncio.TimeoutError: si `timeout` se excede.
    """
    async with FEATHERLESS_SEMAPHORE:
        coro = LLM_FEATHERLESS.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )
        if timeout is not None:
            response = await asyncio.wait_for(coro, timeout=timeout)
        else:
            response = await coro
    return response.choices[0].message.content


# --- 3. Utilidades de Parseo (intactas del router previo) ---------------
def _extract_balanced_json(text: str, start_pos: int = 0) -> str | None:
    """Encuentra el primer `{` desde `start_pos` y devuelve el substring hasta
    su `}` de cierre, contando llaves balanceadas Y respetando strings JSON.

    El tracking de strings es crítico: si el JSON tiene un valor como
    "executive_report_md": "Texto con {algo} entre llaves", esas llaves
    interiores NO se cuentan porque están dentro de un string. El algoritmo
    sigue la spec JSON: comilla doble abre/cierra string, backslash escapa
    el siguiente char (incluyendo otra comilla).

    Devuelve None si no encuentra un objeto bien cerrado.
    """
    start = text.find("{", start_pos)
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        c = text[i]
        # Dentro de un string: solo nos interesa el cierre del string y los escapes.
        # Las { y } literales aquí NO afectan el conteo de profundidad.
        if in_string:
            if escape:
                escape = False
                continue
            if c == "\\":
                escape = True
                continue
            if c == '"':
                in_string = False
            continue
        # Fuera de string: detectar apertura/cierre de strings y de objetos.
        if c == '"':
            in_string = True
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _sanitize_json_block(s: str) -> str:
    """Limpia ruido común en outputs de LLM antes de json.loads."""
    s = s.replace("\xa0", " ").replace("\t", " ")
    s = re.sub(r"//.*", "", s)                        # // comentarios línea
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.DOTALL)  # /* */ comentarios bloque
    s = re.sub(r",\s*([}\]])", r"\1", s)              # trailing commas
    return s.strip()


def extract_json_from_response(raw: str) -> dict:
    """Extrae JSON de la respuesta del LLM, robusto a múltiples formatos:

    - `<json>...</json>` (preferido si el prompt lo pide)
    - ```` ```json ... ``` ```` o ```` ``` ... ``` ```` (markdown fence)
    - `{...}` en texto libre (búsqueda de llaves balanceadas)

    Los bloques `<think>...</think>` de modelos de reasoning (QwQ-32B,
    DeepSeek-R1) se descartan antes de buscar. Si encuentra un candidato
    pero `json.loads` falla, intenta `ast.literal_eval` como rescate
    (cubre comillas simples y otros casos cuasi-Python).

    NOTE: el screener tiene una función equivalente `limpiar_respuesta_qwq` con
    heurísticas un poco distintas. A futuro convendría unificarlas.

    Raises:
        ValueError: si raw está vacío.
        json.JSONDecodeError: si no se logra extraer JSON válido. El mensaje
            incluye un preview de los primeros 500 chars de la respuesta cruda
            para diagnosticar qué devolvió el modelo.
    """
    if not raw or not raw.strip():
        raise ValueError("Received empty response from LLM")

    # 1. Quitar bloques <think>...</think> (incluso múltiples). Si hay <think>
    #    sin cerrar, sobrevive en el texto y caerá en error con preview claro.
    text = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)

    # 2. Reunir candidatos en orden de preferencia.
    #
    # ESTRATEGIA: las regex SOLO se usan para LOCALIZAR el área donde está el
    # JSON (entre <json>...</json> o ```json...```). El recorte del JSON
    # exacto lo hace SIEMPRE _extract_balanced_json, que respeta strings.
    #
    # Antes intentábamos `.*?` non-greedy para capturar el JSON entre las
    # tags, pero eso falla con JSONs grandes que contienen "{" o "}" literales
    # dentro de strings (typical en outputs del writer con markdown embebido).
    candidates: list[str] = []

    # 2a. Si las tags <json>...</json> están presentes, balancear DENTRO de
    # esa área (más rápido y robusto que buscar en todo el texto).
    m_open = re.search(r"<json>", text, re.IGNORECASE)
    if m_open:
        balanced = _extract_balanced_json(text, start_pos=m_open.end())
        if balanced:
            candidates.append(balanced)

    # 2b. Si hay un fence markdown ```json o ```, mismo approach.
    m_fence = re.search(r"```(?:json)?\s*", text, re.IGNORECASE)
    if m_fence:
        balanced = _extract_balanced_json(text, start_pos=m_fence.end())
        if balanced and balanced not in candidates:
            candidates.append(balanced)

    # 2c. Sin tags: balancear desde el primer { del texto entero.
    balanced = _extract_balanced_json(text)
    if balanced and balanced not in candidates:
        candidates.append(balanced)
    # PATCH-1-APPLIED

    # 3. Probar cada candidato con sanitización + parser principal + rescate AST.
    for c in candidates:
        cleaned = _sanitize_json_block(c)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            try:
                py_compat = (
                    cleaned.replace("true", "True")
                           .replace("false", "False")
                           .replace("null", "None")
                )
                parsed = ast.literal_eval(py_compat)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                continue

    # 4. Todo falló: error con preview para diagnosticar.
    preview = raw[:500].replace("\n", " ")
    logger.error(
        f"extract_json_from_response: no parseable JSON encontrado. "
        f"Raw preview (500 chars): {preview!r}"
    )
    raise json.JSONDecodeError(
        f"No se pudo extraer JSON válido. Raw preview: {preview!r}",
        raw, 0,
    )


# --- 4. Enrutador Principal ---------------------------------------------
async def route_task(task_type: str, messages: list[dict], **kwargs) -> str:
    """
    Enruta cada tarea al modelo adecuado según la complejidad cognitiva
    requerida. Aplica el semáforo global de Featherless.

    Args:
        task_type: Categoría de la tarea (ej. 'abstract_screening',
                   'gap_identification', 'rob_assessment', 'grade_profiling').
        messages: Lista de mensajes formato OpenAI.
        **kwargs: Parámetros adicionales (temperature, max_tokens, timeout, etc.)

    Returns:
        str: El contenido crudo de la respuesta del LLM.
    """
    # Routing por tarea. Tareas nuevas (RoB, GRADE, writer) usan los modelos
    # nuevos. Las tareas existentes siguen apuntando a model_7b_name /
    # model_32b_name — que ahora resuelven a Qwen2.5-72B y DeepSeek-R1
    # respectivamente vía el .env, sin tocar agentes.
    routing = {
        "search_decomposition":    settings.model_7b_name,
        "abstract_screening":      settings.model_7b_name,
        "pdf_extraction":          settings.model_7b_name,
        "contradiction_detection": settings.model_32b_name,
        "gap_identification":      settings.model_32b_name,
        "rob_assessment":          settings.model_32b_name,
        "grade_profiling":         settings.model_32b_name,
        "narrative_generation":    settings.model_writer_name,
        "light_reasoning":         settings.model_light_reasoning_name,
    }

    # Por defecto, si no conocemos la tarea, usamos el modelo grande por seguridad
    model_name = routing.get(task_type, settings.model_32b_name)

    logger.info(f"route_task: Enrutando '{task_type}' al modelo {model_name}")

    return await featherless_call(model=model_name, messages=messages, **kwargs)