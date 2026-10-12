"""Fixtures de las evaluaciones sin red: base cifrada temporal con el sitio de pruebas."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from faro_engine.core.db.database import Database, open_profile_database
from tests.db.helpers import TEST_PROFILE_ID, key
from tests.jobs.world import seed


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    db = open_profile_database(tmp_path, TEST_PROFILE_ID, key())
    seed(db)
    yield db
    db.close()
