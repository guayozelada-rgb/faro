"""Filtro de logs por valor y por nombre (ADR 0013), mismas reglas que `redact.rs`."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import pytest
import structlog

from faro_engine.core.logging import configure_logging
from faro_engine.core.redact import (
    REDACTED,
    is_sensitive_name,
    redact_event,
    redact_rendered,
    redact_text,
    redact_value,
    redact_values,
)

# Secretos falsos con la forma de cada patrón (nunca reales).
FAKE_OPENAI = "sk-test" + "A1b2C3d4E5f6G7h8I9j0"
FAKE_ANTHROPIC = "sk-ant-api03-" + "t3st" * 6
FAKE_GOOGLE = "AIza" + "T" * 35
FAKE_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.dGVzdC1zaWduYXR1cmU"  # gitleaks:allow
FAKE_BEARER_TOKEN = "test-bearer-value-123"
FAKE_BASIC = "dGVzdDp0ZXN0LXBhc3N3b3Jk"  # gitleaks:allow
FAKE_HEX_KEY = "ab" * 32
FAKE_BASE64URL = "T" * 20 + "-_" + "e" * 21  # 43 caracteres
FAKE_PAIRING_CODE = "493817"

SECRETS = [
    FAKE_OPENAI,
    FAKE_ANTHROPIC,
    FAKE_GOOGLE,
    FAKE_JWT,
    FAKE_BEARER_TOKEN,
    FAKE_BASIC,
    FAKE_HEX_KEY,
    FAKE_BASE64URL,
]


@pytest.fixture
def stderr_logs(capsys: pytest.CaptureFixture[str]) -> Iterator[pytest.CaptureFixture[str]]:
    """Logs reales a `sys.stderr` (el proxy resuelve el stderr capturado)."""
    configure_logging()
    capsys.readouterr()
    yield capsys
    configure_logging()


def _free_text() -> str:
    return (
        f"openai={FAKE_OPENAI} anthropic {FAKE_ANTHROPIC} google {FAKE_GOOGLE} "
        f"jwt {FAKE_JWT} Authorization: Bearer {FAKE_BEARER_TOKEN} "
        f"basic Basic {FAKE_BASIC} hex {FAKE_HEX_KEY} b64 {FAKE_BASE64URL}"
    )


def test_structlog_to_stderr_redacts_every_pattern(
    stderr_logs: pytest.CaptureFixture[str],
) -> None:
    log = structlog.get_logger("test")
    log.info(
        "test.event",
        message=_free_text(),
        nested={"inner": [f"x {FAKE_JWT}", {"api_key": "test-api-key"}]},
        detail=("tuple", FAKE_HEX_KEY),
        pairing_code=FAKE_PAIRING_CODE,
        hmac_secret="test-hmac",
        secret_ref="wp/01920000-0000-7000-8000-0000000000bb/token",
    )
    err = stderr_logs.readouterr().err
    for secret in [*SECRETS, FAKE_PAIRING_CODE, "test-api-key", "test-hmac"]:
        assert secret not in err
    record = json.loads(err.strip().splitlines()[-1])
    assert "pairing_code" not in record  # nombre sensible en primer nivel: se elimina
    assert "hmac_secret" not in record
    assert record["nested"]["inner"][1]["api_key"] == REDACTED
    assert record["secret_ref"] == "wp/01920000-0000-7000-8000-0000000000bb/token"
    assert REDACTED in record["message"]


def test_stdlib_logs_and_tracebacks_are_redacted(
    stderr_logs: pytest.CaptureFixture[str],
) -> None:
    logging.getLogger("uvicorn.error").warning("fallo con %s", f"Bearer {FAKE_BEARER_TOKEN}")

    def fail() -> None:
        raise ValueError(f"no se pudo usar {FAKE_OPENAI} ni {FAKE_HEX_KEY}")

    try:
        fail()
    except ValueError:
        structlog.get_logger("test").exception("test.failed")
        logging.getLogger("uvicorn.error").exception("stdlib %s", FAKE_GOOGLE)
    err = stderr_logs.readouterr().err
    for secret in [FAKE_BEARER_TOKEN, FAKE_OPENAI, FAKE_HEX_KEY, FAKE_GOOGLE]:
        assert secret not in err
    lines = err.strip().splitlines()
    assert len(lines) == 3
    for line in lines:
        json.loads(line)  # la redacción no rompe el JSON
    assert "Traceback" in json.loads(lines[1])["exception"]


def test_object_repr_is_redacted_on_the_final_line(
    stderr_logs: pytest.CaptureFixture[str],
) -> None:
    class Leaky:
        def __repr__(self) -> str:
            return f"Leaky({FAKE_BASE64URL})"

    structlog.get_logger("test").info("test.object", thing=Leaky())
    err = stderr_logs.readouterr().err
    assert FAKE_BASE64URL not in err
    assert json.loads(err)["thing"] == f"Leaky({REDACTED})"


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        (FAKE_OPENAI, FAKE_OPENAI),
        (FAKE_ANTHROPIC, FAKE_ANTHROPIC),
        (FAKE_GOOGLE, FAKE_GOOGLE),
        (FAKE_JWT, FAKE_JWT),
        (f"Bearer {FAKE_BEARER_TOKEN}", FAKE_BEARER_TOKEN),
        (f"Basic {FAKE_BASIC}", FAKE_BASIC),
        (FAKE_HEX_KEY, FAKE_HEX_KEY),
        (FAKE_BASE64URL, FAKE_BASE64URL),
    ],
)
def test_each_value_pattern(text: str, secret: str) -> None:
    redacted = redact_values(f"antes {text} después")
    assert secret not in redacted
    assert redacted == f"antes {REDACTED} después"


@pytest.mark.parametrize(
    "safe",
    [
        "01920000-0000-7000-8000-0000000000bb",  # UUID: 36 caracteres
        "task-queue-" + "a" * 20,  # `sk-` sin límite de palabra (ta`sk-queue`)
        "a" * 42,
        "a" * 44,
        "ab" * 31,  # 62 hex
        "g" * 64,  # 64 caracteres, no hex
        "Basic auth",
        "código 493817",
    ],
)
def test_values_that_are_not_secrets(safe: str) -> None:
    assert redact_values(safe) == safe


def test_patterns_after_json_escapes() -> None:
    escaped = json.dumps(f"x\n{FAKE_HEX_KEY}\t{FAKE_BASE64URL}\r{FAKE_OPENAI}")
    redacted = redact_values(escaped)
    for secret in (FAKE_HEX_KEY, FAKE_BASE64URL, FAKE_OPENAI):
        assert secret not in redacted
    assert json.loads(redacted) == f"x\n{REDACTED}\t{REDACTED}\r{REDACTED}"


@pytest.mark.parametrize(
    "name",
    [
        "authorization",
        "Authorization",
        "headers",
        "token",
        "secret",
        "cookie",
        "password",
        "api_key",
        "hmac_secret",
        "refresh_token",
        "key",
        "db_key",
        "key_hex",
        "value",
        "pairing_code",
        "x-faro-token",
        "X-Faro-Signature",
        "access_token",
        "client_secret",
        "x-api-key",
        "accessToken",
        "clientSecret",
        "apiKey",
        "dbKey",
        "x2Key",
        "userPassword",
        "PASSWORD_HASH",
    ],
)
def test_sensitive_names(name: str) -> None:
    assert is_sensitive_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "secret_ref",
        "secretRef",
        "secret-ref",
        "SECRET_REF",
        "public_key",
        "publicKey",
        "cache_key",
        "sort_key",
        "primary_key",
        "foreign_key",
        "idempotency_key",
        "idempotencyKey",
        "monkey",
        "hotkey",
        "code",
        "error_code",
        "op",
        "run_id",
        "event",
    ],
)
def test_non_sensitive_names(name: str) -> None:
    assert not is_sensitive_name(name)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("token=test-a1 fin", f"token={REDACTED} fin"),
        ('password: "test a b" fin', f'password: "{REDACTED}" fin'),
        ('{"hmac_secret": "x", "ok": 1', f'{{"hmac_secret": "{REDACTED}", "ok": 1'),
        ("api_key={a: [1, 2]} fin", f"api_key={REDACTED} fin"),
        ('secret=Some("test-x") fin', f"secret={REDACTED} fin"),
        ("secret=Tipo { a: 1 } fin", f"secret={REDACTED} fin"),
        ("Authorization: Token test-t y más", f"Authorization: {REDACTED}"),
        ('Authorization: "Token test-t" fin', f'Authorization: "{REDACTED}" fin'),
        ('msg \\"Authorization: Token t\\" fin', f'msg \\"Authorization: {REDACTED}\\" fin'),
        ("secret_ref=wp/x/token op=get", "secret_ref=wp/x/token op=get"),
        ("pairing_code=493817", f"pairing_code={REDACTED}"),
        ("token=", "token="),
        ('token="sin cierre', f'token="{REDACTED}"'),
        ("token=(a (b) c", f"token={REDACTED}"),
        ('token={"a": "}" } fin', f"token={REDACTED} fin"),
        ('token="a\\"b" fin', f'token="{REDACTED}" fin'),
        ("key: x=token=y", f"key: {REDACTED}"),
        ('token={"a": "x\\"}"} fin', f"token={REDACTED} fin"),
        ("Authorization: Token t\nsiguiente", f"Authorization: {REDACTED}\nsiguiente"),
        ("modo=a key=b", f"modo=a key={REDACTED}"),
    ],
)
def test_named_values_in_free_text(text: str, expected: str) -> None:
    assert redact_text(text) == expected


def test_json_text_inside_a_field_is_redacted_inside() -> None:
    inner = json.dumps({"event": "x", "token": "test-t", "msg": f"k {FAKE_HEX_KEY}"})
    redacted = redact_value(inner)
    assert json.loads(redacted) == {"event": "x", "token": REDACTED, "msg": f"k {REDACTED}"}
    unchanged = json.dumps({"event": "x", "count": 1})
    assert redact_value(unchanged) == unchanged
    assert redact_value("[1, 2") == "[1, 2"  # parece JSON pero no lo es
    assert redact_value('"texto"') == '"texto"'


def test_redact_value_keeps_other_types() -> None:
    assert redact_value(5) == 5
    assert redact_value(None) is None
    assert redact_value({1: "a"}) == {1: "a"}
    assert redact_value((FAKE_HEX_KEY,)) == (REDACTED,)


def test_processors_directly() -> None:
    event = {"event": "x", "token": "test-t", "msg": f"Bearer {FAKE_BEARER_TOKEN}"}
    assert dict(redact_event(None, "info", event)) == {
        "event": "x",
        "token": REDACTED,
        "msg": REDACTED,
    }
    assert redact_rendered(None, "info", f'{{"a": "{FAKE_HEX_KEY}"}}') == f'{{"a": "{REDACTED}"}}'
