"""Construcción de la aplicación FastAPI del motor."""

from __future__ import annotations

from fastapi import FastAPI

from faro_engine.agents.registry import agent_grants_table
from faro_engine.core.audit import AuditLog
from faro_engine.core.config import Settings
from faro_engine.core.db.database import Database
from faro_engine.core.errors import DB_UNAVAILABLE, install_error_handlers
from faro_engine.core.jobs.activity import ActivityEmitter
from faro_engine.core.jobs.control import AgentsControlState
from faro_engine.core.jobs.grants import RunGrantClient
from faro_engine.core.jobs.runner import AgentCatalog
from faro_engine.core.jobs.runtime import JobSystem
from faro_engine.core.logging import RequestLoggingMiddleware
from faro_engine.core.operations import validate_app_operations
from faro_engine.core.routes import agents as agent_routes
from faro_engine.core.routes import health, schedules, sites
from faro_engine.core.routes import llm as llm_routes
from faro_engine.core.run_id import RunIdMiddleware
from faro_engine.core.secrets import SecretBroker
from faro_engine.core.security import SecurityMiddleware
from faro_engine.llm.catalog import default_catalog
from faro_engine.llm.client import LlmClient
from faro_engine.llm.fake import dev_fake_llm, fake_catalog
from faro_engine.llm.service import LazyLiteLlmClient, LlmService
from faro_engine.net.client import NetSettings
from faro_engine.net.urls import NetPolicy


def default_net_settings(settings: Settings) -> NetSettings:
    """Red saliente real. En modo local, el puerto del propio motor sigue prohibido."""
    policy = NetPolicy(allow_local=settings.allow_local_sites, engine_port=settings.port)
    return NetSettings(policy=policy, user_agent=f"Faro/{settings.version}")


def default_llm_service(
    settings: Settings,
    database: Database,
    control: AgentsControlState,
    secrets: SecretBroker,
) -> LlmService:
    """Capa de IA real o, con `--fake-llm` (solo desarrollo), `FakeLLM` con su catálogo.

    El catálogo `models.json` se valida siempre: si no cumple, el motor no arranca. LiteLLM
    no se importa aquí, sino en la primera llamada (`LazyLiteLlmClient`).
    """
    catalog = default_catalog()
    client: LlmClient = LazyLiteLlmClient()
    if settings.fake_llm:
        client = dev_fake_llm()
        catalog = fake_catalog()
    return LlmService(
        database=database, control=control, secrets=secrets, client=client, catalog=catalog
    )


def default_agent_catalog() -> AgentCatalog:
    """Agentes del producto. Vacío en T7: el registro (`agents/registry.py`) llega en T8/T9.
    Los agentes de prueba se registran solo en las pruebas."""
    return AgentCatalog()


def create_app(
    settings: Settings,
    database: Database | None = None,
    *,
    secrets: SecretBroker | None = None,
    audit: AuditLog | None = None,
    net: NetSettings | None = None,
    control: AgentsControlState | None = None,
    grants: RunGrantClient | None = None,
    activity: ActivityEmitter | None = None,
    llm: LlmService | None = None,
    agents: AgentCatalog | None = None,
    jobs: JobSystem | None = None,
) -> FastAPI:
    """App con seguridad Host + Bearer en todas las rutas.

    `database` es la base del perfil ya abierta (o no disponible con su código). Sin ella
    (exportar el OpenAPI, pruebas) la base queda no disponible con `db.unavailable`.
    `secrets` es el cliente del canal de secretos; sin él (modo externo, pruebas) toda
    solicitud falla con `engine.secrets_unavailable`. `audit` escribe en `audit_log` de
    `database` (se crea si no se pasa). `net` es la red saliente (ADR 0012); por defecto,
    la real con la política de `settings` (sitios locales solo con `allow_local_sites`).
    `control`, `grants` y `activity` son la pausa global, el cliente de concesiones por
    ejecución y el emisor de actividad de los agentes (ADR 0014); sin ellos (pruebas), los
    agentes quedan en pausa, toda concesión se deniega y no se emite actividad. `llm` es la
    capa de IA (spec F1b §4.1); por defecto, `default_llm_service` (valida `models.json`:
    si no cumple, el motor no arranca). `agents` son los agentes que el motor puede ejecutar
    (`default_agent_catalog`) y `jobs` la cola, el trabajador y el programador
    (`core/jobs/runtime.py`), construidos con **el mismo** `LlmService` de `app.state.llm`
    (un único `DailyLimiter`, condición T6-C2); `__main__` los arranca y los para
    alrededor de `server.serve()`.

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
    app.state.net = net if net is not None else default_net_settings(settings)
    app.state.agents_control = control if control is not None else AgentsControlState()
    app.state.run_grants = grants if grants is not None else RunGrantClient.unavailable()
    app.state.activity = activity if activity is not None else ActivityEmitter(None)
    app.state.llm = (
        llm
        if llm is not None
        else default_llm_service(
            settings, app.state.database, app.state.agents_control, app.state.secrets
        )
    )
    app.state.jobs = (
        jobs
        if jobs is not None
        else JobSystem(
            database=app.state.database,
            control=app.state.agents_control,
            grants=app.state.run_grants,
            activity=app.state.activity,
            llm=app.state.llm,
            agents=agents if agents is not None else default_agent_catalog(),
        )
    )
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(sites.router)
    app.include_router(llm_routes.router)
    app.include_router(agent_routes.router)
    app.include_router(schedules.router)
    # Toda operación declara timeout y secretos (ADR 0010 §3); si no, el motor no arranca.
    validate_app_operations(app)
    # La tabla de concesiones de los agentes cumple ADR 0014 §1; si no, tampoco arranca.
    agent_grants_table()
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
