"""Escribe el esquema OpenAPI del motor en stdout (lo usa `npm run contracts`).

Uso: `uv run --directory apps/engine python -m faro_engine.export_openapi`
"""

from __future__ import annotations

import json
import secrets
import sys
from typing import Any, BinaryIO

from faro_engine import __version__
from faro_engine.core.app import create_app
from faro_engine.core.config import Settings


def build_openapi() -> dict[str, Any]:
    # Token desechable: la app nunca se sirve, solo se genera su esquema.
    settings = Settings(
        token=secrets.token_urlsafe(32).encode("ascii"), port=0, version=__version__
    )
    return create_app(settings).openapi()


def render_openapi() -> bytes:
    text = json.dumps(build_openapi(), indent=2, ensure_ascii=False)
    return (text + "\n").encode("utf-8")


def main(out: BinaryIO | None = None) -> int:
    target = out if out is not None else sys.stdout.buffer
    target.write(render_openapi())
    target.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover - punto de entrada
    raise SystemExit(main())
