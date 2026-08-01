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

    parkee_proxy_url: str = ""
    parkee_model: str = "deepseek-v3"
    ig_basic_token: str = ""
    ig_user_token: str = ""

    redis_url: str = "redis://localhost:6379"
    database_url: str = "sqlite:///./data/klarifai.db"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
