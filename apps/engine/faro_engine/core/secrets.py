"""Cliente del canal de secretos con el núcleo (ADR 0010 §2, skill `llavero-y-cifrado`).

El motor nunca lee el llavero. Cuando una operación necesita un secreto escribe por stdout

    {"event":"secret_request","id":"<uuid>","run_id":"<uuid>","op":"get","ref":"wp/<uuid>/token"}

(`op` ∈ `get`, `create`, `set`, `delete`; `create` y `set` llevan además `"value"`), y el
núcleo responde por stdin con el mismo `id`:

    {"event":"secret_response","id":"<uuid>","value":"<texto>"}   (get)
    {"event":"secret_response","id":"<uuid>","ok":true}           (create, set, delete)
    {"event":"secret_response","id":"<uuid>","error":"vault.secret_not_allowed"}

Reglas del lado del motor:
- Solo se pide dentro de una operación en curso: el `run_id` sale de la cabecera
  `X-Faro-Run-Id` (`run_id.current_run_id`). Sin él, la solicitud se rechaza aquí con
  `vault.secret_not_allowed` y no sale nada por stdout.
- La referencia se valida con la gramática del llavero (`vault.invalid_ref`) y `db/*`
  nunca se pide (`vault.secret_not_allowed`): la llave de la base solo llega al arrancar.
- Espera máxima `SECRET_TIMEOUT_SECONDS` (10 s) → `vault.secret_timeout`. Una respuesta
  que llega tarde, con `id` desconocido o repetida se ignora (su valor se suelta).
- Un código de error que el núcleo no debería enviar, o una respuesta que no encaja con
  la operación, cuenta como `vault.secret_not_allowed` (se falla cerrado).
- Sin canal (modo desarrollo externo, ADR 0010 §5) → `engine.secrets_unavailable`.
  Cerrado el canal (EOF o `shutdown` en stdin, stdout roto) → `vault.secret_timeout`.

El valor solo vive en memoria durante la operación: `get` devuelve un `SecretValue`
(`bytearray` que se sobrescribe al salir de `with`), y la línea de `create`/`set` se arma
en un `bytearray` que se sobrescribe tras escribirla. Copias inevitables (ver también
`protocol.py`): el `str` que crea `json.loads` al leer la respuesta y el `bytes` de la
lectura de stdin. El motor no guarda secretos entre operaciones ni los registra: los logs
solo llevan `op`, `secret_ref` y el código de resultado.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Final

import anyio.to_thread
import structlog
from fastapi import Request

from faro_engine.core.config import SECRET_TIMEOUT_SECONDS
from faro_engine.core.errors import (
    ENGINE_SECRETS_UNAVAILABLE,
    VAULT_ALREADY_EXISTS,
    VAULT_INVALID_INPUT,
    VAULT_INVALID_REF,
    VAULT_KEYRING_UNAVAILABLE,
    VAULT_NOT_FOUND,
    VAULT_SECRET_NOT_ALLOWED,
    VAULT_SECRET_TIMEOUT,
    FaroError,
)
from faro_engine.core.ids import new_id
from faro_engine.core.operations import SecretAccess
from faro_engine.core.protocol import EVENT_SECRET_REQUEST, ProtocolWriter, wipe_line
from faro_engine.core.run_id import current_run_id, is_valid_run_id

log = structlog.get_logger(__name__)

_UUID: Final = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
# Gramática del llavero (skill `llavero-y-cifrado`). El núcleo aplica la misma.
SECRET_REF_PATTERN: Final = re.compile(
    rf"llm/(?:anthropic|openai|gemini)/[a-z0-9_-]{{1,32}}|wp/{_UUID}/token"
    rf"|oauth/google/[0-9]{{1,64}}|db/{_UUID}/key"
)
_ID_PATTERN: Final = re.compile(_UUID)

# Tamaño máximo de un valor (entrada del llavero ≈ 2,5 KB; `wp/*/token` ≈ 120 bytes).
MAX_SECRET_BYTES: Final = 4096

# Errores que el núcleo puede devolver (spec F1a §5.1) y su estado HTTP si llegan a la ruta.
CORE_ERROR_STATUS: Final[Mapping[str, int]] = {
    VAULT_INVALID_REF: 500,
    VAULT_SECRET_NOT_ALLOWED: 500,
    VAULT_NOT_FOUND: 404,
    VAULT_ALREADY_EXISTS: 409,
    VAULT_INVALID_INPUT: 422,
    VAULT_KEYRING_UNAVAILABLE: 503,
}
# Errores que decide el motor.
ENGINE_ERROR_STATUS: Final[Mapping[str, int]] = {
    VAULT_SECRET_TIMEOUT: 503,
    ENGINE_SECRETS_UNAVAILABLE: 503,
}

_OPS_WITH_VALUE: Final = frozenset({"create", "set"})


class SecretError(FaroError):
    """Fallo al pedir un secreto. Nunca lleva valores ni detalles."""

    def __init__(self, code: str) -> None:
        status = CORE_ERROR_STATUS.get(code) or ENGINE_ERROR_STATUS[code]
        base = FaroError.of(code, status)
        super().__init__(base.code, base.message, base.status)


def is_valid_secret_ref(value: object) -> bool:
    return isinstance(value, str) and SECRET_REF_PATTERN.fullmatch(value) is not None


class SecretValue:
    """Valor de un secreto en memoria durante la operación. Se sobrescribe al salir de
    `with` (o con `wipe()`); `repr` nunca lo muestra."""

    __slots__ = ("_buffer",)

    def __init__(self, buffer: bytearray) -> None:
        self._buffer: bytearray | None = buffer

    @property
    def buffer(self) -> bytearray:
        if self._buffer is None:
            raise ValueError("el secreto ya se borró")
        return self._buffer

    def wipe(self) -> None:
        if self._buffer is not None:
            wipe_line(self._buffer)
            self._buffer = None

    def __enter__(self) -> SecretValue:
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _tb: TracebackType | None,
    ) -> None:
        self.wipe()

    def __repr__(self) -> str:
        return "SecretValue([oculto])"


@dataclass(slots=True)
class _Outcome:
    value: bytearray | None = field(default=None, repr=False)
    error: str | None = None

    def wipe(self) -> None:
        if self.value is not None:
            wipe_line(self.value)
            self.value = None


@dataclass(slots=True)
class _Pending:
    op: SecretAccess
    loop: asyncio.AbstractEventLoop
    future: asyncio.Future[_Outcome]


def _append_json_string(line: bytearray, value: bytes | bytearray) -> None:
    """Añade `value` a `line` como texto JSON, sin pasar por `str`.

    Solo ASCII imprimible (el valor de `wp/*/token` es JSON compacto en ASCII); se
    escapan la comilla y la barra invertida. Cualquier otro byte → `vault.invalid_input`.
    """
    line += b'"'
    for byte in value:
        if byte < 0x20 or byte > 0x7E:
            raise SecretError(VAULT_INVALID_INPUT)
        if byte in (0x22, 0x5C):
            line.append(0x5C)
        line.append(byte)
    line += b'"'


def build_request_line(
    request_id: str, run_id: str, op: SecretAccess, ref: str, value: bytes | bytearray | None
) -> bytearray:
    """`{"event":"secret_request","id":…,"run_id":…,"op":…,"ref":…[,"value":…]}` + LF.

    `request_id`, `run_id`, `op` y `ref` ya están validados (sin comillas ni escapes).
    """
    line = bytearray(
        f'{{"event":"{EVENT_SECRET_REQUEST}","id":"{request_id}","run_id":"{run_id}",'
        f'"op":"{op}","ref":"{ref}"',
        "ascii",
    )
    try:
        if value is not None:
            line += b',"value":'
            _append_json_string(line, value)
        line += b"}\n"
    except BaseException:
        wipe_line(line)
        raise
    return line


def _outcome_from_response(op: SecretAccess, data: Mapping[str, Any]) -> _Outcome:
    """Traduce la respuesta del núcleo. Nunca registra ni conserva el `str` del valor."""
    keys = set(data)
    if keys == {"event", "id", "error"}:
        code = data["error"]
        if isinstance(code, str) and code in CORE_ERROR_STATUS:
            return _Outcome(error=code)
        log.warning("secrets.bad_response", op=op, reason="unknown_error")
        return _Outcome(error=VAULT_SECRET_NOT_ALLOWED)
    if op == "get" and keys == {"event", "id", "value"}:
        raw = data["value"]
        if isinstance(raw, str) and 0 < len(raw) <= MAX_SECRET_BYTES:
            try:
                return _Outcome(value=bytearray(raw, "utf-8"))
            except UnicodeEncodeError:  # sustitutos sueltos
                pass
    elif op != "get" and keys == {"event", "id", "ok"} and data["ok"] is True:
        return _Outcome()
    log.warning("secrets.bad_response", op=op, reason="shape")
    return _Outcome(error=VAULT_SECRET_NOT_ALLOWED)


def _deliver(future: asyncio.Future[_Outcome], outcome: _Outcome) -> None:
    if future.done():
        outcome.wipe()
    else:
        future.set_result(outcome)


def _discard(future: asyncio.Future[_Outcome]) -> None:
    """La operación ya no espera: cancela el futuro o sobrescribe lo que haya llegado."""
    if not future.done():
        future.cancel()
    elif not future.cancelled() and future.exception() is None:
        future.result().wipe()


class SecretBroker:
    """Solicitudes de secretos al núcleo, una por `id`, con su futuro y su tiempo máximo."""

    def __init__(
        self,
        writer: ProtocolWriter | None,
        *,
        timeout: float = SECRET_TIMEOUT_SECONDS,
        unavailable_code: str | None = None,
    ) -> None:
        if writer is None and unavailable_code is None:
            unavailable_code = ENGINE_SECRETS_UNAVAILABLE
        self._writer = writer
        self._timeout = timeout
        self._unavailable_code = unavailable_code
        self._lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}

    @classmethod
    def unavailable(cls, code: str = ENGINE_SECRETS_UNAVAILABLE) -> SecretBroker:
        """Sin canal: toda solicitud falla con `code` (modo desarrollo externo)."""
        return cls(None, unavailable_code=code)

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    # --- API para las operaciones -----------------------------------------------------

    async def get(self, ref: str) -> SecretValue:
        """Lee el secreto `ref`. Úsalo con `with` para que se sobrescriba al terminar."""
        outcome = await self._request("get", ref, None)
        if outcome.value is None:  # pragma: no cover - _outcome_from_response lo garantiza
            raise SecretError(VAULT_SECRET_NOT_ALLOWED)
        value = SecretValue(outcome.value)
        outcome.value = None
        return value

    async def create(self, ref: str, value: bytes | bytearray) -> None:
        """Crea `ref` (el núcleo falla con `vault.already_exists` si existe).

        No sobrescribe `value`: es de quien llama.
        """
        await self._request("create", ref, value)

    async def set(self, ref: str, value: bytes | bytearray) -> None:
        """Crea o reemplaza `ref`. No sobrescribe `value`: es de quien llama."""
        await self._request("set", ref, value)

    async def delete(self, ref: str) -> None:
        """Borra `ref` (idempotente en el llavero)."""
        await self._request("delete", ref, None)

    # --- Entrada desde el lector de stdin (otro hilo) ---------------------------------

    def handle_response(self, data: dict[str, Any]) -> None:
        """Resuelve la solicitud pendiente con ese `id`. Vacía `data` al terminar."""
        try:
            request_id = data.get("id")
            if not isinstance(request_id, str) or _ID_PATTERN.fullmatch(request_id) is None:
                log.warning("secrets.response_ignored", reason="invalid_id")
                return
            with self._lock:
                pending = self._pending.pop(request_id, None)
            if pending is None:
                log.warning("secrets.response_ignored", reason="unknown_id")
                return
            outcome = _outcome_from_response(pending.op, data)
            try:
                pending.loop.call_soon_threadsafe(_deliver, pending.future, outcome)
            except RuntimeError:  # el bucle ya se cerró (apagado)
                outcome.wipe()
        finally:
            # El `str` del valor no se puede sobrescribir; se suelta ya.
            data.clear()

    def close(self, code: str = VAULT_SECRET_TIMEOUT) -> None:
        """Canal cerrado (EOF, `shutdown`): falla lo pendiente y lo que se pida después."""
        with self._lock:
            if self._unavailable_code is None:
                self._unavailable_code = code
            pending = list(self._pending.values())
            self._pending.clear()
        for item in pending:
            # RuntimeError: el bucle ya se cerró durante el apagado.
            with contextlib.suppress(RuntimeError):
                item.loop.call_soon_threadsafe(_deliver, item.future, _Outcome(error=code))

    # --- Interno ----------------------------------------------------------------------

    def _check(self, op: SecretAccess, ref: str, value: bytes | bytearray | None) -> str:
        if self._unavailable_code is not None:
            raise SecretError(self._unavailable_code)
        run_id = current_run_id.get()
        if run_id is None or not is_valid_run_id(run_id):
            log.warning("secrets.rejected", op=op, reason="no_run")
            raise SecretError(VAULT_SECRET_NOT_ALLOWED)
        if not is_valid_secret_ref(ref):
            log.warning("secrets.rejected", op=op, reason="invalid_ref")
            raise SecretError(VAULT_INVALID_REF)
        if ref.startswith("db/"):
            log.warning("secrets.rejected", op=op, secret_ref=ref, reason="db_ref")
            raise SecretError(VAULT_SECRET_NOT_ALLOWED)
        if (value is not None) != (op in _OPS_WITH_VALUE) or (
            value is not None and not 0 < len(value) <= MAX_SECRET_BYTES
        ):
            log.warning("secrets.rejected", op=op, secret_ref=ref, reason="invalid_value")
            raise SecretError(VAULT_INVALID_INPUT)
        return run_id

    async def _request(
        self, op: SecretAccess, ref: str, value: bytes | bytearray | None
    ) -> _Outcome:
        run_id = self._check(op, ref, value)
        writer = self._writer
        if writer is None:  # pragma: no cover - sin escritor, _check ya falló
            raise SecretError(ENGINE_SECRETS_UNAVAILABLE)
        request_id = new_id()
        line = build_request_line(request_id, run_id, op, ref, value)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[_Outcome] = loop.create_future()
        with self._lock:
            self._pending[request_id] = _Pending(op, loop, future)
        log.info("secrets.request", op=op, secret_ref=ref, secret_request_id=request_id)
        outcome: _Outcome | None = None
        try:
            try:
                await anyio.to_thread.run_sync(writer.write_line, line)
            except OSError:
                log.warning("secrets.channel_closed", op=op, secret_ref=ref)
                self.close()
                raise SecretError(VAULT_SECRET_TIMEOUT) from None
            finally:
                wipe_line(line)
            try:
                outcome = await asyncio.wait_for(future, self._timeout)
            except TimeoutError:
                log.warning("secrets.timeout", op=op, secret_ref=ref)
                raise SecretError(VAULT_SECRET_TIMEOUT) from None
        finally:
            with self._lock:
                self._pending.pop(request_id, None)
            if outcome is None:
                _discard(future)
        if outcome.error is not None:
            log.info("secrets.response", op=op, secret_ref=ref, error_code=outcome.error)
            raise SecretError(outcome.error)
        log.info("secrets.response", op=op, secret_ref=ref, result="ok")
        return outcome


def get_secrets(request: Request) -> SecretBroker:
    """Dependencia FastAPI: el cliente del canal de secretos del motor."""
    broker: SecretBroker = request.app.state.secrets
    return broker
