"""Auditoría local en `audit_log` (ADR 0010 §4, spec F1a §6, skill `llavero-y-cifrado`).

Dos orígenes, una tabla:

- **Núcleo** → stdin, una línea por evento (hasta 500 en su búfer mientras el motor no
  está listo):

      {"event":"audit","occurred_at":"2026-09-30T12:00:00.123Z","actor":"system",
       "action":"secret.used","secret_ref":"wp/<uuid>/token","run_id":"<uuid>",
       "result":"ok","details":{"operation":"checkSiteConnection","op":"get"}}

  Acciones `secret.*` y, desde F1b (spec §5.1), `agents.paused`, `agents.resumed`,
  `agent.grant_*` y `audit.dropped` (eventos que el búfer del núcleo tuvo que descartar;
  segunda revisión de seguridad de T5). `secret_ref`, `run_id` y `details` pueden ser `null` u
  omitirse. Un evento inválido se descarta con un aviso en el log que dice qué campo
  falló, nunca su contenido.
- **Motor** (`AuditLog.record`): acciones `site.*` de los casos de uso (F1a T9) y, desde
  F1b (spec §6), `autonomy.changed`, `approval.decided`, `approval.executed`,
  `llm.limit_changed` y `llm.preference_changed`.

Validación común: `actor` ∈ `user`, `agent`, `system`; `result` ∈ `ok`, `denied`,
`error`; `secret_ref` con la gramática del llavero; `run_id` UUID; `details` solo con
las claves `site_id`, `operation`, `provider`, `op`, `reason`, `error_code` (F1a) y
`agent_kind` (F1b) en cualquier acción; `count` solo en las acciones agregables del
núcleo (`CORE_ACTION_DETAIL_KEYS`: `secret.denied`, `agent.grant_denied`,
`agent.grant_released` y `audit.dropped`); y las de `ACTION_DETAIL_KEYS` solo en su acción
del motor (`level` en `autonomy.changed`; `approval_id` en `approval.*`; `decision` en
`approval.decided`), nunca en un evento del núcleo. Valores de texto de 1 a 64
caracteres `[A-Za-z0-9._:/-]` que no tengan forma de secreto (filtro de ADR 0013) y, para
esas claves, su forma exacta (`DETAIL_VALUE_PATTERNS`: `decision` ∈ `approve`, `reject`;
`level` de `0` a `3`; `approval_id` UUID; `count` entero positivo en decimal). Nunca se
guarda un valor de un secreto ni `last4`.

La fila guarda `occurred_at` normalizado a UTC con milisegundos (`…T12:00:00.123Z`),
`id` = UUID v7 nuevo y `details` como JSON compacto con las claves ordenadas.

**Fuera del hilo de stdin** (pendiente F1a §12.2-3, spec F1b T5): el hilo `faro-protocol`
solo reparte. Valida el evento del núcleo (rápido, sin base) y lo deja en la cola de
`AuditWriter`, cuyo hilo `faro-audit` hace las inserciones. Así una inserción lenta no
retrasa `secret_response`, `run_grant_response` ni `agents_control`. La cola guarda hasta
`AUDIT_QUEUE_MAX` eventos; si se llena, el evento se descarta con un aviso (sin
contenido). Al apagar, `close` inserta lo pendiente (con un plazo) antes de cerrar la base.
"""

from __future__ import annotations

import json
import queue
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Literal, Protocol

import structlog
from fastapi import Request

from faro_engine.core.db.connection import Connection, DatabaseError, DbUnavailableError
from faro_engine.core.db.database import Database
from faro_engine.core.ids import new_id
from faro_engine.core.redact import redact_values
from faro_engine.core.run_id import RUN_ID_PATTERN, current_run_id, is_valid_run_id
from faro_engine.core.secrets import is_valid_secret_ref

log = structlog.get_logger(__name__)

Actor = Literal["user", "agent", "system"]
Result = Literal["ok", "denied", "error"]

