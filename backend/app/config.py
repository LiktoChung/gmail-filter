from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_nested_delimiter="__",
    )

    google_client_id: str = ""
    google_client_secret: str = ""
    redirect_uri: str = "http://localhost:8000/api/auth/callback"
    data_dir: Path = Field(
        default=Path("/data"),
        validation_alias=AliasChoices("DATA_DIR", "data_dir"),
    )
    secret_key: str = "change-me-in-production"  # for future sessions if needed

    # Gmail API tuning (see https://developers.google.com/gmail/api/reference/rest/v1/users.messages/list)
    gmail_list_page_size: int = Field(
        default=500,
        ge=1,
        le=500,
        validation_alias=AliasChoices("GMAIL_LIST_PAGE_SIZE", "gmail_list_page_size"),
        description="messages.list maxResults per request (max 500)",
    )
    gmail_parallel_workers: int = Field(
        default=4,
        ge=1,
        le=128,
        validation_alias=AliasChoices("GMAIL_PARALLEL_WORKERS", "gmail_parallel_workers"),
        description="Parallel threads for per-message metadata.get during sync/search enrich",
    )
    gmail_http_timeout_seconds: int = Field(
        default=300,
        ge=30,
        le=600,
        validation_alias=AliasChoices("GMAIL_HTTP_TIMEOUT_SECONDS", "gmail_http_timeout_seconds"),
        description="Per-request HTTP timeout for Google API (seconds)",
    )
    gmail_enrich_chunk_size: int = Field(
        default=8,
        ge=1,
        le=200,
        validation_alias=AliasChoices("GMAIL_ENRICH_CHUNK_SIZE", "gmail_enrich_chunk_size"),
        description="Search-result enrich: metadata.get calls per chunk (avoids huge parallel bursts)",
    )
    gmail_sync_chunk_pause_seconds: float = Field(
        default=0.75,
        ge=0.0,
        le=60.0,
        validation_alias=AliasChoices(
            "GMAIL_SYNC_CHUNK_PAUSE_SECONDS", "gmail_sync_chunk_pause_seconds"
        ),
        description="Pause after each metadata chunk (sync job + /api/messages enrich)",
    )
    gmail_list_page_pause_seconds: float = Field(
        default=0.35,
        ge=0.0,
        le=60.0,
        validation_alias=AliasChoices(
            "GMAIL_LIST_PAGE_PAUSE_SECONDS", "gmail_list_page_pause_seconds"
        ),
        description="Pause after each messages.list page during sync",
    )
    gmail_adaptive_sync: bool = Field(
        default=True,
        validation_alias=AliasChoices("GMAIL_ADAPTIVE_SYNC", "gmail_adaptive_sync"),
        description="Ramp sync throughput until rate limits, then back off (sync job only)",
    )
    gmail_retry_initial_delay_seconds: float = Field(
        default=2.0,
        ge=0.5,
        le=300.0,
        validation_alias=AliasChoices(
            "GMAIL_RETRY_INITIAL_DELAY_SECONDS", "gmail_retry_initial_delay_seconds"
        ),
        description="First backoff delay on Gmail 403/429 quota errors",
    )
    gmail_retry_max_delay_seconds: float = Field(
        default=120.0,
        ge=1.0,
        le=600.0,
        validation_alias=AliasChoices("GMAIL_RETRY_MAX_DELAY_SECONDS", "gmail_retry_max_delay_seconds"),
        description="Max backoff delay between retries",
    )
    gmail_retry_max_attempts: int = Field(
        default=12,
        ge=1,
        le=30,
        validation_alias=AliasChoices("GMAIL_RETRY_MAX_ATTEMPTS", "gmail_retry_max_attempts"),
        description="Max attempts per Gmail API request (including retries)",
    )

    @property
    def token_path(self) -> Path:
        return self.data_dir / "tokens.json"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "gmail_cache.sqlite3"


settings = Settings()
