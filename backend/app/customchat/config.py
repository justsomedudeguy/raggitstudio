from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CUSTOMCHAT_", env_file=Path(__file__).resolve().parents[3] / ".env")

    app_root: Path = Field(default_factory=lambda: Path(__file__).resolve().parents[3])
    lemonade_base_url: str = ""
    chat_model_id: str = ""
    embedding_model_id: str = ""
    reranker_model_id: str = ""
    classifier_model_id: str = ""
    chunk_tokens: int = 800
    overlap_tokens: int = 120
    crawl_max_pages: int = 25
    crawl_timeout_seconds: float = 12.0
    crawl_delay_seconds: float = 0.5
    status_timeout_seconds: float = 8.0

    @property
    def data_dir(self) -> Path:
        return self.app_root / "data"

    @property
    def database_path(self) -> Path:
        return self.data_dir / "customchat.db"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"

    @property
    def attachments_dir(self) -> Path:
        return self.data_dir / "attachments"


settings = Settings()