ACTORS: Final = frozenset({"user", "agent", "system"})
RESULTS: Final = frozenset({"ok", "denied", "error"})
# Acciones que puede enviar el núcleo (Bóveda y canal de secretos).
CORE_ACTIONS: Final = frozenset(
    {
        "secret.added",
        "secret.replaced",
        "secret.tested",
        "secret.used",
        "secret.denied",
        "secret.deleted",
        # Pausa global y concesiones por ejecución (spec F1b §5.1, ADR 0014).
        "agents.paused",
        "agents.resumed",
        "agent.grant_issued",
        "agent.grant_denied",
        "agent.grant_released",
        # Eventos descartados por el búfer lleno del núcleo (segunda revisión de T5).
        "audit.dropped",
    },
)
# Acciones que registra el propio motor (casos de uso de sitios y, desde F1b §6,
# autonomía, aprobaciones y ajustes de la capa de IA).
ENGINE_ACTIONS: Final = frozenset(
    {
        "site.connected",
        "site.reconnected",
        "site.revoked_detected",
        "site.removed",
        "autonomy.changed",
        "approval.decided",
        "approval.executed",
        "llm.limit_changed",
        "llm.preference_changed",
    },
)
# Claves de `details` que admite cualquier acción (núcleo y motor).
DETAIL_KEYS: Final = frozenset(
    {
        "site_id",
        "operation",
        "provider",
        "op",
        "reason",
        "error_code",
        # F1b §6.
        "agent_kind",
    },
)
# Claves que solo admite una acción concreta del motor (F1b §6); nunca llegan del núcleo.
ACTION_DETAIL_KEYS: Final[Mapping[str, frozenset[str]]] = {
    "autonomy.changed": frozenset({"level"}),
    "approval.decided": frozenset({"approval_id", "decision"}),
    "approval.executed": frozenset({"approval_id"}),
}
# Claves que solo admite una acción concreta del núcleo (revisiones de seguridad de T5):
# `count` en los rechazos que el núcleo agrega y en `audit.dropped`.
CORE_ACTION_DETAIL_KEYS: Final[Mapping[str, frozenset[str]]] = {
    "secret.denied": frozenset({"count"}),
    "agent.grant_denied": frozenset({"count"}),
    "agent.grant_released": frozenset({"count"}),
    "audit.dropped": frozenset({"count"}),
}
DETAIL_VALUE_PATTERN: Final = re.compile(r"[A-Za-z0-9._:/-]{1,64}")
# Forma exacta del valor de las claves que la tienen.
DETAIL_VALUE_PATTERNS: Final[Mapping[str, re.Pattern[str]]] = {
    "decision": re.compile(r"approve|reject"),
    "level": re.compile(r"[0-3]"),
    "approval_id": RUN_ID_PATTERN,
    # Entero positivo de 1 a 18 dígitos sin ceros a la izquierda: de 1 a
    # 999 999 999 999 999 999. El núcleo lo pone con valor ≥ 2 en un rechazo agregado y
    # ≥ 1 en `audit.dropped`.
    "count": re.compile(r"[1-9][0-9]{0,17}"),
}
# RFC 3339 en UTC con `Z` y de 0 a 9 decimales.
OCCURRED_AT_PATTERN: Final = re.compile(
    r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?Z",
)

_REQUIRED_CORE_KEYS: Final = frozenset({"event", "occurred_at", "actor", "action", "result"})
_CORE_KEYS: Final = _REQUIRED_CORE_KEYS | {"secret_ref", "run_id", "details"}

