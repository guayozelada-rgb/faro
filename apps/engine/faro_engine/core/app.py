"""Construcción de la aplicación FastAPI del motor."""

from __future__ import annotations

from fastapi import FastAPI

from faro_engine.core.audit import AuditLog
from faro_engine.core.config import Settings
from faro_engine.core.db.database import Database
from faro_engine.core.errors import DB_UNAVAILABLE, install_error_handlers
from faro_engine.core.logging import RequestLoggingMiddleware
from faro_engine.core.operations import validate_app_operations
from faro_engine.core.routes import health
from faro_engine.core.run_id import RunIdMiddleware
from faro_engine.core.secrets import SecretBroker
from faro_engine.core.security import SecurityMiddleware


def create_app(
    settings: Settings,
    database: Database | None = None,
    *,
    secrets: SecretBroker | None = None,
    audit: AuditLog | None = None,
) -> FastAPI:
    """App con seguridad Host + Bearer en todas las rutas.

    `database` es la base del perfil ya abierta (o no disponible con su código). Sin ella
    (exportar el OpenAPI, pruebas) la base queda no disponible con `db.unavailable`.
    `secrets` es el cliente del canal de secretos; sin él (modo externo, pruebas) toda
    solicitud falla con `engine.secrets_unavailable`. `audit` escribe en `audit_log` de
    `database` (se crea si no se pasa).

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
    app.state.database = database if database is not None else Database.unavailable(DB_UNAVAILABLE)
    app.state.secrets = secrets if secrets is not None else SecretBroker.unavailable()
    app.state.audit = audit if audit is not None else AuditLog(app.state.database)
    install_error_handlers(app)
    app.include_router(health.router)
    # Toda operación declara timeout y secretos (ADR 0010 §3); si no, el motor no arranca.
    validate_app_operations(app)
    # add_middleware apila hacia fuera: el último añadido es el más externo. Orden de
    # entrada: logs → Host y Bearer → `X-Faro-Run-Id` → rutas.
    app.add_middleware(RunIdMiddleware)
    app.add_middleware(
        SecurityMiddleware,
        token=settings.token,
        expected_host=settings.expected_host,
    )
    app.add_middleware(RequestLoggingMiddleware)
    return app
