"""Configuración del motor y del modo de desarrollo (ADR 0004)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from faro_engine.core.db.connection import is_valid_key_hex
from faro_engine.core.db.profile import is_valid_profile_id
from faro_engine.core.protocol import is_valid_token

# El motor solo escucha aquí. Nunca 0.0.0.0.
HOST = "127.0.0.1"

# Tiempos del protocolo (spec F0, §4.4).
TOKEN_TIMEOUT_SECONDS = 10.0
DB_KEY_TIMEOUT_SECONDS = 10.0  # 2.ª línea de stdin (ADR 0010 §1)
SHUTDOWN_GRACE_SECONDS = 10.0
SECRET_TIMEOUT_SECONDS = 10.0  # espera de cada `secret_response` (ADR 0010 §2)

# Modo desarrollo "externo".
DEFAULT_DEV_PORT = 8765
DEV_TOKEN_VAR = "FARO_ENGINE_DEV_TOKEN"  # noqa: S105 - nombre de variable, no un secreto
DEV_PORT_VAR = "FARO_ENGINE_DEV_PORT"
# Base de desarrollo (ADR 0009, excepción consciente solo en `--dev`, nunca datos reales).
DEV_DB_KEY_VAR = "FARO_ENGINE_DEV_DB_KEY"
DEV_PROFILE_ID_VAR = "FARO_ENGINE_DEV_PROFILE_ID"
# Modo de sitios locales en `--dev` (ADR 0012). Solo el valor exacto `1` lo activa.
ALLOW_LOCAL_SITES_VAR = "FARO_ALLOW_LOCAL_SITES"
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
    # Modo de sitios locales (solo desarrollo, ADR 0012): `http` y loopback permitidos.
    allow_local_sites: bool = False

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
    # Llave de la base de desarrollo (64 hex) y perfil; `None` si no están definidos.
    db_key: bytearray | None = field(default=None, repr=False)
    profile_id: str | None = None
    allow_local_sites: bool = False


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

    db_key: bytearray | None = None
    profile_id: str | None = None
    raw_key = values.get(DEV_DB_KEY_VAR, "").strip()
    if raw_key:
        if not raw_key.isascii() or not is_valid_key_hex(raw_key.encode("ascii")):
            raise DevConfigError("db_key_invalid")
        raw_profile = values.get(DEV_PROFILE_ID_VAR, "").strip()
        if not is_valid_profile_id(raw_profile):
            raise DevConfigError("profile_id_invalid")
        db_key = bytearray(raw_key, "ascii")
        profile_id = raw_profile
    return DevConfig(
        token=token,
        port=port,
        db_key=db_key,
        profile_id=profile_id,
        allow_local_sites=values.get(ALLOW_LOCAL_SITES_VAR, "").strip() == "1",
    )
