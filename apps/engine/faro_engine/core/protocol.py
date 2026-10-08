"""Protocolo núcleo ↔ motor por stdin/stdout (skill `tauri-sidecar-python`, ADR 0004 y 0010).

- stdin: 1.ª línea = token de sesión; 2.ª = `db_key` con la llave de la base del perfil
  (o el error del núcleo); después, eventos JSON: `shutdown`, `secret_response`, `audit` y,
  desde F1b (ADR 0014), `run_grant_response` y `agents_control`.
- stdout: solo eventos JSON del protocolo, una línea cada uno (`ready`, `secret_request` y,
  desde F1b, `run_grant_request`, `run_grant_release` y `agent_activity`), siempre por
  `ProtocolWriter` (candado + una sola escritura + `flush`).

Nunca se registra el contenido de ninguna línea de stdin.

Copias de secretos en memoria (inevitables en Python, igual que en T5): el `bytes` que
devuelve `os.read`, el `str` que crea `json.loads` al decodificar la línea y el `str` del
valor dentro del diccionario. Las líneas se entregan como `bytearray` y quien las consume
las sobrescribe (`wipe_line`); el búfer del lector se pone a cero antes de descartar lo
ya leído.
"""

from __future__ import annotations

import json
import os
import queue
import re
import threading
from dataclasses import dataclass, field
from typing import Any, BinaryIO, Final

from faro_engine.core.db.connection import is_valid_key_hex, wipe
from faro_engine.core.db.profile import is_valid_profile_id
from faro_engine.core.errors import DB_KEY_MISSING, VAULT_KEYRING_UNAVAILABLE

TOKEN_PATTERN: Final = re.compile(rb"[A-Za-z0-9_-]{43}")
MAX_LINE_BYTES: Final = 64 * 1024
_READ_CHUNK: Final = 4096
_UTF8_BOM: Final = b"\xef\xbb\xbf"

EVENT_SHUTDOWN: Final = "shutdown"
EVENT_DB_KEY: Final = "db_key"
EVENT_READY: Final = "ready"
EVENT_SECRET_REQUEST: Final = "secret_request"  # noqa: S105 - nombre de evento
EVENT_SECRET_RESPONSE: Final = "secret_response"  # noqa: S105 - nombre de evento
EVENT_AUDIT: Final = "audit"
# Errores que el núcleo puede enviar en lugar de la llave (ADR 0010 §1, spec F1a §5.1).
DB_KEY_ERRORS: Final = frozenset({DB_KEY_MISSING, VAULT_KEYRING_UNAVAILABLE})


def is_valid_token(value: bytes) -> bool:
    """32 bytes en base64url sin relleno = exactamente 43 caracteres `[A-Za-z0-9_-]`."""
    return TOKEN_PATTERN.fullmatch(value) is not None


def ready_line(*, port: int, version: str, pid: int) -> bytes:
    payload = {"event": EVENT_READY, "port": port, "version": version, "pid": pid}
    return json.dumps(payload, separators=(",", ":")).encode("ascii") + b"\n"


def wipe_line(line: bytearray) -> None:
    """Sobrescribe con ceros una línea recibida (puede llevar un secreto)."""
    line[:] = bytes(len(line))


def parse_message(line: bytes | bytearray) -> dict[str, Any] | None:
    """Objeto JSON con `"event"` de texto, o `None` si la línea no es un evento válido."""
    try:
        data = json.loads(line)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    if not isinstance(data.get("event"), str):
        data.clear()
        return None
    return data


def parse_event(line: bytes | bytearray) -> str | None:
    """Devuelve el nombre del evento o `None` si la línea no es un evento válido."""
    data = parse_message(line)
    if data is None:
        return None
    event: str = data["event"]
    data.clear()
    return event


class ProtocolWriter:
    """Único escritor de stdout: cada evento es una línea JSON completa, escrita de una vez
    bajo un candado y con `flush`, sin mezclarse con otras (ni con logs, que van a stderr).

    Tras un error de escritura (el núcleo cerró la tubería) queda cerrado y cada intento
    siguiente lanza `BrokenPipeError` sin tocar la salida.
    """

    def __init__(self, out: BinaryIO) -> None:
        self._out = out
        self._lock = threading.Lock()
        self._broken = False

    @property
    def broken(self) -> bool:
        return self._broken

    def write_line(self, line: bytes | bytearray) -> None:
        if not line.endswith(b"\n") or line.count(b"\n") != 1:
            raise ValueError("una línea del protocolo termina en un único salto de línea")
        with self._lock:
            if self._broken:
                raise BrokenPipeError("stdout cerrado")
            try:
                self._out.write(line)
                self._out.flush()
            except (OSError, ValueError) as exc:
                self._broken = True
                raise BrokenPipeError("stdout cerrado") from exc


