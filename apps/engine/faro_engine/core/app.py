"""Construcción de la aplicación FastAPI del motor."""

from __future__ import annotations

from fastapi import FastAPI

from faro_engine.core.config import Settings
from faro_engine.core.errors import install_error_handlers
from faro_engine.core.logging import RequestLoggingMiddleware
from faro_engine.core.routes import health
from faro_engine.core.security import SecurityMiddleware


def create_app(settings: Settings) -> FastAPI:
    """App con seguridad Host + Bearer en todas las rutas.

    Sin `/docs`, `/redoc` ni `/openapi.json` por HTTP (tampoco en `--dev`): el esquema
    se exporta con `python -m faro_engine.export_openapi`.
    """
    app = FastAPI(
        title="Faro Engine",
        version=settings.version,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        swagger_ui_oauth2_redirect_url=None,
    )
    app.state.settings = settings
    install_error_handlers(app)
    app.include_router(health.router)
    # add_middleware apila hacia fuera: el último añadido es el más externo.
    app.add_middleware(
        SecurityMiddleware,
        token=settings.token,
        expected_host=settings.expected_host,
    )
    app.add_middleware(RequestLoggingMiddleware)
    return app
