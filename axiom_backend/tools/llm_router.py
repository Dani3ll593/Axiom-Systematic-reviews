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


# --- 2. Semáforo global credit-based ------------------------------------
# CONTEXTO HISTÓRICO: antes esto era `asyncio.Semaphore(4)`, que cuenta
# REQUESTS, no UNITS. Pero Featherless cobra:
#   - Qwen-7B  → 1 unit por request
#   - QwQ-32B  → 2 units por request
# Plan feather_pro_plus = 4 units totales.
#
# Con el viejo Semaphore(4), era legal tener 4 requests del 32B en vuelo,
# que serían 8 units → 429 RateLimitError "concurrency_limit_exceeded".
#
# La versión credit-based abajo conoce los costos. Cada agente declara su
# costo (1 o 2). El semáforo libera cuando hay créditos suficientes, evitando
# 429s en origen en vez de "manejarlos mejor" (que era el approach viejo).
#
# Patrón de uso recomendado (nuevo):
#
#     async with featherless_credit(cost=2):       # 32B
#         response = await LLM_FEATHERLESS.chat.completions.create(...)
#
# El context manager adquiere y libera limpiamente incluso ante excepciones.

class CreditedSemaphore:
    """Semáforo basado en créditos (no en cantidad de requests).

    `acquire(cost)` espera hasta que haya `cost` créditos disponibles.
    `release(cost)` los devuelve y despierta a todos los waiters (notify_all
    porque un waiter durmiendo pidiendo 1 podría avanzar aunque solo se
    liberaran 2, etc — la espera no es FIFO estricta pero es starvation-free
    en la práctica para nuestras cargas pequeñas).

    Esto NO es un RateLimiter (no maneja tiempo, solo capacidad concurrente).
    Si en el futuro Featherless agrega rate limits por minuto/hora, eso vivirá
    en otra capa (probablemente con `aiolimiter` o equivalente).
    """

    def __init__(self, max_credits: int) -> None:
        self._max = max_credits
        self._available = max_credits
        # Lock + Condition: Condition usa el lock para sincronizar wait/notify.
        self._cond = asyncio.Condition()

    async def acquire(self, cost: int) -> None:
        if cost < 1 or cost > self._max:
            raise ValueError(
                f"cost={cost} fuera de rango [1, {self._max}]. "
                f"Si un modelo cuesta más que el plan, no hay forma de servirlo."
            )
        async with self._cond:
            # `while`, no `if`: la condición debe revalidarse después de cada
            # notify_all porque varios waiters pueden ser despertados pero solo
            # un subconjunto cabe con los créditos liberados.
            while self._available < cost:
                await self._cond.wait()
            self._available -= cost

    async def release(self, cost: int) -> None:
        async with self._cond:
            self._available += cost
            # No queremos overshoot por bugs upstream (release sin acquire previo).
            if self._available > self._max:
                logger.warning(
                    "CreditedSemaphore: release overshoot (available=%d > max=%d). "
                    "Capando al máximo. Esto indica un release sin acquire previo.",
                    self._available, self._max,
                )
                self._available = self._max
            self._cond.notify_all()

    @property
    def available(self) -> int:
        """Solo lectura, snapshot best-effort para diagnóstico/logging."""
        return self._available

    def credit(self, cost: int) -> "_CreditContext":
        """Devuelve un async context manager que adquiere+libera `cost` créditos.

        Uso: `async with sem.credit(cost=2): ...`
        """
        return _CreditContext(self, cost)


class _CreditContext:
    """Context manager para `CreditedSemaphore.credit(cost)`."""
    __slots__ = ("_sem", "_cost", "_acquired")

    def __init__(self, sem: CreditedSemaphore, cost: int) -> None:
        self._sem = sem
        self._cost = cost
        self._acquired = False

    async def __aenter__(self) -> "_CreditContext":
        await self._sem.acquire(self._cost)
        self._acquired = True
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        # Idempotente: solo libera si acquire tuvo éxito. Protege contra
        # CancelledError entre acquire y body (raro pero posible).
        if self._acquired:
            await self._sem.release(self._cost)
            self._acquired = False


# Instancia global. Cap = settings.featherless_max_concurrent (4 en Premium).
_FEATHERLESS_CREDITS = CreditedSemaphore(settings.featherless_max_concurrent)