INSERT_SQL: Final = (
    "INSERT INTO audit_log (id, occurred_at, actor, action, secret_ref, run_id, result, details)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)


class InvalidAuditEventError(ValueError):
    """Evento de auditoría inválido. `field` dice qué campo falló; nunca su contenido."""

    def __init__(self, field_name: str) -> None:
        super().__init__(field_name)
        self.field = field_name


def format_timestamp(moment: datetime) -> str:
    """UTC ISO-8601 con milisegundos y `Z`."""
    moment = moment.astimezone(UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def parse_occurred_at(value: object) -> str:
    """Valida un instante RFC 3339 en UTC (`…Z`) y lo normaliza a milisegundos."""
    if not isinstance(value, str) or (match := OCCURRED_AT_PATTERN.fullmatch(value)) is None:
        raise InvalidAuditEventError("occurred_at")
    fraction = (match.group(2) or "").ljust(6, "0")[:6]
    try:
        moment = datetime.fromisoformat(f"{match.group(1)}.{fraction}+00:00")
    except ValueError as exc:
        raise InvalidAuditEventError("occurred_at") from exc
    return format_timestamp(moment)


def validate_details(details: object, action: str) -> dict[str, str]:
    """Claves comunes más las propias de `action` (`ACTION_DETAIL_KEYS` del motor y
    `CORE_ACTION_DETAIL_KEYS` del núcleo; las dos listas de acciones no se cruzan)."""
    if details is None:
        return {}
    if not isinstance(details, Mapping):
        raise InvalidAuditEventError("details")
    allowed = (
        DETAIL_KEYS
        | ACTION_DETAIL_KEYS.get(action, frozenset())
        | CORE_ACTION_DETAIL_KEYS.get(action, frozenset())
    )
    clean: dict[str, str] = {}
    for key, value in details.items():
        if key not in allowed:
            raise InvalidAuditEventError("details")
        exact = DETAIL_VALUE_PATTERNS.get(key)
        if (
            not isinstance(value, str)
            or DETAIL_VALUE_PATTERN.fullmatch(value) is None
            or (exact is not None and exact.fullmatch(value) is None)
            # Segunda defensa: nada con forma de secreto (p. ej. 43 base64url).
            or redact_values(value) != value
        ):
            raise InvalidAuditEventError("details")
        clean[key] = value
    return clean


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """Una fila de `audit_log` ya validada."""

    occurred_at: str
    actor: str
    action: str
    result: str
    secret_ref: str | None = None
    run_id: str | None = None
    details: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        *,
        actions: frozenset[str],
        occurred_at: object,
        actor: object,
        action: object,
        result: object,
        secret_ref: object = None,
        run_id: object = None,
        details: object = None,
    ) -> AuditEvent:
        """Valida cada campo; lanza `InvalidAuditEventError` con el nombre del que falla."""
        timestamp = parse_occurred_at(occurred_at)
        if not isinstance(actor, str) or actor not in ACTORS:
            raise InvalidAuditEventError("actor")
        if not isinstance(action, str) or action not in actions:
            raise InvalidAuditEventError("action")
        if not isinstance(result, str) or result not in RESULTS:
            raise InvalidAuditEventError("result")
        if secret_ref is not None and not is_valid_secret_ref(secret_ref):
            raise InvalidAuditEventError("secret_ref")
        if run_id is not None and not is_valid_run_id(run_id):
            raise InvalidAuditEventError("run_id")
        return cls(
            occurred_at=timestamp,
            actor=actor,
            action=action,
            result=result,
            secret_ref=secret_ref if isinstance(secret_ref, str) else None,
            run_id=run_id if isinstance(run_id, str) else None,
            details=validate_details(details, action),
        )

    def row(self, row_id: str) -> tuple[str, str, str, str, str | None, str | None, str, str]:
        details = json.dumps(dict(self.details), sort_keys=True, separators=(",", ":"))
        return (
            row_id,
            self.occurred_at,
            self.actor,
            self.action,
            self.secret_ref,
            self.run_id,
            self.result,
            details,
        )


def parse_core_event(data: Mapping[str, Any]) -> AuditEvent:
    """Evento `audit` recibido por stdin (ya decodificado)."""
    keys = set(data)
    if not keys >= _REQUIRED_CORE_KEYS or not keys <= _CORE_KEYS:
        raise InvalidAuditEventError("keys")
    return AuditEvent.build(
        actions=CORE_ACTIONS,
        occurred_at=data["occurred_at"],
        actor=data["actor"],
        action=data["action"],
        result=data["result"],
        secret_ref=data.get("secret_ref"),
        run_id=data.get("run_id"),
        details=data.get("details"),
    )


def insert_event(conn: Connection, event: AuditEvent) -> str:
    """Inserta la fila (autocommit: una sola sentencia). Devuelve su `id`."""
    row_id = new_id()
    conn.execute(INSERT_SQL, event.row(row_id))
    return row_id


class AuditLog:
    """Escribe en `audit_log` de la base del perfil. Nunca lanza por un fallo de la base:
    lo registra (sin contenido) y devuelve `False`."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def record_core_event(self, data: dict[str, Any]) -> bool:
        """Desde el lector de stdin (hilo propio). Vacía `data` al terminar."""
        try:
            event = parse_core_event(data)
        except InvalidAuditEventError as exc:
            log.warning("audit.invalid_event", source="core", field=exc.field)
            return False
        finally:
            data.clear()
        return self._insert_sync(event)

    async def record(
        self,
        *,
        action: str,
        result: Result,
        actor: Actor = "user",
        secret_ref: str | None = None,
        details: Mapping[str, str] | None = None,
        occurred_at: datetime | None = None,
    ) -> bool:
        """Evento del motor (`ENGINE_ACTIONS`). El `run_id` es el de la operación en curso.

        Un evento mal formado es un error de programación: lanza `InvalidAuditEventError`.
        """
        moment = occurred_at if occurred_at is not None else datetime.now(UTC)
        event = AuditEvent.build(
            actions=ENGINE_ACTIONS,
            occurred_at=format_timestamp(moment),
            actor=actor,
            action=action,
            result=result,
            secret_ref=secret_ref,
            run_id=current_run_id.get(),
            details=details,
        )
        try:
            await self._database.run(lambda conn: insert_event(conn, event))
        except (DbUnavailableError, DatabaseError) as exc:
            self._log_failure(event, exc)
            return False
        return True

    def insert_sync(self, event: AuditEvent) -> bool:
        """Inserta un evento ya validado (desde el hilo `faro-audit`)."""
        return self._insert_sync(event)

    def _insert_sync(self, event: AuditEvent) -> bool:
        try:
            self._database.run_sync(lambda conn: insert_event(conn, event))
        except (DbUnavailableError, DatabaseError) as exc:
            self._log_failure(event, exc)
            return False
        return True

    @staticmethod
    def _log_failure(event: AuditEvent, exc: Exception) -> None:
        if isinstance(exc, DbUnavailableError):
            log.warning("audit.dropped", action=event.action, error_code=exc.code)
        else:
            log.error("audit.insert_failed", action=event.action, error_type=type(exc).__name__)


class CoreAuditSink(Protocol):
    """Quien recibe los eventos `audit` del núcleo desde el hilo de stdin."""

    def record_core_event(self, data: dict[str, Any]) -> bool: ...


AUDIT_QUEUE_MAX: Final = 1000
AUDIT_CLOSE_SECONDS: Final = 5.0


class AuditWriter:
    """Hilo `faro-audit`: inserta los eventos del núcleo fuera del hilo de stdin."""

    def __init__(self, audit: AuditLog, *, max_pending: int = AUDIT_QUEUE_MAX) -> None:
        self._audit = audit
        self._queue: queue.Queue[AuditEvent | None] = queue.Queue(maxsize=max_pending)
        self._thread = threading.Thread(target=self._run, name="faro-audit", daemon=True)
        self._closed = False
        self._lock = threading.Lock()

    def start(self) -> None:
        self._thread.start()

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    def record_core_event(self, data: dict[str, Any]) -> bool:
        """Desde el hilo de stdin: valida y encola, sin tocar la base. Vacía `data`."""
        try:
            event = parse_core_event(data)
        except InvalidAuditEventError as exc:
            log.warning("audit.invalid_event", source="core", field=exc.field)
            return False
        finally:
            data.clear()
        with self._lock:
            if self._closed:
                log.warning("audit.dropped", action=event.action, reason="closed")
                return False
            try:
                self._queue.put_nowait(event)
            except queue.Full:
                log.warning("audit.dropped", action=event.action, reason="queue_full")
                return False
        return True

    def close(self, timeout: float = AUDIT_CLOSE_SECONDS) -> bool:
        """Deja de aceptar eventos e inserta lo pendiente. `False` si no terminó a tiempo."""
        with self._lock:
            if self._closed:
                return not self._thread.is_alive()
            self._closed = True
        if not self._thread.is_alive():
            return True
        try:
            self._queue.put(None, timeout=timeout)
        except queue.Full:
            log.warning("audit.close_timeout", pending=self._queue.qsize())
            return False
        self._thread.join(timeout)
        if self._thread.is_alive():
            log.warning("audit.close_timeout", pending=self._queue.qsize())
            return False
        return True

    def _run(self) -> None:
        while (event := self._queue.get()) is not None:
            self._audit.insert_sync(event)


def get_audit(request: Request) -> AuditLog:
    """Dependencia FastAPI: el registro de auditoría del motor."""
    audit: AuditLog = request.app.state.audit
    return audit