class StdinReader:
    """Hilo único que lee líneas de un descriptor (stdin) y las pone en una cola.

    Lee con `os.read` sobre el descriptor para no bloquear el lock de `sys.stdin`
    al cerrar el intérprete. Al llegar a EOF (o error de lectura) encola `None`.
    Una línea más larga que `max_line` se descarta y se entrega vacía (inválida).
    Cada línea es un `bytearray` nuevo: quien la consume la sobrescribe con `wipe_line`.
    """

    def __init__(self, fd: int, *, max_line: int = MAX_LINE_BYTES) -> None:
        self._fd = fd
        self._max_line = max_line
        self._queue: queue.Queue[bytearray | None] = queue.Queue()
        self._eof = False
        self._thread = threading.Thread(target=self._run, name="faro-stdin", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def get(self, timeout: float | None = None) -> bytearray | None:
        """Siguiente línea (sin `\\r\\n`), o `None` en EOF. Lanza `queue.Empty` si vence.

        Tras el EOF, todas las llamadas siguientes devuelven `None` al momento: quien lea
        después (p. ej. el vigilante de `shutdown` tras la línea `db_key`) también lo ve.
        """
        if self._eof:
            return None
        line = self._queue.get(timeout=timeout)
        if line is None:
            self._eof = True
        return line

    def _run(self) -> None:
        buf = bytearray()
        discarding = False
        try:
            while True:
                chunk = os.read(self._fd, _READ_CHUNK)
                if not chunk:
                    break
                buf += chunk
                del chunk
                while (index := buf.find(b"\n")) >= 0:
                    line = buf[:index]
                    if line.endswith(b"\r"):
                        del line[-1]
                    # Ceros antes de descartar: `del` al principio de un bytearray solo
                    # mueve su inicio y dejaría la línea en la memoria reservada.
                    buf[: index + 1] = bytes(index + 1)
                    del buf[: index + 1]
                    if discarding or len(line) > self._max_line:
                        discarding = False
                        wipe_line(line)
                        self._queue.put(bytearray())
                    else:
                        self._queue.put(line)
                if len(buf) > self._max_line:
                    wipe_line(buf)
                    buf.clear()
                    discarding = True
        except OSError:
            pass
        if buf and not discarding:
            line = bytearray(buf)
            if line.endswith(b"\r"):
                del line[-1]
            self._queue.put(line)
        wipe_line(buf)
        self._queue.put(None)


def read_token(reader: StdinReader, timeout: float) -> bytes | None:
    """Primera línea de stdin como token. `None` si no llega a tiempo, hay EOF o es inválido."""
    try:
        line = reader.get(timeout=timeout)
    except queue.Empty:
        return None
    if line is None:
        return None
    # Algunos escritores (p. ej. StreamWriter de .NET) anteponen un BOM UTF-8.
    token = bytes(line.removeprefix(_UTF8_BOM))
    wipe_line(line)
    return token if is_valid_token(token) else None


@dataclass(slots=True)
class DbKeyLine:
    """Resultado de la 2.ª línea de stdin. La llave nunca aparece en `repr`.

    Exactamente uno de `key` o `error_code` tiene valor. `reason` explica al log por qué
    no hay llave (`timeout`, `eof`, `invalid`, `core_error`) sin incluir contenido.
    `shutdown` indica que en su lugar llegó `{"event":"shutdown"}`.
    """

    profile_id: str | None = None
    key: bytearray | None = field(default=None, repr=False)
    error_code: str | None = None
    reason: str | None = None
    shutdown: bool = False

    def wipe(self) -> None:
        if self.key is not None:
            wipe(self.key)
            self.key = None


def _missing(reason: str) -> DbKeyLine:
    return DbKeyLine(error_code=DB_KEY_MISSING, reason=reason)


def parse_db_key(line: bytes | bytearray) -> DbKeyLine:
    """`{"event":"db_key","profile":"<uuid>","key":"<64 hex>"}` o con `"error"` en lugar de
    `"key"`. Cualquier otra forma cuenta como llave ausente (`db.key_missing`)."""
    try:
        data = json.loads(line.removeprefix(_UTF8_BOM))
    except ValueError:
        return _missing("invalid")
    if not isinstance(data, dict):
        return _missing("invalid")
    if data.get("event") == EVENT_SHUTDOWN and len(data) == 1:
        return DbKeyLine(shutdown=True, reason="shutdown")
    try:
        if data.get("event") != EVENT_DB_KEY or not is_valid_profile_id(data.get("profile")):
            return _missing("invalid")
        if set(data) == {"event", "profile", "key"}:
            raw = data["key"]
            if isinstance(raw, str) and raw.isascii():
                key = bytearray(raw, "ascii")
                if is_valid_key_hex(key):
                    return DbKeyLine(profile_id=data["profile"], key=key)
                wipe(key)
            return _missing("invalid")
        if set(data) == {"event", "profile", "error"} and data["error"] in DB_KEY_ERRORS:
            return DbKeyLine(
                profile_id=data["profile"], error_code=data["error"], reason="core_error"
            )
        return _missing("invalid")
    finally:
        # La copia `str` de la llave dentro de `data` no se puede borrar; se suelta ya.
        data.clear()


def read_db_key(reader: StdinReader, timeout: float) -> DbKeyLine:
    """2.ª línea de stdin (ADR 0010 §1). Si no llega a tiempo, la base queda sin llave."""
    try:
        line = reader.get(timeout=timeout)
    except queue.Empty:
        return _missing("timeout")
    if line is None:
        return _missing("eof")
    try:
        return parse_db_key(line)
    finally:
        wipe_line(line)
