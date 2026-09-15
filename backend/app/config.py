"""Application settings, loaded from the environment (and .env if present)."""

from __future__ import annotations

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The dialect we actually have installed. Managed Postgres providers hand out
# bare `postgres://` or `postgresql://` URLs, which SQLAlchemy resolves to
# psycopg2 — a driver this project does not install. Rewriting the scheme is
# cheaper and less error-prone than asking every operator to remember the
# `+psycopg` suffix when they paste a connection string.
PSYCOPG_SCHEME = "postgresql+psycopg://"
REWRITTEN_SCHEMES = ("postgres://", "postgresql://")

# Generating a world costs memory roughly linearly in the user count. A 512 MB
# instance measured ~120 MB peak RSS at 3000 users, so 1500 leaves a wide
# margin for the web server, the connection pool and the request itself.
HOSTED_MAX_DEMO_USERS = 1500
LOCAL_MAX_DEMO_USERS = 20_000

# A fresh deployment bootstraps itself with this much data.
BOOTSTRAP_SEED = 42
BOOTSTRAP_USERS = 1200


def normalize_database_url(url: str) -> str:
    """Force a URL onto the psycopg3 dialect, preserving everything else.

    Accepts every form a provider is likely to hand over:

        postgres://u:p@host/db                  -> postgresql+psycopg://...
        postgresql://u:p@host/db?sslmode=require-> postgresql+psycopg://...?sslmode=require
        postgresql+psycopg://u:p@host/db        -> unchanged

    The query string is untouched, so Neon's required `?sslmode=require`
    survives and is handled by psycopg itself.
    """
    stripped = url.strip()
    if stripped.startswith(PSYCOPG_SCHEME):
        return stripped
    for scheme in REWRITTEN_SCHEMES:
        if stripped.startswith(scheme):
            return PSYCOPG_SCHEME + stripped[len(scheme) :]
    # Anything else (sqlite, a psycopg2 URL someone set deliberately) is left
    # alone rather than mangled.
    return stripped


class Settings(BaseSettings):
    """Runtime configuration for the SignalStack API."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    DATABASE_URL: str = (
        "postgresql+psycopg://signalstack:signalstack@localhost:5433/signalstack"
    )
    CORS_ORIGINS: str = "http://localhost:5173"
    ENVIRONMENT: str = "local"

    #: Populate an empty database on startup so a fresh deploy is never blank.
    BOOTSTRAP_ON_EMPTY: bool = True

    @field_validator("DATABASE_URL")
    @classmethod
    def _normalize_url(cls, value: str) -> str:
        return normalize_database_url(value)

    @property
    def cors_origins_list(self) -> list[str]:
        """CORS_ORIGINS split into a list, ignoring blank entries.

        Accepts a comma-separated list of real origins, e.g.
        `https://signalstack.vercel.app,http://localhost:5173`.
        """
        return [
            origin.strip()
            for origin in self.CORS_ORIGINS.split(",")
            if origin.strip()
        ]

    @property
    def is_local(self) -> bool:
        return self.ENVIRONMENT.strip().lower() == "local"

    @property
    def max_demo_users(self) -> int:
        """Ceiling for POST /api/demo/reset.

        Hosted environments run on small instances, so the request's `users`
        is clamped rather than rejected — a demo button that errors is worse
        than one that quietly generates a smaller world and says so.
        """
        return LOCAL_MAX_DEMO_USERS if self.is_local else HOSTED_MAX_DEMO_USERS


settings = Settings()
