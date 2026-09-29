"""Protocolo núcleo ↔ motor por stdin/stdout (skill `tauri-sidecar-python`, ADR 0004).

- stdin: primera línea = token de sesión; después, eventos JSON (`{"event":"shutdown"}`).
- stdout: solo eventos JSON del protocolo, una línea cada uno (`ready`).
"""

from __future__ import annotations

import json
import os
import queue
import re
import threading
from typing import Final

TOKEN_PATTERN: Final = re.compile(rb"[A-Za-z0-9_-]{43}")
MAX_LINE_BYTES: Final = 64 * 1024
_READ_CHUNK: Final = 4096
_UTF8_BOM: Final = b"\xef\xbb\xbf"

EVENT_SHUTDOWN: Final = "shutdown"


def is_valid_token(value: bytes) -> bool:
    """32 bytes en base64url sin relleno = exactamente 43 caracteres `[A-Za-z0-9_-]`."""
    return TOKEN_PATTERN.fullmatch(value) is not None


def ready_line(*, port: int, version: str, pid: int) -> bytes:
    payload = {"event": "ready", "port": port, "version": version, "pid": pid}
    return json.dumps(payload, separators=(",", ":")).encode("ascii") + b"\n"


def parse_event(line: bytes) -> str | None:
    """Devuelve el nombre del evento o `None` si la línea no es un evento válido."""
    try:
        data = json.loads(line)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    event = data.get("event")
    return event if isinstance(event, str) else None


class StdinReader:
    """Hilo único que lee líneas de un descriptor (stdin) y las pone en una cola.

    Lee con `os.read` sobre el descriptor para no bloquear el lock de `sys.stdin`
    al cerrar el intérprete. Al llegar a EOF (o error de lectura) encola `None`.
    Una línea más larga que `max_line` se descarta y se entrega como `b""` (inválida).
    """

    def __init__(self, fd: int, *, max_line: int = MAX_LINE_BYTES) -> None:
        self._fd = fd
        self._max_line = max_line
        self._queue: queue.Queue[bytes | None] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="faro-stdin", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def get(self, timeout: float | None = None) -> bytes | None:
        """Siguiente línea (sin `\\r\\n`), o `None` en EOF. Lanza `queue.Empty` si vence."""
        return self._queue.get(timeout=timeout)

    def _run(self) -> None:
        buf = bytearray()
        discarding = False
        try:
            while True:
                chunk = os.read(self._fd, _READ_CHUNK)
                if not chunk:
                    break
                buf += chunk
                while (index := buf.find(b"\n")) >= 0:
                    line = bytes(buf[:index]).rstrip(b"\r")
                    del buf[: index + 1]
                    if discarding or len(line) > self._max_line:
                        discarding = False
                        self._queue.put(b"")
                    else:
                        self._queue.put(line)
                if len(buf) > self._max_line:
                    buf.clear()
                    discarding = True
        except OSError:
            pass
        if buf and not discarding:
            self._queue.put(bytes(buf).rstrip(b"\r"))
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
    line = line.removeprefix(_UTF8_BOM)
    return line if is_valid_token(line) else None
