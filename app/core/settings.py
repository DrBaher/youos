import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    app_name: str = "YouOS"
    version: str = "0.1.25"
    environment: str = "dev"
    instance_name: str = "YouOS"
    data_dir: Path | None = Field(default=None)  # YOUOS_DATA_DIR — instance root
    database_url: str = Field(default="sqlite:///var/youos.db")
    configs_dir: Path = Field(default=ROOT_DIR / "configs")

    model_config = SettingsConfigDict(
        env_prefix="YOUOS_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    @model_validator(mode="after")
    def _apply_data_dir(self) -> "Settings":
        """Derive database_url and configs_dir from data_dir when not set explicitly."""
        if self.data_dir is not None:
            data_dir = Path(self.data_dir).expanduser().resolve()
            if "YOUOS_DATABASE_URL" not in os.environ:
                self.database_url = f"sqlite:///{data_dir}/var/youos.db"
            if "YOUOS_CONFIGS_DIR" not in os.environ:
                self.configs_dir = data_dir / "configs"
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def get_instance_root() -> Path:
    """Return the root of the active instance — ``data_dir`` if set, else repo root.

    Use this when an absolute path under an instance is needed and the more
    specific helpers (``get_var_dir``, ``get_models_dir``, ``get_adapter_path``)
    don't apply — e.g. the per-instance ``youos_config.yaml``.
    """
    settings = get_settings()
    if settings.data_dir is not None:
        return Path(settings.data_dir).expanduser().resolve()
    return ROOT_DIR


def get_var_dir() -> Path:
    """Return the var/ directory for the active instance (or project root var/)."""
    return get_instance_root() / "var"


def get_models_dir() -> Path:
    """Return the models/ directory for the active instance.

    Houses ``adapters/latest/`` (the LoRA adapter) and any future per-instance
    model artifacts. Centralised here so doctor / teardown / status report on
    the same directory the fine-tune writer (``scripts.finetune_lora``) uses.
    """
    return get_instance_root() / "models"


def get_adapter_path() -> Path:
    """Return the LoRA adapter directory for the active instance.

    Honors YOUOS_DATA_DIR so per-instance fine-tunes land in (and are read
    from) the instance's own ``models/adapters/latest``. Without this every
    instance shared the repo-root adapter dir — fine-tunes overwrote each
    other and a generation reader looking at the instance dir would not find
    the adapter that the writer had put in the repo dir.
    """
    return get_models_dir() / "adapters" / "latest"
