"""Fixtures de `core/jobs`: logs capturados en memoria."""

from __future__ import annotations

import io
from collections.abc import Iterator

import pytest

from faro_engine.core.logging import configure_logging


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    configure_logging(stream=stream)
    yield stream
    configure_logging()
