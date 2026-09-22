from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    app_name: str = "AI Checklist Hub"
    database_url: str = f"sqlite:///{(BASE_DIR / 'data' / 'checklist.db').as_posix()}"
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4.1-mini"
    webhook_token: str | None = None
    model_config = SettingsConfigDict(env_file=BASE_DIR.parent / ".env", extra="ignore")


settings = Settings()
