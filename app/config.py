"""
Configuración central del microservicio.
Lee todas las variables de entorno desde el archivo .env
"""
from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # --- Aplicación ---
    APP_NAME: str = "Microservicio de Prospección Automatizada"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False

    # --- Base de Datos Local ---
    DATABASE_URL: str = "sqlite+aiosqlite:///./microservicio_leads.db"

    # --- Evolution API ---
    EVOLUTION_API_URL: str = "http://localhost:8080"
    EVOLUTION_API_KEY: str = ""
    EVOLUTION_INSTANCE: str = "mi-instancia"

    # --- LLM (por defecto OpenAI, compatible con cualquier API OpenAI-like) ---
    LLM_PROVIDER: str = "bedrock"  # "openai" | "gemini" | "ollama" | "bedrock"
    LLM_API_KEY: str = "placeholder"
    LLM_MODEL: str = "placeholder"
    LLM_BASE_URL: str = "placeholder"

    # --- AWS Bedrock ---
    AWS_REGION: str = "us-east-1"
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    BEDROCK_MODEL_ID: str = "us.anthropic.claude-3-5-sonnet-20241022-v2:0"


    # --- Worker de Envío ---
    WORKER_BATCH_SIZE: int = 5           # Leads por ciclo
    WORKER_DELAY_MIN: int = 45           # Segundos mínimos entre mensajes
    WORKER_DELAY_MAX: int = 120          # Segundos máximos entre mensajes
    WORKER_HORA_INICIO: int = 9          # Hora en que el worker empieza a enviar
    WORKER_HORA_FIN: int = 20            # Hora en que el worker deja de enviar
    WORKER_MAX_DIARIO: int = 250         # Límite de mensajes por día (anti-ban)

    # --- CRM Externo (Handoff) ---
    CRM_WEBHOOK_URL: str = ""            # URL del webhook del CRM principal
    CRM_WEBHOOK_SECRET: str = ""         # Token de seguridad para el CRM
    MAX_DUDAS_ANTES_HANDOFF: int = 3     # Cuántas respuestas DUDA antes de escalar

    # --- Seguridad del Webhook Entrante ---
    WEBHOOK_SECRET_TOKEN: str = "cambia-esto-en-produccion"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
