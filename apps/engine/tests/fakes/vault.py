"""Núcleo simulado para el canal de secretos: un llavero en memoria (nunca el real).

Recibe cada `secret_request` que escribe el `SecretBroker` del motor y responde al
momento, como haría el núcleo (ADR 0010 §2 y 3): valida la forma exacta del valor de
`wp/*/token`, `create` falla si ya existe y `get` si no existe. Opcionalmente aplica una
concesión (`grants`) para comprobar que el motor solo pide lo que su operación declara.
"""

from __future__ import annotations

import io
import json
import re
from collections.abc import Callable
from typing import Any, Final

from faro_engine.core.protocol import ProtocolWriter
from faro_engine.core.secrets import SecretBroker

WP_VALUE_RE: Final = re.compile(
    r'\{"v":1,"token":"[A-Za-z0-9_-]{43}","hmac_secret":"[A-Za-z0-9_-]{43}"\}'
)


class _Stream(io.BytesIO):
    def __init__(self, vault: FakeVault) -> None:
        super().__init__()
        self._vault = vault

    def write(self, line: Any) -> int:
        request = json.loads(bytes(line))
        reply = self._vault.answer(request)
        message = {"event": "secret_response", "id": request["id"], **reply}
        self._vault.broker.handle_response(message)
        return len(line)


class FakeVault:
    """Llavero en memoria detrás de un `SecretBroker` real."""

    def __init__(self, *, timeout: float = 5.0) -> None:
        self.store: dict[str, str] = {}
        self.ops: list[tuple[str, str]] = []
        self.run_ids: list[str] = []
        # (op, ref) → código de error forzado (p. ej. llavero caído).
        self.failures: dict[tuple[str, str], str] = {}
        # Concesión opcional (como el núcleo): decide si `(op, ref)` está permitido.
        self.grants: Callable[[str, str], bool] | None = None
        self.broker = SecretBroker(ProtocolWriter(_Stream(self)), timeout=timeout)

    def fail(self, op: str, ref: str, code: str) -> None:
        self.failures[(op, ref)] = code

    def answer(self, request: dict[str, Any]) -> dict[str, Any]:
        op: str = request["op"]
        ref: str = request["ref"]
        self.ops.append((op, ref))
        self.run_ids.append(request["run_id"])
        forced = self.failures.get((op, ref))
        if forced is not None:
            return {"error": forced}
        if self.grants is not None and not self.grants(op, ref):
            return {"error": "vault.secret_not_allowed"}
        if op == "get":
            if ref not in self.store:
                return {"error": "vault.not_found"}
            return {"value": self.store[ref]}
        if op in {"create", "set"}:
            value: str = request["value"]
            if WP_VALUE_RE.fullmatch(value) is None:
                return {"error": "vault.invalid_input"}
            if op == "create" and ref in self.store:
                return {"error": "vault.already_exists"}
            self.store[ref] = value
            return {"ok": True}
        self.store.pop(ref, None)  # delete: idempotente
        return {"ok": True}


class OperationGrant:
    """Concesión de una llamada a partir de `engine-operations.json` (ADR 0010 §3).

    `{param}` se sustituye por el parámetro de ruta; `{new}` permite una sola `create` de
    un `wp/<uuid>/token` nuevo y el `delete` de esa misma referencia.
    """

    _WP_REF: Final = re.compile(r"wp/[0-9a-f-]{36}/token")

    def __init__(self, secrets: list[dict[str, Any]], path: dict[str, str]) -> None:
        self._fixed: set[tuple[str, str]] = set()
        self._new: set[str] = set()
        self.created: str | None = None
        for grant in secrets:
            ref: str = grant["ref"]
            if ref == "wp/{new}/token":
                self._new = set(grant["access"])
                continue
            for name, value in path.items():
                ref = ref.replace("{" + name + "}", value)
            self._fixed |= {(op, ref) for op in grant["access"]}

    def __call__(self, op: str, ref: str) -> bool:
        if (op, ref) in self._fixed:
            return True
        if (
            op == "create"
            and "create" in self._new
            and self.created is None
            and self._WP_REF.fullmatch(ref)
        ):
            self.created = ref
            return True
        return op == "delete" and "delete" in self._new and ref == self.created
