"""Application settings, loaded from the environment (and .env if present)."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the SignalStack API."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    DATABASE_URL: str = (
        "postgresql+psycopg://signalstack:signalstack@localhost:5432/signalstack"
    )
    CORS_ORIGINS: str = "http://localhost:5173"
    ENVIRONMENT: str = "local"

    @property
    def cors_origins_list(self) -> list[str]:
        """CORS_ORIGINS split into a list, ignoring blank entries."""
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]


settings = Settings()
