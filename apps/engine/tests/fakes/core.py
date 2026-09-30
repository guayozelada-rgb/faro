"""El núcleo visto desde el motor: `engine_call` con la concesión de cada operación.

Las operaciones salen de `packages/shared/engine-operations.json` (lo que el núcleo
incrusta al compilar): método, ruta, `X-Faro-Run-Id` solo si la operación tiene secretos
y el llavero simulado aplica exactamente sus `secrets` (ADR 0010 §3).
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import httpx

from tests.fakes.vault import FakeVault, OperationGrant

OPERATIONS: dict[str, dict[str, Any]] = {
    op["operationId"]: op
    for op in json.loads(
        (
            Path(__file__).resolve().parents[4] / "packages" / "shared" / "engine-operations.json"
        ).read_text(encoding="utf-8")
    )
}


class Core:
    """El núcleo visto desde el motor: `engine_call` con concesión por llamada."""

    def __init__(self, http: httpx.AsyncClient, vault: FakeVault, token: str) -> None:
        self.http = http
        self.vault = vault
        self.token = token

    async def call(
        self,
        operation: str,
        *,
        path: dict[str, str] | None = None,
        query: dict[str, str] | None = None,
        body: Any = None,
        run_id: bool = True,
    ) -> httpx.Response:
        op = OPERATIONS[operation]
        url = op["path"]
        for name, value in (path or {}).items():
            url = url.replace("{" + name + "}", value)
        headers = {"Authorization": f"Bearer {self.token}"}
        if op["secrets"] and run_id:
            headers["X-Faro-Run-Id"] = str(uuid.uuid4())
        self.vault.grants = OperationGrant(op["secrets"], path or {})
        return await self.http.request(op["method"], url, params=query, json=body, headers=headers)
