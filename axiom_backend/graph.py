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
    # El kill-switch llm_router.py se migra a este endpoint en el próximo paso.
    featherless_api_key: str | None = None
    featherless_base_url: str = "https://api.featherless.ai/v1"
    # Cap GLOBAL de conexiones concurrentes (Premium = 4). Excederlo causa
    # 429s en cascada. El semáforo se aplica en llm_router (post-migración).
    featherless_max_concurrent: int = 4

    # ─── Modelos por Rol ───
    # IDs deben coincidir con los disponibles en featherless.ai/models.
    model_7b_name: str = "Qwen/Qwen2.5-72B-Instruct"
    # Reasoning model (DeepSeek-R1) usado por: screener uncertain pass,
    # analyst_32b, gap_finder, rob_assessor, grade_profiler.
    model_32b_name: str = "deepseek-ai/DeepSeek-R1"
    # Writer model (Kimi-K2) — narrativa larga coherente para reportes APA 7.
    model_writer_name: str = "moonshotai/Kimi-K2-Instruct"
    # Light reasoning (DeepSeek-V3) para analyst_7b — DEBE ser distinto a
    # model_32b_name para que el reconciler tenga señal de desacuerdo útil.
    model_light_reasoning_name: str = "deepseek-ai/DeepSeek-V3"

    # ─── Servidores Locales (vLLM en AMD MI300X) — DEPRECATED ───
    # Mantenidos solo hasta que llm_router.py se migre a Featherless.
    # No tocar — eliminar junto con la refactorización del router.
    vllm_url_7b: str = "VLLM_URL_7B"
    vllm_url_32b: str = "VLLM_URL_32B"
    vllm_api_key: str | None = None

    # ─── Modo Cochrane (Risk of Bias + GRADE) ───
    # Kill-switch global del servidor. Si False, los nodos rob_assessor y
    # grade_profiler se SALTAN aunque state["cochrane_mode"] sea True.
    # Útil para desactivar Cochrane sin tocar código cuando Featherless está
    # rate-limitado o los modelos de reasoning están lentos.
    cochrane_mode_enabled: bool = True

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