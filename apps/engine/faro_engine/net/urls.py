"""Normalización y validación de URL de sitios (ADR 0012, regla 1).

- Se recortan espacios; sin esquema → `https://`. Máximo 2048 caracteres.
- Solo `https`. `http` solo en el modo de sitios locales y solo hacia `localhost`,
  `127.0.0.1` o `::1` (`site.https_required` en otro caso).
- Sin usuario ni contraseña (`site.invalid_url`).
- Host en minúsculas; nombres internacionales en punycode (IDNA 2008, UTS 46).
- Sin puerto o puerto 443. En modo local, cualquier puerto hacia loopback **salvo el del
  propio motor** (`site.address_not_allowed`).
- Se quitan consulta y fragmento; se conserva la ruta (WordPress en subcarpeta) sin barra
  final.
- `localhost`, `*.localhost` y las IP escritas en la URL que no sean públicas se rechazan
  aquí mismo (`site.address_not_allowed`); los nombres se comprueban después de resolverlos
  (`guard.py`).
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Final
from urllib.parse import quote, urlsplit

import idna

from faro_engine.core.errors import (
    SITE_ADDRESS_NOT_ALLOWED,
    SITE_HTTPS_REQUIRED,
    SITE_INVALID_URL,
    site_error,
)
from faro_engine.net.guard import IPAddress, is_forbidden_address

MAX_URL_LENGTH: Final = 2048
HTTPS_PORT: Final = 443
HTTP_PORT: Final = 80
# Hosts de loopback que admite el modo de sitios locales (ADR 0012).
LOCAL_HOSTS: Final = frozenset({"localhost", "127.0.0.1", "::1"})

_SCHEME_RE: Final = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_LABEL_RE: Final = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
# Última etiqueta numérica o hexadecimal: formas raras de escribir una IP (`2130706433`,
# `0x7f.1`) que algunos sistemas resuelven a loopback.
_NUMERIC_TLD_RE: Final = re.compile(r"[0-9]+|0x[0-9a-f]*")
# Caracteres de la ruta que se dejan tal cual (RFC 3986 `pchar` + `/` + `%` ya codificado).
_PATH_SAFE: Final = "/%:@!$&'()*+,;=-._~"


@dataclass(frozen=True, slots=True)
class NetPolicy:
    """Qué destinos se permiten. `allow_local` solo en desarrollo (`--allow-local-sites`)."""

    allow_local: bool = False
    engine_port: int | None = None


@dataclass(frozen=True, slots=True)
class Target:
    """Destino ya validado de una petición (sin ruta ni consulta)."""

    scheme: str
    host: str  # punycode en minúsculas, o la IP sin corchetes
    port: int
    ip: IPAddress | None  # si el host es una IP escrita en la URL
    local: bool  # host de loopback admitido por el modo de sitios locales

    @property
    def default_port(self) -> int:
        return HTTPS_PORT if self.scheme == "https" else HTTP_PORT

    @property
    def netloc(self) -> str:
        host = f"[{self.host}]" if isinstance(self.ip, ipaddress.IPv6Address) else self.host
        return host if self.port == self.default_port else f"{host}:{self.port}"


def _invalid() -> Exception:
    return site_error(SITE_INVALID_URL)


def _host(raw_host: str) -> tuple[str, IPAddress | None]:
    """Host en minúsculas y punycode, o la IP. `site.invalid_url` si no es válido."""
    text = raw_host.lower()
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        pass
    else:
        return str(ip), ip
    text = text.removesuffix(".")
    if not text:
        raise _invalid()
    try:
        ascii_host = idna.encode(text, uts46=True).decode("ascii")
    except (idna.IDNAError, UnicodeError) as exc:
        raise _invalid() from exc
    labels = ascii_host.split(".")
    if len(ascii_host) > 253 or not all(_LABEL_RE.fullmatch(label) for label in labels):
        raise _invalid()  # pragma: no cover - idna ya lo rechaza; defensa si cambia
    if _NUMERIC_TLD_RE.fullmatch(labels[-1]):
        raise _invalid()
    return ascii_host, None


def check_target(scheme: str, raw_host: str | None, port: int | None, policy: NetPolicy) -> Target:
    """Valida esquema, host y puerto con las reglas de ADR 0012 (antes de resolver)."""
    scheme = scheme.lower()
    if scheme not in {"http", "https"} or not raw_host:
        raise _invalid()
    host, ip = _host(raw_host)
    local_name = host in LOCAL_HOSTS
    local = policy.allow_local and local_name
    default_port = HTTPS_PORT if scheme == "https" else HTTP_PORT
    real_port = default_port if port is None else port

    if local:
        if policy.engine_port is not None and real_port == policy.engine_port:
            raise site_error(SITE_ADDRESS_NOT_ALLOWED)
        return Target(scheme, host, real_port, ip, local=True)

    if local_name or host.endswith(".localhost") or (ip is not None and is_forbidden_address(ip)):
        raise site_error(SITE_ADDRESS_NOT_ALLOWED)
    if scheme != "https":
        raise site_error(SITE_HTTPS_REQUIRED)
    if real_port != HTTPS_PORT:
        raise _invalid()
    return Target(scheme, host, real_port, ip, local=False)


def _split(url: str) -> tuple[str, str | None, int | None, str]:
    """Esquema, host, puerto y ruta. `site.invalid_url` si la URL está mal formada."""
    if any(ord(ch) <= 0x20 or ord(ch) == 0x7F or ch == "\\" for ch in url):
        raise _invalid()
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise _invalid() from exc
    if "@" in parts.netloc:
        raise _invalid()
    return parts.scheme, parts.hostname, port, parts.path


def target_of(url: str, policy: NetPolicy) -> Target:
    """Destino de una URL completa (con ruta y consulta) que el motor va a pedir."""
    scheme, host, port, _path = _split(url)
    return check_target(scheme, host, port, policy)


def normalize_site_url(raw: str, policy: NetPolicy) -> str:
    """Dirección del sitio normalizada: `https://host[:puerto][/ruta]` sin barra final."""
    text = raw.strip()
    if not text or len(text) > MAX_URL_LENGTH:
        raise _invalid()
    if not _SCHEME_RE.match(text):
        text = "https://" + text
    scheme, host, port, path = _split(text)
    target = check_target(scheme, host, port, policy)
    segments = path.split("/")
    if any(segment in {".", ".."} for segment in segments):
        raise _invalid()
    clean_path = quote(path, safe=_PATH_SAFE).rstrip("/")
    result = f"{target.scheme}://{target.netloc}{clean_path}"
    if len(result) > MAX_URL_LENGTH:
        raise _invalid()
    return result
