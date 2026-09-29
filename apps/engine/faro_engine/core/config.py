"""Configuración del motor y del modo de desarrollo (ADR 0004)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from faro_engine.core.protocol import is_valid_token

# El motor solo escucha aquí. Nunca 0.0.0.0.
HOST = "127.0.0.1"

# Tiempos del protocolo (spec F0, §4.4).
TOKEN_TIMEOUT_SECONDS = 10.0
SHUTDOWN_GRACE_SECONDS = 10.0

# Modo desarrollo "externo".
DEFAULT_DEV_PORT = 8765
DEV_TOKEN_VAR = "FARO_ENGINE_DEV_TOKEN"  # noqa: S105 - nombre de variable, no un secreto
DEV_PORT_VAR = "FARO_ENGINE_DEV_PORT"
MIN_DEV_PORT = 1024
MAX_PORT = 65535


def repo_root() -> Path:
    """Raíz del repositorio: `faro_engine/core/config.py` → `apps/engine` → raíz."""
    return Path(__file__).resolve().parents[4]


def default_env_file() -> Path:
    return repo_root() / ".env.local"


@dataclass(frozen=True, slots=True)
class Settings:
    """Parámetros de una sesión del motor. El token nunca aparece en `repr`."""

    token: bytes = field(repr=False)
    port: int
    version: str
    dev: bool = False
    data_dir: Path | None = None

    @property
    def expected_host(self) -> str:
        return f"{HOST}:{self.port}"


class DevConfigError(Exception):
    """Configuración de `.env.local` ausente o inválida. `reason` no contiene valores."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class DevConfig:
    token: bytes = field(repr=False)
    port: int


def parse_env_file(text: str) -> dict[str, str]:
    """Lee líneas `CLAVE=valor`; ignora vacías y comentarios. Quita comillas simples o dobles."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def load_dev_config(env_file: Path) -> DevConfig:
    """Carga token y puerto del modo `--dev` desde `.env.local`."""
    try:
        text = env_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise DevConfigError("env_file_missing") from exc

    values = parse_env_file(text)
    raw_token = values.get(DEV_TOKEN_VAR)
    if not raw_token:
        raise DevConfigError("token_missing")
    token = raw_token.encode("ascii", errors="replace")
    if not is_valid_token(token):
        raise DevConfigError("token_invalid")

    raw_port = values.get(DEV_PORT_VAR, "").strip()
    if not raw_port:
        port = DEFAULT_DEV_PORT
    else:
        try:
            port = int(raw_port)
        except ValueError as exc:
            raise DevConfigError("port_invalid") from exc
        if not MIN_DEV_PORT <= port <= MAX_PORT:
            raise DevConfigError("port_invalid")
    return DevConfig(token=token, port=port)
