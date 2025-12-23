from __future__ import annotations

from functools import lru_cache
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://log_detector:log_detector@db:5432/log_detector"

    aws_ses_region: str = "us-east-1"
    aws_ses_access_key_id: str = ""
    aws_ses_secret_access_key: str = ""
    aws_ses_sender_email: str = ""

    alert_email_enabled: bool = True
    alert_email_to: str = ""

    base_url: str = "http://localhost:8000"
    dedupe_window_seconds: int = 300

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    def alert_recipients(self) -> List[str]:
        recipients = [addr.strip() for addr in self.alert_email_to.split(",")]
        return [addr for addr in recipients if addr]


@lru_cache
def get_settings() -> Settings:
    return Settings()
