"""
Configuración global de Axiom.
Carga las variables desde el archivo .env de forma tipada y segura.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field

class Settings(BaseSettings):
    # ─── APIs de Búsqueda y Extracción ───
    pubmed_api_key: str | None = None
    openalex_api_key: str | None = None
    # El email es obligatorio para los "polite pools". Falla si está vacío.
    contact_email: str = Field(..., min_length=1)

    # ─── Featherless API (LLM Inference) ───
    # Reemplaza vLLM/MI300X. Cliente único, OpenAI-compatible.
    # Si featherless_api_key está vacío, llm_router falla al cargar — eso es
    # deliberado: no queremos boot silencioso con un client roto.
    featherless_api_key: str | None = None
    featherless_base_url: str = "https://api.featherless.ai/v1"
    # Cap GLOBAL de conexiones concurrentes (Premium = 4). Excederlo causa
    # 429s en cascada. El semáforo se aplica en llm_router.
    featherless_max_concurrent: int = 4

    # ─── Modelos por Rol ───
    # IDs deben coincidir con los disponibles en featherless.ai/models.
    model_7b_name: str = "Qwen/Qwen2.5-72B-Instruct"
    # Reasoning model (DeepSeek-R1) usado por: screener uncertain pass,
    # analyst_32b, gap_finder, rob_assessor, grade_profiler.
    model_32b_name: str = "deepseek-ai/DeepSeek-R1"
    # Writer model (Kimi-K2) — narrativa larga coherente para reportes APA 7.
    # Los agentes existentes aún usan model_32b_name; activar Kimi-K2 requiere
    # un cambio surgical en writer.py (próximo paso).
    model_writer_name: str = "moonshotai/Kimi-K2-Instruct"
    # Light reasoning (DeepSeek-V3) para analyst_7b — DEBE ser distinto a
    # model_32b_name para que el reconciler tenga señal de desacuerdo útil.
    # Activarlo requiere cambio en analyst_7b.py (próximo paso).
    model_light_reasoning_name: str = "deepseek-ai/DeepSeek-V3"

    # ─── Modo Cochrane (Risk of Bias + GRADE) ───
    # Kill-switch global del servidor. Si False, los nodos rob_assessor y
    # grade_profiler se SALTAN aunque state["cochrane_mode"] sea True.
    # Útil para desactivar Cochrane sin tocar código si Featherless está
    # rate-limitado o los modelos de reasoning están lentos.
    cochrane_mode_enabled: bool = True

    # Timeouts por agente Cochrane. Los archivos rob_assessor.py y
    # grade_profiler.py por ahora los tienen hardcoded como módulo-level
    # constants (TIMEOUT_S = 120.0 / 180.0). Cambio surgical pendiente:
    # reemplazar esos TIMEOUT_S por settings.cochrane_*_timeout_s.
    cochrane_rob_timeout_s:   float = 120.0
    cochrane_grade_timeout_s: float = 180.0

    # ─── Cola de Jobs del API ───
    # Antes hardcoded en axiom_api.py. Cap simultáneo de runs en cola.
    max_queue_size: int = 10

    # ─── Auth del Backend ───
    # Bearer token que el frontend envía en Authorization. axiom_api.py
    # ahora puede leerlo de settings en vez de os.environ.
    axiom_backend_api_key: str | None = None

    # ─── Rutas del Sistema ───
    chroma_persist_dir: str = "./data/chroma_db"

    # ─── Clusterer (BGE-M3 + AgglomerativeClustering) ───
    # Métrica coseno: threshold=distancia, NO similitud.
    #   0.30-0.40 → near-duplicates (muy estricto, muchos singletons)
    #   0.50      → mismo subtopic (default sensato para BGE-M3)
    #   0.60-0.70 → mismo dominio general (laxo, clusters grandes)
    cluster_distance_threshold: float = 0.7

    # Cota dura sobre el JSON serializado del cluster que se manda al analyst.
    # Con Featherless Premium (32K context) podemos subir vs el cap original
    # de 16K que asumía vLLM con ctx=8192. Margen: system prompt (~1400) +
    # max_tokens (4096) + buffer (~500) ≈ 6K, deja ~26K para user msg.
    analyst_max_user_chars: int = 28000

    # ─── UI y Streamlit ───
    streamlit_server_port: int = 8501

    # Permite ignorar variables extra en el .env que no usemos aquí
    model_config = SettingsConfigDict(
        env_file=".env", 
        env_file_encoding="utf-8", 
        extra="ignore"
    )

# Instancia global (singleton) para importar desde otros módulos
settings = Settings()