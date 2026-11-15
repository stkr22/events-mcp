"""Runtime configuration, loaded from ``EVENTS_*`` environment variables."""

from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Qwen3-Embedding is instruction-aware: queries carry a task prefix, documents do not.
DEFAULT_QUERY_PREFIX = (
    "Instruct: Given a question about local events, exhibitions or videos, "
    "retrieve the listings that answer it\nQuery: "
)


class Settings(BaseSettings):
    """Server and CLI settings."""

    model_config = SettingsConfigDict(env_prefix="EVENTS_", extra="ignore")

    database_url: SecretStr
    """libpq connection string, e.g. ``postgresql://events:pw@events-postgres-rw.events.svc:5432/events``."""

    mcp_token: SecretStr | None = None
    """Static bearer token required on every MCP request. Unset disables auth (local development only)."""

    host: str = "0.0.0.0"  # noqa: S104 - listens inside a pod; the network policy is the boundary
    port: int = 8000
    metrics_port: int = 9090

    embedding_base_url: str | None = None
    """OpenAI-compatible base URL, e.g. ``http://192.168.178.7:8080/v1``. Unset disables semantic search."""
    embedding_model: str = "Qwen3-Embedding-0.6B-8bit"
    embedding_dimensions: int = 1024
    embedding_api_key: SecretStr = SecretStr("local")
    embedding_query_prefix: str = DEFAULT_QUERY_PREFIX
    embedding_timeout_sec: float = 5.0
    embedding_retry_after_sec: float = 60.0
    """After a failed call, skip the endpoint for this long instead of waiting on every query."""

    timezone: str = "Europe/Berlin"
    """Zone used to read bare dates (``2026-11-21``) in tool arguments and to render times."""

    metrics_refresh_sec: float = 60.0

    @property
    def tz(self) -> ZoneInfo:
        """Configured zone as a ``ZoneInfo``."""
        return ZoneInfo(self.timezone)


@lru_cache
def get_settings() -> Settings:
    """Return the cached settings instance."""
    return Settings()
