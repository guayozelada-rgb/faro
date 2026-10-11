"""Exportación del OpenAPI (fuente de `npm run contracts`)."""

from __future__ import annotations

import io
import json
import subprocess
import sys

from faro_engine import __version__
from faro_engine.export_openapi import main, render_openapi


def test_export_writes_openapi_json() -> None:
    out = io.BytesIO()
    assert main(out) == 0
    schema = json.loads(out.getvalue())
    assert schema["info"]["version"] == __version__
    operations = [op["operationId"] for path in schema["paths"].values() for op in path.values()]
    assert operations == [
        "getHealth",
        "listSites",
        "connectSite",
        "reconnectSite",
        "checkSiteConnection",
        "listSiteContent",
        "removeSite",
        "getLlmUsage",
        "setLlmDailyLimit",
        "setLlmPreferences",
        "listAgents",
        "estimateAgentRun",
        "startAgentRun",
        "listAgentRuns",
        "getAgentRun",
        "cancelAgentRun",
        "acknowledgeAgentNotices",
        "listSchedules",
        "createSchedule",
        "updateSchedule",
        "deleteSchedule",
    ]
    assert {"ErrorOut", "HealthOut", "SiteOut", "SiteContentPage"} <= set(
        schema["components"]["schemas"]
    )


def test_export_is_deterministic() -> None:
    assert render_openapi() == render_openapi()


def test_export_module_runs_as_script() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "faro_engine.export_openapi"],
        capture_output=True,
        check=True,
        timeout=60,
    )
    schema = json.loads(result.stdout)
    assert "/health" in schema["paths"]
    assert not result.stdout.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" not in result.stdout
