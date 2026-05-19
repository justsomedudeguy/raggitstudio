from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CUSTOMCHAT_", env_file=".env")

    app_root: Path = Field(default_factory=lambda: Path(__file__).resolve().parents[3])
    lemonade_base_url: str = "http://127.0.0.1:13305/v1"
    chat_model_id: str = "Qwen3.5-4B-heretic-v2-i1-GGUF-Q4_K_M"
    embedding_model_id: str = "zembed-1-Q4_K_M-GGUF-Q4_K_M"
    reranker_model_id: str = "bge-reranker-v2-m3-Q8_0-GGUF"
    classifier_model_id: str = "Qwen3.5-4B-heretic-v2-i1-GGUF-Q4_K_M"
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
