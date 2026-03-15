from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    app_name: str = "YouOS"
    environment: str = "dev"
    database_url: str = Field(default="sqlite:///var/youos.db")
    configs_dir: Path = Field(default=ROOT_DIR / "configs")

    model_config = SettingsConfigDict(
        env_prefix="YOUOS_",
        env_file=".env",
        env_file_encoding="utf-8",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
