"""Metadatos `x-faro-*` de las operaciones (ADR 0010 §3, spec F1a §4.5)."""

from __future__ import annotations

from typing import Any, cast

import pytest
from fastapi import FastAPI

from faro_engine.core.operations import (
    MAX_TIMEOUT_SECONDS,
    MIN_TIMEOUT_SECONDS,
    SECRETS_KEY,
    TIMEOUT_KEY,
    OperationMetadataError,
    SecretAccess,
    SecretGrant,
    faro_operation,
    validate_app_operations,
)


def _grant(ref: str, *access: str) -> SecretGrant:
    return SecretGrant(ref=ref, access=cast(tuple[SecretAccess, ...], access))


def test_faro_operation_without_secrets() -> None:
    assert faro_operation(timeout_seconds=30, secrets=[]) == {
        TIMEOUT_KEY: 30,
        SECRETS_KEY: [],
    }


def test_faro_operation_sorts_refs_and_access_canonically() -> None:
    extra = faro_operation(
        timeout_seconds=45,
        secrets=[
            _grant("wp/{site_id}/token", "delete", "get"),
            _grant("llm/anthropic/default", "get"),
        ],
    )
    assert extra[SECRETS_KEY] == [
        {"ref": "llm/anthropic/default", "access": ["get"]},
        {"ref": "wp/{site_id}/token", "access": ["get", "delete"]},
    ]


@pytest.mark.parametrize(
    "ref",
    [
        "llm/anthropic/default",
        "llm/openai/trabajo_2",
        "llm/gemini/a-b",
        "wp/{site_id}/token",
        "oauth/google/1234567890",
    ],
)
def test_valid_ref_templates(ref: str) -> None:
    faro_operation(timeout_seconds=30, secrets=[_grant(ref, "get")])


def test_new_placeholder_allows_create_and_delete() -> None:
    extra = faro_operation(
        timeout_seconds=60, secrets=[_grant("wp/{new}/token", "delete", "create")]
    )
    assert extra[SECRETS_KEY] == [{"ref": "wp/{new}/token", "access": ["create", "delete"]}]


@pytest.mark.parametrize("timeout", [0, -1, MIN_TIMEOUT_SECONDS - 1, MAX_TIMEOUT_SECONDS + 1])
def test_timeout_out_of_range_is_rejected(timeout: int) -> None:
    with pytest.raises(OperationMetadataError, match="timeout_seconds"):
        faro_operation(timeout_seconds=timeout, secrets=[])


@pytest.mark.parametrize("timeout", [True, 30.0, "30", None])
def test_timeout_must_be_an_integer(timeout: object) -> None:
    with pytest.raises(OperationMetadataError, match="timeout_seconds"):
        faro_operation(timeout_seconds=cast(int, timeout), secrets=[])


@pytest.mark.parametrize(
    "ref", ["db/{profile_id}/key", "db/0192a6e2-6b9c-7c2e-9f1a-0a1b2c3d4e5f/key"]
)
def test_db_refs_are_never_granted(ref: str) -> None:
    with pytest.raises(OperationMetadataError, match="nunca se concede"):
        faro_operation(timeout_seconds=30, secrets=[_grant(ref, "get")])


@pytest.mark.parametrize(
    "ref",
    [
        "llm/{provider}/default",  # los parámetros se validan como UUID en el núcleo
        "llm/mistral/default",
        "llm/anthropic/Default",
        "wp/0192a6e2-6b9c-7c2e-9f1a-0a1b2c3d4e5f/token",  # UUID literal: siempre {param}
        "wp/{Site}/token",
        "wp/{site_id}/hmac",
        "wp/*/token",
        "oauth/google/{account}",
        "oauth/google/usuario@example.com",
        "",
        "wp/{site_id}/token\n",
    ],
)
def test_invalid_ref_templates_are_rejected(ref: str) -> None:
    with pytest.raises(OperationMetadataError, match="gramática"):
        faro_operation(timeout_seconds=30, secrets=[_grant(ref, "get")])


def test_access_must_not_be_empty() -> None:
    with pytest.raises(OperationMetadataError, match="ningún acceso"):
        faro_operation(timeout_seconds=30, secrets=[_grant("wp/{site_id}/token")])


def test_access_must_not_repeat() -> None:
    with pytest.raises(OperationMetadataError, match="repite"):
        faro_operation(timeout_seconds=30, secrets=[_grant("wp/{site_id}/token", "get", "get")])


