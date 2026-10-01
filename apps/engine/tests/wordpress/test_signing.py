"""Firma v1 con los vectores compartidos y formato del secreto del sitio (ADR 0011 §2 y 3).

Los vectores de `packages/shared/fixtures/wp-signature-v1.json` los usa también PHPUnit en
el plugin: si esta prueba falla, la app y el plugin ya no firman igual.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from faro_engine.wordpress import signing
from faro_engine.wordpress.signing import (
    HEADER_CONNECTION,
    HEADER_NONCE,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    HEADER_TOKEN,
    SiteCredentials,
    canonical,
    canonical_route,
    decode_b64url_32,
    is_b64url_32,
    new_nonce,
    parse_secret_value,
    rawurlencode,
    sign,
    signed_headers,
)

FIXTURES = Path(__file__).resolve().parents[4] / "packages" / "shared" / "fixtures"
VECTORS: dict[str, Any] = json.loads(
    (FIXTURES / "wp-signature-v1.json").read_text(encoding="utf-8")
)
CONNECTION_ID = "0192f0a0-0001-4abc-8def-0123456789ab"


def _b64(data: bytes) -> bytes:
    return base64.urlsafe_b64encode(data).rstrip(b"=")


# Valores de prueba con forma evidente (32 bytes `test-…`), nunca credenciales reales.
TOKEN = _b64(b"test-token-000000000000000000000")
HMAC_SECRET = _b64(b"test-hmac-secret-000000000000000")


def test_vectors_are_complete() -> None:
    assert VECTORS["version"] == 1
    assert len(VECTORS["cases"]) >= 8


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda c: c["name"])
def test_shared_vectors(case: dict[str, Any]) -> None:
    route = canonical_route(case["route"], case["query"])
    assert route == case["expected_canonical_route"]
    text = canonical(case["method"], route, case["timestamp"], case["nonce"], case["body"].encode())
    assert text == case["expected_canonical"]
    key = decode_b64url_32(case["hmac_secret"].encode("ascii"))
    assert key is not None
    assert len(key) == 32
    assert sign(text, key) == case["expected_signature"]


def test_canonical_route_accepts_pairs_and_skips_rest_route() -> None:
    assert canonical_route("/faro/v1/pages/", [("rest_route", "/x"), ("b", "2"), ("a", "1")]) == (
        "/faro/v1/pages?a=1&b=2"
    )
    assert canonical_route("/faro/v1/status\\", {}) == "/faro/v1/status"
    assert canonical("get", "/r", "1", "n", b"").startswith("GET\n/r\n1\nn\n")


def test_rawurlencode_matches_php() -> None:
    assert rawurlencode("a b+c~*'()!") == "a%20b%2Bc~%2A%27%28%29%21"
    assert rawurlencode("ñ") == "%C3%B1"


def test_nonce_is_fresh_16_bytes() -> None:
    nonces = {new_nonce() for _ in range(100)}
    assert len(nonces) == 100
    for nonce in nonces:
        assert len(nonce) == 22
        assert len(base64.urlsafe_b64decode(nonce + "==")) == 16


@pytest.mark.parametrize(
    "value",
    [
        b"",
        b"a" * 42,
        b"a" * 44,
        b"a" * 42 + b"=",
        b"a" * 42 + b"+",
        b"A" * 42 + b"B",  # los 2 bits sobrantes no son cero: forma no canónica
    ],
)
def test_decode_rejects_non_canonical(value: bytes) -> None:
    assert decode_b64url_32(value) is None
    assert not is_b64url_32(value)


def test_credentials_roundtrip_and_wipe() -> None:
    credentials = SiteCredentials.from_encoded(CONNECTION_ID, TOKEN, HMAC_SECRET)
    assert credentials is not None
    value = credentials.secret_value()
    expected = json.dumps(
        {"v": 1, "token": TOKEN.decode(), "hmac_secret": HMAC_SECRET.decode()},
        separators=(",", ":"),
    ).encode()
    assert bytes(value) == expected  # exactamente el JSON compacto que valida el núcleo
    assert credentials.token_sha256() == hashlib.sha256(TOKEN).hexdigest()
    parsed = parse_secret_value(CONNECTION_ID, value)
    assert parsed is not None
    assert bytes(parsed.token) == TOKEN
    assert bytes(parsed.hmac_key) == b"test-hmac-secret-000000000000000"
    assert "test" not in repr(parsed)
    assert CONNECTION_ID in repr(parsed)
    token_buffer, key_buffer = parsed.token, parsed.hmac_key
    with parsed:
        pass
    assert token_buffer == bytearray(len(token_buffer))
    assert key_buffer == bytearray(32)
    with pytest.raises(ValueError, match="borraron"):
        _ = parsed.token
    with pytest.raises(ValueError, match="borraron"):
        _ = parsed.hmac_key
    parsed.wipe()  # idempotente


@pytest.mark.parametrize(
    "value",
    [
        b"",
        b'{"v":1,"token":"x","hmac_secret":"y"}',
        b'{"v": 1, "token": "' + TOKEN + b'", "hmac_secret": "' + HMAC_SECRET + b'"}',
        b'{"token":"' + TOKEN + b'","v":1,"hmac_secret":"' + HMAC_SECRET + b'"}',
        b'{"v":2,"token":"' + TOKEN + b'","hmac_secret":"' + HMAC_SECRET + b'"}',
        b'{"v":1,"token":"' + TOKEN + b'","hmac_secret":"' + HMAC_SECRET + b'"}\n',
        b'{"v":1,"token":"' + b"A" * 42 + b'B","hmac_secret":"' + HMAC_SECRET + b'"}',
        b'{"v":1,"token":"' + TOKEN + b'","hmac_secret":"' + b"A" * 42 + b'B"}',
    ],
)
def test_parse_secret_value_rejects_other_shapes(value: bytes) -> None:
    assert parse_secret_value(CONNECTION_ID, value) is None


def test_signed_headers() -> None:
    credentials = SiteCredentials.from_encoded(CONNECTION_ID, TOKEN, HMAC_SECRET)
    assert credentials is not None
    headers = signed_headers(
        credentials,
        method="GET",
        route="/faro/v1/posts",
        query={"per_page": "50", "page": "2"},
        body=b"",
        timestamp=1790000100,
        nonce="dGVzdC1ub25jZS0wMDAwMg",
    )
    text = canonical(
        "GET", "/faro/v1/posts?page=2&per_page=50", "1790000100", "dGVzdC1ub25jZS0wMDAwMg", b""
    )
    assert headers == {
        HEADER_CONNECTION: CONNECTION_ID,
        HEADER_TOKEN: TOKEN.decode(),
        HEADER_TIMESTAMP: "1790000100",
        HEADER_NONCE: "dGVzdC1ub25jZS0wMDAwMg",
        HEADER_SIGNATURE: sign(text, b"test-hmac-secret-000000000000000"),
    }
    assert signing.SIGNATURE_VERSION == 1
