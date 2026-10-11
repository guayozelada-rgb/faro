"""Checkpointer propio de LangGraph sobre la base cifrada (ADR 0015 §2, spec F1b §4.2).

No se usa `langgraph-checkpoint-sqlite` (espera `sqlite3`, crea tablas fuera de nuestras
migraciones y abre otra conexión): `FaroCheckpointSaver` implementa los métodos
**asíncronos** de `BaseCheckpointSaver` (`aget_tuple`, `alist`, `aput`, `aput_writes`,
`adelete_thread`, `aprune`) sobre `Database.run` (una conexión, un candado) y las tablas
`agent_checkpoints` y `agent_checkpoint_writes` de la migración 0002
(`core/store/checkpoints.py`). Los síncronos lanzan `NotImplementedError`: el motor solo usa
`ainvoke`. `thread_id` = `agent_runs.id`. El checkpoint se guarda entero (con
`channel_values`) en una fila.

**Serializador solo JSON, sin `pickle` ni importación dinámica** (`JsonOnlySerializer`):

- escribe siempre `type = "json"` y, al leer, rechaza cualquier otro `type` y cualquier
  `BLOB` que no sea JSON UTF-8 válido (un `pickle` incluido) con
  `CheckpointUnreadableError` → la tarea falla con `agent.state_unreadable`; no se intenta
  reparar;
- solo admite tipos JSON (`None`, `bool`, `int`, `float` finito, `str`, listas, tuplas y
  mapas con claves de texto) y una **lista cerrada** de tipos internos de LangGraph 1.2.13
  que aparecen en checkpoints y escrituras pendientes (verificado en T8):
  `langgraph.types.Interrupt` (la escritura `__interrupt__` de `interrupt()`) y las
  excepciones de la escritura `__error__`, de las que solo se guarda el **nombre de la
  clase** (nunca el mensaje: puede llevar contenido) y que al leer vuelven como
  `CheckpointedError`. Cualquier otro tipo → `TypeError` al escribir (falla cerrado);
- la marca `__faro__` es reservada: un mapa del estado no puede usarla como clave.

`with_allowlist` devuelve el propio guardador: la lista de `msgpack` de LangGraph no aplica
(no hay `msgpack`).
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import AsyncIterator, Callable, Collection, Sequence
from datetime import UTC, datetime
from typing import Any, Final

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_serializable_checkpoint_metadata,
)
from langgraph.types import Interrupt

from faro_engine.core.db.connection import Connection
from faro_engine.core.db.database import Database
from faro_engine.core.store import checkpoints as store
from faro_engine.core.store.common import atomic, format_utc

JSON_TYPE: Final = store.CHECKPOINT_TYPE
RESERVED_KEY: Final = "__faro__"
INTERRUPT_TAG: Final = "interrupt"
ERROR_TAG: Final = "error"
ERROR_NAME_PATTERN: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
CHECKPOINT_KEYS: Final = frozenset({"v", "id", "ts", "channel_values", "channel_versions"})


class CheckpointUnreadableError(ValueError):
    """Un checkpoint o una escritura no se puede leer o validar (sin contenido)."""


class CheckpointedError(Exception):
    """Error guardado en un checkpoint: solo el nombre de su clase."""


def _encode(value: Any) -> Any:
    if value is None or isinstance(value, bool | str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError("número no finito en el estado")
        return value
    if isinstance(value, list | tuple):
        return [_encode(item) for item in value]
    if isinstance(value, dict):
        encoded: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or key == RESERVED_KEY:
                raise TypeError("clave de mapa no admitida en el estado")
            encoded[key] = _encode(item)
        return encoded
    if isinstance(value, Interrupt):
        if value.response_schema is not None:
            raise TypeError("interrupción con esquema de respuesta no admitida")
        return {RESERVED_KEY: INTERRUPT_TAG, "value": _encode(value.value), "id": value.id}
    if isinstance(value, BaseException):
        name = type(value).__name__
        if ERROR_NAME_PATTERN.fullmatch(name) is None:
            name = "Error"
        return {RESERVED_KEY: ERROR_TAG, "name": name}
    raise TypeError(f"tipo no admitido en un checkpoint: {type(value).__name__}")


def _decode_object(data: dict[str, Any]) -> Any:
    tag = data.get(RESERVED_KEY)
    if tag is None:
        return data
    if tag == INTERRUPT_TAG and set(data) == {RESERVED_KEY, "value", "id"}:
        interrupt_id = data["id"]
        if not isinstance(interrupt_id, str):
            raise CheckpointUnreadableError("interrupción inválida")
        return Interrupt(value=data["value"], id=interrupt_id)
    if tag == ERROR_TAG and set(data) == {RESERVED_KEY, "name"}:
        name = data["name"]
        if not isinstance(name, str) or ERROR_NAME_PATTERN.fullmatch(name) is None:
            raise CheckpointUnreadableError("error guardado inválido")
        return CheckpointedError(name)
    raise CheckpointUnreadableError("tipo desconocido en un checkpoint")


def _reject_constant(_name: str) -> Any:
    raise CheckpointUnreadableError("número no finito en un checkpoint")


class JsonOnlySerializer:
    """Protocolo de serialización de LangGraph (`dumps_typed`/`loads_typed`) solo con JSON."""

    def dumps_typed(self, obj: Any) -> tuple[str, bytes]:
        text = json.dumps(_encode(obj), ensure_ascii=False, separators=(",", ":"))
        return JSON_TYPE, text.encode("utf-8")

    def loads_typed(self, data: tuple[str, bytes]) -> Any:
        kind, payload = data
        if kind != JSON_TYPE:
            raise CheckpointUnreadableError("checkpoint que no es JSON")
        try:
            text = bytes(payload).decode("utf-8")
            return json.loads(text, object_hook=_decode_object, parse_constant=_reject_constant)
        except CheckpointUnreadableError:
            raise
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise CheckpointUnreadableError("checkpoint ilegible") from None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _ids(config: RunnableConfig) -> tuple[str, str, str | None]:
    configurable = config.get("configurable") or {}
    thread_id = configurable.get("thread_id")
    if not isinstance(thread_id, str):
        raise TypeError("falta thread_id")
    namespace = configurable.get("checkpoint_ns", "")
    checkpoint_id = configurable.get("checkpoint_id")
    return thread_id, str(namespace), checkpoint_id if isinstance(checkpoint_id, str) else None


def _config(thread_id: str, namespace: str, checkpoint_id: str) -> RunnableConfig:
    return {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": namespace,
            "checkpoint_id": checkpoint_id,
        }
    }


class FaroCheckpointSaver(BaseCheckpointSaver[int]):
    """Checkpoints de LangGraph en la base cifrada, solo JSON (ver la cabecera)."""

    def __init__(self, database: Database, *, clock: Callable[[], datetime] = _utc_now) -> None:
        super().__init__(serde=JsonOnlySerializer())
        self._database = database
        self._clock = clock

    def with_allowlist(self, extra_allowlist: Collection[tuple[str, ...]]) -> FaroCheckpointSaver:
        del extra_allowlist  # sin msgpack: el serializador ya es una lista cerrada
        return self

    # --- Lectura -------------------------------------------------------------------------

    def _load(self, kind: str, payload: bytes) -> Any:
        return self.serde.loads_typed((kind, payload))

    def _tuple(self, row: store.CheckpointRow, writes: Sequence[store.WriteRow]) -> CheckpointTuple:
        checkpoint = self._load(row.type, row.checkpoint)
        metadata = self._load(row.type, row.metadata)
        if (
            not isinstance(checkpoint, dict)
            or not set(checkpoint) >= CHECKPOINT_KEYS
            or not isinstance(checkpoint["channel_values"], dict)
            or checkpoint["id"] != row.checkpoint_id
            or not isinstance(metadata, dict)
        ):
            raise CheckpointUnreadableError("checkpoint con otra forma")
        checkpoint.setdefault("versions_seen", {})
        checkpoint.setdefault("updated_channels", None)
        pending = [(w.task_id, w.channel, self._load(w.type, w.value)) for w in writes]
        parent = (
            _config(row.thread_id, row.checkpoint_ns, row.parent_checkpoint_id)
            if row.parent_checkpoint_id
            else None
        )
        return CheckpointTuple(
            config=_config(row.thread_id, row.checkpoint_ns, row.checkpoint_id),
            checkpoint=checkpoint,  # type: ignore[arg-type]
            metadata=metadata,  # type: ignore[arg-type]
            parent_config=parent,
            pending_writes=pending,
        )

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        thread_id, namespace, checkpoint_id = _ids(config)

        def read(conn: Connection) -> tuple[store.CheckpointRow | None, list[store.WriteRow]]:
            row = store.get_checkpoint(conn, thread_id, namespace, checkpoint_id)
            if row is None:
                return None, []
            return row, store.list_writes(conn, thread_id, namespace, row.checkpoint_id)

        row, writes = await self._database.run(read)
        return None if row is None else self._tuple(row, writes)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,  # noqa: A002 - firma de LangGraph
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        if config is None:
            raise ValueError("solo se listan los checkpoints de un hilo")
        thread_id, namespace, _ = _ids(config)
        before_id = _ids(before)[2] if before is not None else None

        def read(conn: Connection) -> list[tuple[store.CheckpointRow, list[store.WriteRow]]]:
            rows = store.list_checkpoints(
                conn, thread_id, checkpoint_ns=namespace, before=before_id
            )
            return [
                (row, store.list_writes(conn, thread_id, row.checkpoint_ns, row.checkpoint_id))
                for row in rows
            ]

        found = 0
        for row, writes in await self._database.run(read):
            item = self._tuple(row, writes)
            if filter and any(item.metadata.get(k) != v for k, v in filter.items()):
                continue
            yield item
            found += 1
            if limit is not None and found >= limit:
                return

    # --- Escritura -----------------------------------------------------------------------

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        del new_versions  # el checkpoint se guarda entero, con sus `channel_values`
        thread_id, namespace, parent = _ids(config)
        kind, blob = self.serde.dumps_typed(checkpoint)
        _, meta = self.serde.dumps_typed(get_serializable_checkpoint_metadata(config, metadata))
        row = store.CheckpointRow(
            thread_id=thread_id,
            checkpoint_ns=namespace,
            checkpoint_id=checkpoint["id"],
            parent_checkpoint_id=parent,
            type=kind,
            checkpoint=blob,
            metadata=meta,
            created_at=format_utc(self._clock()),
        )
        await self._database.run(lambda conn: store.put_checkpoint(conn, row))
        return _config(thread_id, namespace, checkpoint["id"])

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        thread_id, namespace, checkpoint_id = _ids(config)
        if checkpoint_id is None:
            raise ValueError("falta checkpoint_id")
        special: list[store.WriteRow] = []
        regular: list[store.WriteRow] = []
        for index, (channel, value) in enumerate(writes):
            idx = WRITES_IDX_MAP.get(channel, index)
            kind, blob = self.serde.dumps_typed(value)
            row = store.WriteRow(
                thread_id, namespace, checkpoint_id, task_id, task_path, idx, channel, kind, blob
            )
            (special if idx < 0 else regular).append(row)

        def write(conn: Connection) -> None:
            # Como los guardadores oficiales: las escrituras especiales (error, interrupción,
            # reanudación) reemplazan; las normales no pisan las que ya existían.
            with atomic(conn):
                if special:
                    store.put_writes(conn, special, replace=True)
                if regular:
                    store.put_writes(conn, regular, replace=False)

        await self._database.run(write)

    async def adelete_thread(self, thread_id: str) -> None:
        await self._database.run(lambda conn: store.delete_thread(conn, thread_id))

    async def aprune(self, thread_ids: Sequence[str], *, strategy: str = "keep_latest") -> None:
        """`keep_latest`: deja solo el último checkpoint de cada hilo (al terminar la tarea);
        `delete`: los borra todos."""
        if strategy not in ("keep_latest", "delete"):
            raise ValueError("estrategia desconocida")
        for thread_id in thread_ids:
            if strategy == "delete":
                await self.adelete_thread(thread_id)
            else:
                await self._prune_one(thread_id)

    async def _prune_one(self, thread_id: str) -> None:
        await self._database.run(lambda conn: store.prune_checkpoints(conn, thread_id))