def featherless_credit(cost: int) -> _CreditContext:
    """Context manager para adquirir `cost` créditos del cap global.

    Uso típico desde un agente:

        from axiom_backend.tools.llm_router import featherless_credit, LLM_FEATHERLESS

        async with featherless_credit(cost=2):     # 2 = 32B, 1 = 7B
            response = await LLM_FEATHERLESS.chat.completions.create(
                model=settings.model_32b_name,
                messages=...,
            )

    El cost es responsabilidad del caller — depende de qué modelo se va a usar.
    Convención: 7B=1, 32B=2. Si en el futuro Featherless cambia los costos por
    modelo, basta tocar la tabla de los callers (o centralizar en un helper).
    """
    return _FEATHERLESS_CREDITS.credit(cost)


# Costos conocidos de Featherless por familia de modelo. Centralizado aquí
# para que los agentes no hardcodeen 1/2. Si Featherless cambia los costos,
# este es el único sitio que toca actualizar.
#
# Ver https://featherless.ai/pricing (al momento del refactor):
#   - 7B  (Qwen2.5-7B-Instruct): 1 unit
#   - 32B (DeepSeek-R1-Distill-Qwen-32B): 2 units
#   - Modelos grandes (Kimi-K2, etc.) pueden costar más — TODO confirmar.
COST_7B  = 1
COST_32B = 2
COST_WRITER = 2   # Kimi-K2-Instruct — TODO: confirmar contra docs Featherless
COST_LIGHT  = 1


# DEPRECATED — alias hacia el cap viejo que contaba REQUESTS, NO UNITS.
# Solo lo conservamos para no romper imports legacy mientras migramos los
# agentes. Migrar a `featherless_credit(cost=N)` cuando sea posible.
#
# El nombre se mantiene pero la implementación ahora delega al credit-based
# usando cost=1 (asume que el caller es un 7B). Si el caller real es 32B,
# este alias sub-cuenta y los 429s vuelven. **No usar en código nuevo.**
class _LegacyFEATHERLESSSemaphoreShim:
    """Shim para `async with FEATHERLESS_SEMAPHORE`: adquiere 1 crédito (asume 7B).

    Si el agente caller hace 32B, esto es subcontar y eventualmente verá 429.
    Migrar el caller a `featherless_credit(cost=COST_32B)`.
    """
    async def __aenter__(self):
        await _FEATHERLESS_CREDITS.acquire(COST_7B)
        return self
    async def __aexit__(self, exc_type, exc, tb):
        await _FEATHERLESS_CREDITS.release(COST_7B)

FEATHERLESS_SEMAPHORE = _LegacyFEATHERLESSSemaphoreShim()


async def featherless_call(
    model: str,
    messages: list[dict],
    *,
    temperature: float = 0.3,
    max_tokens: int = 4096,
    timeout: float | None = None,
    cost: int | None = None,
    **kwargs,
) -> str:
    """Llamada a Featherless con el semáforo credit-based aplicado.

    Retorna el `content` crudo de la respuesta (string). Si el caller
    necesita más metadata (logprobs, finish_reason), llama directamente
    a LLM_FEATHERLESS.chat.completions.create dentro de un bloque
    `async with featherless_credit(cost=N)`.

    `cost` es el costo en units del modelo. Si no se pasa, se infiere del
    nombre del modelo (7B=1, 32B/Kimi=2). Si la inferencia falla, asume 1
    con warning — pasar cost=N explícito para evitar adivinanzas.

    Raises:
        openai.APIError y subclases: errores de red / 4xx / 5xx.
        asyncio.TimeoutError: si `timeout` se excede.
    """
    if cost is None:
        # Heurística simple por nombre. Para precisión, los callers deberían
        # pasar `cost` explícito o usar las constantes COST_7B/COST_32B/etc.
        m = (model or "").lower()
        if "32b" in m or "kimi" in m or "k2" in m:
            cost = COST_32B
        elif "7b" in m or "qwen2.5" in m:
            cost = COST_7B
        else:
            logger.warning(
                "featherless_call: no pude inferir cost para model=%r, "
                "usando cost=1. Pasa `cost=N` explícito para evitar warnings.",
                model,
            )
            cost = 1

    async with featherless_credit(cost=cost):
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