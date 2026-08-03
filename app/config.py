"""Application configuration via environment variables."""

from pydantic_settings import BaseSettings
from pydantic import Field
from typing import Optional


class Settings(BaseSettings):
    meta_app_id: str = "1322990666256367"
    meta_app_secret: str = ""
    meta_app_secret_2: str = ""
    meta_skip_signature: bool = Field(default=False, alias="META_SKIP_SIGNATURE_CHECK")
    meta_page_id: str = ""
    meta_page_access_token: str = ""
    meta_verify_token: str = "klarifai_verify_2026"

    ig_business_id: str = ""

    # LLM access. Priority: FACTBOT_PROXY_URL (company proxy, future) →
    # DEEPSEEK_API_KEY (direct API, current dev). FACTBOT_MODEL default flash.
    factbot_proxy_url: str = ""
    factbot_proxy_key: str = ""
    factbot_model: str = "deepseek-v4-flash"
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_model: str = "deepseek-v4-flash"
    ig_basic_token: str = ""
    ig_user_token: str = ""

    redis_url: str = "redis://localhost:6379"
    database_url: str = "sqlite:///./data/klarifai.db"

    # ---- Deploy VPS: LLM proxy key + FactBot web API (publish report) ----
    factbot_api_url: str = "https://factbot.tech/api/v1/reports"
    factbot_api_key: str = ""
    factbot_timeout: float = 30.0      # timeout upload report (>= 30s)

    # ---- Pipeline analisa (LLM OpenAI-compatible) ----
    llm_timeout_seconds: float = 30.0

    # ---- Pipeline video (faster-whisper / yt-dlp) ----
    whisper_model: str = "small"          # base | small
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    whisper_language: str = "id"
    max_video_seconds: int = 180

    # ---- Worker (app/worker.py — service bot-worker) ----
    worker_concurrency: int = 2
    worker_poll_seconds: float = 2.0
    worker_heartbeat_seconds: float = 15.0
    worker_heartbeat_stale_seconds: float = 60.0
    worker_max_attempts: int = 3       # max retry per job (transient error)

    # ---- Progress notifier (app/pipeline/progress.py — DM progress bertahap) ----
    progress_enabled: bool = True
    progress_min_interval_seconds: float = 20.0
    progress_slow_after_seconds: float = 30.0

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
