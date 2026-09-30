"""Firma v1 de las peticiones al plugin y formato del secreto del sitio (ADR 0011 §2 y 3).

Canónica (idéntica a `Faro_Signature` en PHP; vectores en
`packages/shared/fixtures/wp-signature-v1.json`):

    METHOD "\\n" ROUTE "\\n" TIMESTAMP "\\n" NONCE "\\n" hex(sha256(raw_body))

- `METHOD` en mayúsculas. `ROUTE` = ruta REST sin barra final; con consulta (salvo
  `rest_route`), `?` + pares `rawurlencode(clave)=rawurlencode(valor)` (RFC 3986, hex en
  mayúsculas) ordenados por clave y luego valor codificados, unidos con `&`.
- `TIMESTAMP` = segundos Unix; `NONCE` = 16 bytes aleatorios en base64url sin relleno.
- Firma = base64 estándar de HMAC-SHA256 con la clave = **32 bytes decodificados** de
  `hmac_secret`.

Secreto del sitio en el llavero (`wp/<site_id>/token`): exactamente el JSON compacto
`{"v":1,"token":"<43 base64url>","hmac_secret":"<43 base64url>"}` (el núcleo lo compara
byte a byte). Se arma y se lee en `bytearray`, sin pasar por `str`, y se sobrescribe al
terminar (`SiteCredentials.wipe`).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import secrets
from collections.abc import Iterable, Mapping
from types import TracebackType
from typing import Final
from urllib.parse import quote

SIGNATURE_VERSION: Final = 1
NONCE_BYTES: Final = 16
SECRET_BYTES: Final = 32
B64URL_32_LEN: Final = 43

HEADER_CONNECTION: Final = "X-Faro-Connection"
HEADER_TOKEN: Final = "X-Faro-Token"  # noqa: S105 - nombre de cabecera
HEADER_TIMESTAMP: Final = "X-Faro-Timestamp"
HEADER_NONCE: Final = "X-Faro-Nonce"
HEADER_SIGNATURE: Final = "X-Faro-Signature"

_B64URL_43: Final = rb"[A-Za-z0-9_-]{43}"
SECRET_VALUE_RE: Final = re.compile(
    rb'\{"v":1,"token":"(' + _B64URL_43 + rb')","hmac_secret":"(' + _B64URL_43 + rb')"\}'
)
_SECRET_PREFIX: Final = b'{"v":1,"token":"'
_SECRET_MIDDLE: Final = b'","hmac_secret":"'
_SECRET_SUFFIX: Final = b'"}'


def _wipe(buffer: bytearray) -> None:
    buffer[:] = bytes(len(buffer))


def decode_b64url_32(value: bytes | bytearray) -> bytearray | None:
    """32 bytes de un base64url sin relleno de 43 caracteres en forma canónica; si no, `None`."""
    if re.fullmatch(_B64URL_43, value) is None:
        return None
    try:
        raw = bytearray(base64.urlsafe_b64decode(bytes(value) + b"="))
    except (binascii.Error, ValueError):  # pragma: no cover - la expresión ya lo impide
        return None
    # Forma canónica: los 2 bits sobrantes del último carácter deben ser cero.
    if base64.urlsafe_b64encode(raw).rstrip(b"=") != bytes(value):
        _wipe(raw)
        return None
    return raw


def is_b64url_32(value: bytes | bytearray) -> bool:
    raw = decode_b64url_32(value)
    if raw is None:
        return False
    _wipe(raw)
    return True


class SiteCredentials:
    """Credenciales de una conexión en memoria. `repr` nunca muestra los valores."""

    __slots__ = ("_hmac_key", "_token", "connection_id")

    def __init__(self, connection_id: str, token: bytearray, hmac_key: bytearray) -> None:
        self.connection_id = connection_id
        self._token: bytearray | None = token
        self._hmac_key: bytearray | None = hmac_key

    @classmethod
    def from_encoded(
        cls, connection_id: str, token: bytes | bytearray, hmac_secret: bytes | bytearray
    ) -> SiteCredentials | None:
        """Desde `token` y `hmac_secret` en base64url; `None` si no tienen la forma v1."""
        if not is_b64url_32(token):
            return None
        key = decode_b64url_32(hmac_secret)
        if key is None:
            return None
        return cls(connection_id, bytearray(token), key)

    @property
    def token(self) -> bytearray:
        if self._token is None:
            raise ValueError("las credenciales ya se borraron")
        return self._token

    @property
    def hmac_key(self) -> bytearray:
        if self._hmac_key is None:
            raise ValueError("las credenciales ya se borraron")
        return self._hmac_key

    def token_sha256(self) -> str:
        """`sha256(token)` en hex: lo único del token que se guarda en la base."""
        return hashlib.sha256(self.token).hexdigest()

    def secret_value(self) -> bytearray:
        """Valor exacto para el llavero (`wp/<site_id>/token`). Quien llama lo sobrescribe."""
        secret = bytearray(base64.urlsafe_b64encode(self.hmac_key).rstrip(b"="))
        try:
            return bytearray(_SECRET_PREFIX + self.token + _SECRET_MIDDLE + secret + _SECRET_SUFFIX)
        finally:
            _wipe(secret)

    def wipe(self) -> None:
        for buffer in (self._token, self._hmac_key):
            if buffer is not None:
                _wipe(buffer)
        self._token = None
        self._hmac_key = None

    def __enter__(self) -> SiteCredentials:
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _tb: TracebackType | None,
    ) -> None:
        self.wipe()

    def __repr__(self) -> str:
        return f"SiteCredentials(connection_id={self.connection_id!r}, [oculto])"


def parse_secret_value(connection_id: str, value: bytes | bytearray) -> SiteCredentials | None:
    """Credenciales desde el valor del llavero; `None` si no es exactamente el JSON v1."""
    match = SECRET_VALUE_RE.fullmatch(value)
    if match is None:
        return None
    token = bytearray(match.group(1))
    hmac_secret = bytearray(match.group(2))
    try:
        return SiteCredentials.from_encoded(connection_id, token, hmac_secret)
    finally:
        _wipe(token)
        _wipe(hmac_secret)


def rawurlencode(value: str) -> str:
    """Como `rawurlencode` de PHP: solo `A-Z a-z 0-9 - _ . ~` sin codificar."""
    return quote(value, safe="", encoding="utf-8")


def canonical_route(route: str, query: Mapping[str, str] | Iterable[tuple[str, str]]) -> str:
    """Ruta REST sin barra final + consulta ordenada (sin `rest_route`)."""
    items = query.items() if isinstance(query, Mapping) else query
    pairs = sorted(
        (rawurlencode(key), rawurlencode(value)) for key, value in items if key != "rest_route"
    )
    route = route.rstrip("/\\")
    if not pairs:
        return route
    return route + "?" + "&".join(f"{key}={value}" for key, value in pairs)


def canonical(method: str, route: str, timestamp: str, nonce: str, body: bytes) -> str:
    """Texto que se firma. `route` es el resultado de `canonical_route`."""
    digest = hashlib.sha256(body).hexdigest()
    return "\n".join((method.upper(), route, timestamp, nonce, digest))


def sign(canonical_text: str, key: bytes | bytearray) -> str:
    """Base64 estándar (con relleno) de HMAC-SHA256."""
    mac = hmac.new(key, canonical_text.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(mac).decode("ascii")


def new_nonce() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(NONCE_BYTES)).rstrip(b"=").decode("ascii")


def signed_headers(
    credentials: SiteCredentials,
    *,
    method: str,
    route: str,
    query: Mapping[str, str],
    body: bytes,
    timestamp: int,
    nonce: str,
) -> dict[str, str]:
    """Cabeceras `X-Faro-*` de una petición firmada."""
    text = canonical(method, canonical_route(route, query), str(timestamp), nonce, body)
    return {
        HEADER_CONNECTION: credentials.connection_id,
        HEADER_TOKEN: credentials.token.decode("ascii"),
        HEADER_TIMESTAMP: str(timestamp),
        HEADER_NONCE: nonce,
        HEADER_SIGNATURE: sign(text, credentials.hmac_key),
    }
