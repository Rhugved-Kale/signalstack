"""HTTP API layer.

Thin wrappers over `app.attribution.analytics` and the pipeline runners. The
router is mounted under `/api` by `app.main`.
"""

from app.api.routes import router

__all__ = ["router"]