def test_unknown_access_is_rejected() -> None:
    with pytest.raises(OperationMetadataError, match="desconocidos"):
        faro_operation(timeout_seconds=30, secrets=[_grant("wp/{site_id}/token", "read")])


@pytest.mark.parametrize("access", [("get",), ("delete",), ("create", "get"), ("create", "set")])
def test_new_placeholder_only_allows_create_and_delete(access: tuple[str, ...]) -> None:
    with pytest.raises(OperationMetadataError, match=r"\{new\}"):
        faro_operation(timeout_seconds=60, secrets=[_grant("wp/{new}/token", *access)])


def test_duplicated_refs_are_rejected() -> None:
    with pytest.raises(OperationMetadataError, match="más de una vez"):
        faro_operation(
            timeout_seconds=30,
            secrets=[_grant("wp/{site_id}/token", "get"), _grant("wp/{site_id}/token", "set")],
        )


def _app_with(path: str, extra: dict[str, Any] | None) -> FastAPI:
    app = FastAPI()

    async def endpoint() -> dict[str, str]:  # pragma: no cover - nunca se llama
        return {}

    app.add_api_route(
        path, endpoint, methods=["POST"], operation_id="probeOperation", openapi_extra=extra
    )
    return app


def test_validate_app_accepts_declared_operations() -> None:
    extra = faro_operation(
        timeout_seconds=45,
        secrets=[_grant("wp/{site_id}/token", "get"), _grant("llm/openai/default", "get")],
    )
    validate_app_operations(_app_with("/sites/{site_id}/check", extra))
    validate_app_operations(
        _app_with(
            "/sites",
            faro_operation(timeout_seconds=60, secrets=[_grant("wp/{new}/token", "create")]),
        )
    )


def test_validate_app_ignores_routes_outside_the_schema() -> None:
    app = FastAPI()

    async def endpoint() -> dict[str, str]:  # pragma: no cover - nunca se llama
        return {}

    app.add_api_route("/interna", endpoint, include_in_schema=False)
    validate_app_operations(app)


@pytest.mark.parametrize(
    "extra",
    [
        None,
        {},
        {TIMEOUT_KEY: 30},
        {SECRETS_KEY: []},
        {TIMEOUT_KEY: 30, SECRETS_KEY: [], "x-faro-timeout": 30},
    ],
)
def test_validate_app_requires_exactly_the_faro_extensions(extra: dict[str, Any] | None) -> None:
    with pytest.raises(OperationMetadataError, match="faro_operation"):
        validate_app_operations(_app_with("/probe", extra))


def test_validate_app_keeps_other_openapi_extra() -> None:
    extra = {**faro_operation(timeout_seconds=30, secrets=[]), "deprecated": True}
    validate_app_operations(_app_with("/probe", extra))


@pytest.mark.parametrize(
    "secrets", [[{"access": ["get"]}], [{"ref": "wp/{id}/token"}], ["wp/{id}/token"], None]
)
def test_validate_app_rejects_malformed_secrets(secrets: object) -> None:
    extra = {TIMEOUT_KEY: 30, SECRETS_KEY: secrets}
    with pytest.raises(OperationMetadataError, match="forma inválida"):
        validate_app_operations(_app_with("/probe/{id}", extra))


def test_validate_app_revalidates_hand_written_metadata() -> None:
    extra = {TIMEOUT_KEY: 30, SECRETS_KEY: [{"ref": "db/{id}/key", "access": ["get"]}]}
    with pytest.raises(OperationMetadataError, match=r"probeOperation.*nunca se concede"):
        validate_app_operations(_app_with("/probe/{id}", extra))


def test_validate_app_requires_canonical_form() -> None:
    extra = {
        TIMEOUT_KEY: 30,
        SECRETS_KEY: [{"ref": "wp/{id}/token", "access": ["delete", "get"]}],
    }
    with pytest.raises(OperationMetadataError, match="canónica"):
        validate_app_operations(_app_with("/probe/{id}", extra))


def test_validate_app_requires_placeholders_to_be_path_params() -> None:
    extra = faro_operation(timeout_seconds=30, secrets=[_grant("wp/{site_id}/token", "get")])
    with pytest.raises(OperationMetadataError, match="site_id"):
        validate_app_operations(_app_with("/sites/{id}/check", extra))


def test_validate_app_reserves_new_as_path_param() -> None:
    extra = faro_operation(timeout_seconds=30, secrets=[])
    with pytest.raises(OperationMetadataError, match="reservado"):
        validate_app_operations(_app_with("/sites/{new}", extra))
